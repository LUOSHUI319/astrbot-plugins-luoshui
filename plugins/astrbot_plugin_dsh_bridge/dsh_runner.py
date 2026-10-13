"""DSH headless 运行器：把一条任务交给 DSH 子代理跑，并把事件流解析成结构化结果。

这个模块刻意不依赖 AstrBot，方便单独测试。
调用方式：

```python
import asyncio
from dsh_runner import run_task

result = asyncio.run(run_task("列出当前目录的文件", workspace=r"E:\\dsh_agent_ws"))
print(result.final)
```

要点：
- 命令形态是 `dsh --profile <profile> --json "<task>"`，事件是按行 JSON 走 stdout，诊断走 stderr。
- Windows 下 `dsh` 实际是 dsh.cmd，asyncio 直接 exec 跑不起来，所以统一走 `cmd /c`。
- 超时到点杀整个进程树；退出码 0 表示任务完成，1 表示中止或出错。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field


@dataclass
class RunResult:
    """一次 headless 运行的结果。"""

    ok: bool = False
    final: str = ""
    session_id: str = ""
    tool_calls: list[str] = field(default_factory=list)
    thinking_chars: int = 0
    usage: dict = field(default_factory=dict)
    duration: float = 0.0
    exit_code: int | None = None
    error: str = ""
    event_count: int = 0
    stderr_tail: str = ""
    timed_out: bool = False
    needs_acl: bool = False
    acl_fixed: str = ""


def _summarize_tool_input(tool: str, payload) -> str:
    """把工具调用压成一行短描述，方便回传聊天。"""
    if not isinstance(payload, dict):
        text = str(payload)
    else:
        for key in ("command", "description", "path", "file_path", "pattern", "prompt", "text"):
            if payload.get(key):
                text = f"{key}={payload[key]}"
                break
        else:
            text = json.dumps(payload, ensure_ascii=False)
    text = " ".join(str(text).split())
    if len(text) > 120:
        text = text[:117] + "..."
    return f"{tool}: {text}"


def _decode(chunk: bytes) -> str:
    return chunk.decode("utf-8", errors="replace")


async def _run_once(
    task: str,
    *,
    workspace: str,
    profile: str = "headless",
    dsh_cmd: str = "dsh",
    timeout: int = 300,
    session_id: str = "",
    env_extra: dict | None = None,
    on_event=None,
) -> RunResult:
    """跑一条任务，返回结构化结果。on_event(event: dict) 可选，用来做实时进度。"""
    result = RunResult()
    ws = os.path.abspath(workspace)
    if not os.path.isdir(ws):
        os.makedirs(ws, exist_ok=True)

    base = [dsh_cmd, "--profile", profile, "--json"]
    if session_id:
        base += ["--session-id", session_id]
    base.append(task)

    if os.name == "nt":
        argv = ["cmd", "/c"] + base
    else:
        resolved = shutil.which(dsh_cmd) or dsh_cmd
        argv = [resolved] + base[1:]

    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if env_extra:
        env.update(env_extra)

    started = time.time()
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=ws,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        async def pump_stdout() -> None:
            assert proc is not None and proc.stdout is not None
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = _decode(raw).strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                result.event_count += 1
                etype = event.get("type")
                if etype == "session":
                    result.session_id = str(event.get("sessionId") or "")
                elif etype == "thinking":
                    result.thinking_chars += len(str(event.get("text") or ""))
                elif etype == "tool_call":
                    result.tool_calls.append(_summarize_tool_input(str(event.get("tool") or "?"), event.get("input")))
                elif etype == "tool_result":
                    blob = str(event.get("result") or "")
                    if "grantWrite" in blob or "SetNamedSecurityInfoW" in blob:
                        result.needs_acl = True
                elif etype == "status":
                    usage = event.get("usage")
                    if isinstance(usage, dict):
                        result.usage = usage
                elif etype == "final":
                    result.final = str(event.get("text") or "")
                if on_event is not None:
                    try:
                        on_event(event)
                    except Exception:
                        pass

        async def pump_stderr() -> None:
            assert proc is not None and proc.stderr is not None
            buf: list[str] = []
            while True:
                raw = await proc.stderr.readline()
                if not raw:
                    break
                text = _decode(raw).rstrip()
                if text:
                    buf.append(text)
                    del buf[:-20]
            result.stderr_tail = "\n".join(buf)[-1500:]

        try:
            await asyncio.wait_for(asyncio.gather(pump_stdout(), pump_stderr()), timeout=timeout)
            result.exit_code = await proc.wait()
        except asyncio.TimeoutError:
            result.timed_out = True
            result.error = f"超过 {timeout} 秒还没跑完，已中止"
            _kill_tree(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except Exception:
                pass
        result.ok = (not result.timed_out) and result.exit_code == 0 and bool(result.final)
        if not result.ok and not result.error:
            if result.exit_code not in (0, None):
                result.error = (result.stderr_tail or "").strip() or f"退出码 {result.exit_code}"
            elif not result.final:
                result.error = (result.stderr_tail or "").strip() or "没有拿到最终答案"
    except FileNotFoundError:
        result.error = f"找不到可执行文件：{dsh_cmd}（确认 dsh 在 PATH 里）"
    except Exception as exc:  # noqa: BLE001
        result.error = f"{type(exc).__name__}: {exc}"
        if proc is not None:
            _kill_tree(proc)
    finally:
        result.duration = round(time.time() - started, 1)
    return result


def _kill_tree(proc) -> None:
    """杀掉进程树（windows 用 taskkill，其他用 killpg）。"""
    try:
        if os.name == "nt":
            subprocess_run(["taskkill", "/F", "/T", "/PID", str(proc.pid)])
        else:
            os.killpg(os.getpgid(proc.pid), 9)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def subprocess_run(argv) -> None:
    """同步小工具：只用于收尾杀进程，失败无所谓。"""
    import subprocess

    try:
        subprocess.run(argv, capture_output=True, timeout=15)
    except Exception:
        pass


# ── 环境探测（为了在别人的机器上也能用） ─────────────────────────


async def run_task(
    task: str,
    *,
    workspace: str,
    profile: str = "headless",
    dsh_cmd: str = "dsh",
    timeout: int = 300,
    session_id: str = "",
    env_extra: dict | None = None,
    on_event=None,
) -> RunResult:
    """跑一条任务；如果撞上沙箱授权失败（Windows 上没有改工作区 ACL 的权限），自动修一次再重试。"""
    result = await _run_once(
        task,
        workspace=workspace,
        profile=profile,
        dsh_cmd=dsh_cmd,
        timeout=timeout,
        session_id=session_id,
        env_extra=env_extra,
        on_event=on_event,
    )
    if result.needs_acl:
        ok, detail = ensure_workspace_acl(workspace)
        result.acl_fixed = f"{'已修' if ok else '没修成'}：{detail}"
        if ok:
            retry = await _run_once(
                task,
                workspace=workspace,
                profile=profile,
                dsh_cmd=dsh_cmd,
                timeout=timeout,
                session_id=session_id,
                env_extra=env_extra,
                on_event=on_event,
            )
            retry.acl_fixed = result.acl_fixed
            return retry
    return result


def ensure_workspace_acl(workspace: str) -> tuple[bool, str]:
    """Windows 上给工作区授当前用户完全控制。

    DSH 的沙箱在跑命令前会给工作区授予写权限，如果当前用户对目录没有改 ACL 的权限，
    就会报 SetNamedSecurityInfoW failed (Win32 5): grantWrite(...)。这里先手补上。
    """
    import subprocess

    if os.name != "nt":
        return True, "非 Windows，不需要"
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".strip("\\")
    try:
        proc = subprocess.run(
            ["icacls", workspace, "/grant", f"{user}:(OI)(CI)F", "/T", "/C"],
            capture_output=True,
            text=True,
            timeout=180,
            errors="replace",
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        last = [l for l in out.strip().splitlines() if l.strip()]
        return proc.returncode == 0, (last[-1].strip() if last else "")
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


# ── 一键装 DSH（给别人机器用） ───────────────────────────────────

def install_portable_dsh(target_dir: str, version: str = "latest") -> tuple[bool, str]:
    """在插件数据目录里装一份便携 DSH，省掉全局安装。

    装完的可执行文件会落在 <target_dir>/node_modules/.bin/dsh(.cmd)，find_dsh 会自动认。
    """
    import subprocess

    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        return False, "没找到 npm：先装 Node.js（DSH 要 22.19.x 或 24+）"
    os.makedirs(target_dir, exist_ok=True)
    argv = [
        npm, "i", "--prefix", target_dir, f"@deepseek-ai/dsh@{version}",
        "--no-fund", "--no-audit",
        "--allow-scripts=@deepseek-ai/dsh-subprocess-local,koffi,node-pty,@google/genai,protobufjs",
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=2400, errors="replace")
    except subprocess.TimeoutExpired:
        return False, "装了 40 分钟还没完，先停了（网络慢的话可以挂个镜像再来）"
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    tail = "\n".join(out.splitlines()[-4:])
    exe = os.path.join(target_dir, "node_modules", ".bin", "dsh.cmd" if os.name == "nt" else "dsh")
    if os.path.exists(exe):
        return True, f"装好了：{exe}"
    if os.path.exists(exe.replace(".cmd", "")):
        return True, f"装好了：{exe.replace('.cmd', '')}"
    return False, "npm 跑完了但没找到 dsh，输出尾部：\n" + tail


def find_dsh(hint: str = "", extra_dirs: list[str] | None = None) -> str:
    """找 dsh 可执行文件：显式路径 > PATH > npm 全局目录 > 插件自带运行时 > 兜底给 dsh。

    别人机器上 dsh 很可能不在 PATH 里（比如只在 npm 全局目录），所以这里逐个探。
    """
    hint = (hint or "").strip()
    cands: list[str] = []
    if hint and hint.lower() not in ("dsh", "dsh.cmd"):
        cands.append(os.path.expandvars(os.path.expanduser(hint)))
    which = shutil.which(hint or "dsh")
    if which:
        cands.append(which)
    home = os.path.expanduser("~")
    appdata = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    sep = os.sep
    if os.name == "nt":
        cands += [
            os.path.join(appdata, "npm", "dsh.cmd"),
            os.path.join(home, ".dsh-runtime", "node_modules", ".bin", "dsh.cmd"),
            os.path.join(os.environ.get("ProgramFiles", "C:" + sep + "Program Files"), "nodejs", "dsh.cmd"),
        ]
    else:
        cands += [
            os.path.join(home, ".dsh-runtime", "node_modules", ".bin", "dsh"),
            "/usr/local/bin/dsh",
            "/usr/bin/dsh",
        ]
    for d in extra_dirs or []:
        if not d:
            continue
        name = "dsh.cmd" if os.name == "nt" else "dsh"
        cands.insert(0, os.path.join(d, "node_modules", ".bin", name))
        cands.insert(0, os.path.join(os.path.expanduser(d), "node_modules", ".bin", name))
    for cand in cands:
        if cand and os.path.exists(cand):
            return cand
    return hint or "dsh"


def _version_of(argv: list[str], timeout: int = 20) -> str:
    import subprocess

    # Windows 上 npm / pnpm / corepack 都是 .cmd 垫片，直接 exec 会 FileNotFoundError
    exe = shutil.which(argv[0]) or shutil.which(argv[0] + ".cmd") or argv[0]
    argv = [exe] + list(argv[1:])
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, errors="replace")
        out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        return out.splitlines()[0].strip() if out else ""
    except Exception as exc:  # noqa: BLE001
        return f"（没跑起来：{type(exc).__name__}）"


def probe_environment(dsh_cmd: str = "", profile: str = "headless", workspace: str = "") -> dict:
    """体检：把"这台机器能不能跑"需要的信息一次性收集起来。"""
    info: dict = {}
    info["platform"] = f"{os.name} / {sys.platform}"
    info["node"] = _version_of(["node", "-v"])
    info["npm"] = _version_of(["npm", "-v"])
    info["pnpm"] = _version_of(["pnpm", "-v"]) or _version_of(["corepack", "-v"])
    resolved = find_dsh(dsh_cmd)
    info["dsh_path"] = resolved
    info["dsh_version"] = _version_of(["cmd", "/c", resolved, "--version"] if os.name == "nt" else [resolved, "--version"])
    home_dsh = os.path.join(os.path.expanduser("~"), ".dsh")
    info["dsh_home"] = home_dsh
    info["dsh_home_exists"] = os.path.isdir(home_dsh)
    cred = os.path.join(home_dsh, ".credentials.yaml")
    info["credentials"] = os.path.exists(cred)
    profiles_dir = os.path.join(home_dsh, "profiles")
    try:
        info["profiles"] = sorted(
            d
            for d in os.listdir(profiles_dir)
            if os.path.isdir(os.path.join(profiles_dir, d)) and not d.startswith(".") and d != "node_modules"
        )
    except Exception:
        info["profiles"] = []
    info["profile_ready"] = profile in info["profiles"]
    info["portable_dsh"] = os.path.exists(
        os.path.join(os.path.expanduser("~"), ".dsh-runtime", "node_modules", ".bin", "dsh.cmd" if os.name == "nt" else "dsh")
    )
    if workspace:
        try:
            os.makedirs(workspace, exist_ok=True)
            probe_file = os.path.join(workspace, ".dsh-bridge-write-test")
            with open(probe_file, "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.remove(probe_file)
            info["workspace_writable"] = True
        except Exception as exc:  # noqa: BLE001
            info["workspace_writable"] = f"不能写：{exc}"
    return info


def format_probe(info: dict) -> str:
    """把体检结果写成一段给人看的话，顺便说缺什么怎么补。"""
    lines = [
        f"系统：{info.get('platform')}",
        f"Node：{info.get('node') or '没找到'} | npm：{info.get('npm') or '没找到'} | pnpm：{info.get('pnpm') or '没找到'}",
        f"dsh：{info.get('dsh_path')}（版本 {info.get('dsh_version') or '读不到'}）",
        f"DSH home：{info.get('dsh_home')}（{'在' if info.get('dsh_home_exists') else '不在'}）",
        f"凭据：{'已就绪' if info.get('credentials') else '没有，需要先在本机登录一次 DSH，或改用 API key 适配器'}",
        f"profile：{'、'.join(info.get('profiles') or []) or '（空）'}",
        f"目标 profile：{'已初始化' if info.get('profile_ready') else '还没初始化（第一次跑任务时 DSH 会从模板自动建）'}",
    ]
    if "workspace_writable" in info:
        lines.append(f"工作区可写：{info['workspace_writable']}")
    if info.get("api_key_source"):
        lines.append(f"模型 key：{info['api_key_source']}")
    if info.get("portable_dsh") is not None:
        lines.append(f"便携 DSH：{'已装' if info['portable_dsh'] else '没装（可用 /dsh装dsh）'}"
                     + (f" | 目标目录：{info['portable_target']}" if info.get("portable_target") else ""))
    if info.get("acl"):
        lines.append(f"工作区权限：{info['acl']}")
    tips = []
    if not info.get("node"):
        tips.append("装 Node（DSH 要 22.19.x 或 24+）")
    if "没跑起来" in str(info.get("dsh_version")) or not info.get("dsh_version"):
        tips.append("npm i -g @deepseek-ai/dsh 装 DSH，或把 dsh 的完整路径填进插件配置")
    if not info.get("credentials"):
        tips.append("在 DSH 里登录一次账号，或给 profile 挂 API key 适配器（apiKeyEnv 方式）")
    if not info.get("pnpm") and not info.get("npm"):
        tips.append("corepack enable 打开 pnpm（装插件要用）")
    if tips:
        lines.append("还需要：")
        lines += [f"- {t}" for t in tips]
    else:
        lines.append("看起来什么都不缺，直接派活就行")
    return chr(10).join(lines)
