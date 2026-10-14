"""
DSH 子代理桥：把任务派给本机的 DeepSeek Harness（headless profile）执行，再把结果收回来。

- 子代理在指定工作区里干活（读写文件、跑命令、调它自己装的那些插件工具）
- 每次调用都是一次独立会话；想接着上文跑就把上次返回的 session_id 带回来
- 一次只跑一个任务（串行队列），避免 profile 目录和会话存储被并发踩
- 只有白名单里的用户能让它干活（默认只有望山）
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_runner():
    """按文件路径把 dsh_runner 重新加载一遍。

    不能直接 `import dsh_runner`：AstrBot 重载插件时只清插件自己的模块，
    顶层名的 dsh_runner 会留在 sys.modules 里，改过代码也会命中旧版本
    （表现就是 cannot import name 'xxx' from 'dsh_runner'）。
    这里每次用新的模块名从文件重新执行，顺带避免和别的插件重名。
    """
    import importlib.util

    name = "astrbot_plugin_dsh_bridge_runner"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, os.path.join(_PLUGIN_DIR, "dsh_runner.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_runner = _load_runner()
find_dsh = _runner.find_dsh
format_probe = _runner.format_probe
probe_environment = _runner.probe_environment
run_task = _runner.run_task
install_portable_dsh = _runner.install_portable_dsh
ensure_workspace_acl = _runner.ensure_workspace_acl
_summarize_tool_input = _runner._summarize_tool_input

DEFAULTS = {
    "enabled": True,
    "workspace": "",
    "profile": "headless",
    "dsh_cmd": "dsh",
    "timeout_seconds": 300,
    "max_final_chars": 3000,
    "allow_user_ids": "",
    "log_runs": True,
    "inject_manual": True,
    "operator_default": False,
    "share_python_path": True,
    "manual_text": "",
    "api_key": "",
    "api_base": "",
    "model": "",
    "auto_fix_acl": True,
    "dsh_install_dir": "",
    "dsh_version": "latest",
    "web_auto_start": True,
    "web_port": 3939,
    "web_workspace": "",
}


MANUAL = """【DSH 子代理操作手册（操作手模式已开启）】
现在是操作手模式：收到望山的要求先判断要不要动用 Harness——不需要就直接正常聊；
需要就先想清怎么做、把提示词派出去（dsh_task），再用 dsh_trace 自己检查，不满意就再派一轮，
跑完他发 /操作手 关 就退回普通聊天。
你手上有一条通到本机 DeepSeek Harness（DSH）的通道：工具 dsh_task 派单、dsh_trace 回看它干过什么、
dsh_sessions 列会话。它不是让你传话的，是让你当操作手的，按这个循环干：
1. 接活先想清楚：拆几步、每步要它做什么、产物放哪、怎么算做完。把计划写进任务档案（工作区里建任务目录，
   计划、每轮提示词、每轮结果、验收记录各存一份）。
2. 派单：dsh_task 的任务描述要具体，写清目标、约束、验收标准、工作目录。
3. 收结果先自查，别直接转达：用 dsh_trace 看它实际跑了哪些命令、有没有报错；需要的话自己用 shell 和
   文件工具去翻产物、跑测试、看日志。
4. 不满意就再派一轮：把上轮的问题和新的要求写清楚，session_id 带上同一个，上下文不断。
5. 满意了再汇报：做了什么、产物在哪、你怎么验证的、还剩什么没做到。
6. 全程留痕：任务档案目录里能看出你思考过什么、试过什么。
"""


@register("astrbot_plugin_dsh_bridge", "洛水", "DSH 子代理桥：派单给 DeepSeek Harness 子代理，并支持操作手模式（派单—自查—迭代—汇报）", "0.2.0")
class DshBridgePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.cfg = {**DEFAULTS, **(config or {})}
        self._lock = asyncio.Lock()
        self._data_dir = str(StarTools.get_data_dir("astrbot_plugin_dsh_bridge"))
        os.makedirs(self._data_dir, exist_ok=True)


    def _install_dir(self) -> str:
        """便携 dsh 的安装目录：配置优先，默认放插件数据目录下。"""
        raw = str(self._conf("dsh_install_dir") or "").strip()
        if raw:
            return os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))
        return os.path.join(self._data_dir, "dsh-runtime")

    # ── Key 与运行环境 ─────────────────────────────────────────
    def _bot_deepseek_key(self) -> str:
        """从 AstrBot 已配置的提供商里挑一个 DeepSeek 的 key（默认就用它）。"""
        try:
            providers = self.context.get_all_providers()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DshBridge] 读提供商失败: %s", exc)
            return ""
        fallback = ""
        for provider in providers or []:
            cfg = getattr(provider, "provider_config", {}) or {}
            if not isinstance(cfg, dict):
                continue
            raw_keys = cfg.get("key")
            if isinstance(raw_keys, str):
                keys = [raw_keys]
            elif isinstance(raw_keys, (list, tuple)):
                keys = [str(k) for k in raw_keys]
            else:
                keys = []
            key = next((k for k in keys if k and k.strip()), "")
            if not key:
                continue
            ptype = str(cfg.get("type") or "")
            model = ""
            try:
                model = str(provider.get_model() or "")
            except Exception:  # noqa: BLE001
                model = ""
            blob = f"{ptype} {model} {cfg.get('api_base') or ''}".lower()
            if "deepseek" in blob:
                return key.strip()
            fallback = fallback or key.strip()
        return fallback

    def _api_key(self) -> tuple[str, str]:
        """返回 (key, 来源说明)：配置里填了用配置的，留空就用 bot 里那个 DeepSeek 的。"""
        own = str(self._conf("api_key") or "").strip()
        if own:
            return own, "插件配置里填的"
        from_bot = self._bot_deepseek_key()
        if from_bot:
            return from_bot, "bot 里配置的 DeepSeek key"
        return "", ""

    def _run_env(self) -> tuple[dict, str]:
        env: dict = {}
        key, source = self._api_key()
        if key:
            env["DEEPSEEK_API_KEY"] = key
        base = str(self._conf("api_base") or "").strip()
        if base:
            env["DEEPSEEK_BASE_URL"] = base
        if self._conf("share_python_path"):
            # 子代理的 shell 里常常没有 python/node（Windows 上还有商店别名坑），
            # 把 AstrBot 自己那个解释器目录前置到 PATH，它就能直接跑脚本
            here = os.path.dirname(sys.executable)
            current = os.environ.get("PATH", "")
            if here and here.lower() not in current.lower():
                env["PATH"] = here + os.pathsep + current
        return env, source

    def _ensure_model_patch(self, model: str) -> str:
        """把默认模型写进目标 profile 的 patch（留空则不动，DSH 默认就是 flash）。"""
        model = (model or "").strip()
        if not model:
            return ""
        profile = str(self._conf("profile"))
        path = os.path.join(os.path.expanduser("~"), ".dsh", "profiles", profile, "cordis.patch.yml")
        if not os.path.exists(path):
            return f"没找到 {path}，模型没改"
        try:
            text = open(path, encoding="utf-8").read()
        except Exception as exc:  # noqa: BLE001
            return f"读 patch 失败：{exc}"
        if f"model: {model}" in text and "agent-default-model" in text:
            return ""
        row = (
            f"# 由 DSH 桥写入：新会话默认模型\n"
            f"- id: agent-default-model\n"
            f"  config:\n"
            f"    provider: deepseek-official\n"
            f"    model: {model}\n"
        )
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(("\n" if not text.endswith("\n") else "") + row)
        except Exception as exc:  # noqa: BLE001
            return f"写 patch 失败：{exc}"
        return f"默认模型已设为 {model}（写进 {profile} profile 的 patch）"

    # ── 路径解析 ───────────────────────────────────────────────
    def _workspace(self) -> str:
        """工作区：配置里填了就用填的，没填就用插件数据目录下的 workspace（换机器也能用）。"""
        raw = str(self._conf("workspace") or "").strip()
        if raw:
            return os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))
        path = os.path.join(self._data_dir, "workspace")
        os.makedirs(path, exist_ok=True)
        return path

    # ── 配置与权限 ─────────────────────────────────────────────
    def _conf(self, key: str):
        value = self.cfg.get(key, DEFAULTS.get(key))
        if value is None or value == "":
            return DEFAULTS.get(key)
        return value

    def _allowed(self, event: AstrMessageEvent) -> bool:
        raw = str(self._conf("allow_user_ids") or "").strip()
        if not raw:
            return True
        allow = {x.strip() for x in raw.replace("，", ",").split(",") if x.strip()}
        return str(event.get_sender_id()) in allow

    def _log_run(self, line: str) -> None:
        if not self._conf("log_runs"):
            return
        try:
            path = os.path.join(self._data_dir, "runs.jsonl")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DshBridge] 写日志失败: %s", exc)

    # ── 核心：跑一条任务 ───────────────────────────────────────
    async def _run(self, task: str, timeout: int = 0, session_id: str = "") -> str:
        task = (task or "").strip()
        if not task:
            return "任务描述是空的，先说要它干什么"
        timeout = int(timeout or 0) or int(self._conf("timeout_seconds") or 300)
        workspace = self._workspace()
        profile = str(self._conf("profile"))
        dsh_cmd = find_dsh(str(self._conf("dsh_cmd")), [self._install_dir()])
        max_chars = int(self._conf("max_final_chars") or 3000)

        if self._lock.locked():
            logger.info("[DshBridge] 上一个任务还在跑，排队等待")
        async with self._lock:
            started = time.time()
            env_extra, key_source = self._run_env()
            model_note = self._ensure_model_patch(str(self._conf("model") or ""))
            result = await run_task(
                task,
                workspace=workspace,
                profile=profile,
                dsh_cmd=dsh_cmd,
                timeout=timeout,
                session_id=session_id,
                env_extra=env_extra,
            )
            self._log_run(
                f'{{"at":"{datetime.now():%Y-%m-%d %H:%M:%S}","task":{task!r},"ok":{result.ok},'
                f'"seconds":{result.duration},"session":{result.session_id!r},'
                f'"tools":{result.tool_calls!r},"error":{result.error!r}}}'
            )

        head = "子代理跑完了" if result.ok else "子代理没跑成"
        lines = [f"{head}（耗时 {result.duration}s，工作区 {workspace}）"]
        if result.tool_calls:
            shown = result.tool_calls[:8]
            lines.append("它动手做了这些：" + "；".join(shown))
            if len(result.tool_calls) > len(shown):
                lines.append(f"（还有 {len(result.tool_calls) - len(shown)} 步没列）")
        if result.final:
            text = result.final.strip()
            if len(text) > max_chars:
                text = text[:max_chars] + "\n…（太长截断了）"
            lines.append("它的答复：")
            lines.append(text)
        if not result.ok:
            lines.append("失败原因：" + (result.error or "未知"))
        if result.session_id and result.ok:
            lines.append(f"会话 id：{result.session_id}（想接着聊就带上它）")
        if result.usage:
            lines.append(
                "用量：输入 {inputTokens} / 输出 {outputTokens}".format(
                    inputTokens=result.usage.get("inputTokens", "?"),
                    outputTokens=result.usage.get("outputTokens", "?"),
                )
            )
        if result.timed_out:
            lines.append("注意：是超时被中途中止的，子代理可能留下了半成品")
        if result.acl_fixed:
            lines.append("沙箱授权失败，已自动修工作区权限：" + result.acl_fixed)
        lines.append(f"用的 key：{key_source or '（没找到，可能要在这台机器上登录 DSH 或填一个）'}")
        if model_note:
            lines.append(model_note)
        logger.info("[DshBridge] 任务结束 ok=%s 耗时=%ss 工具步数=%s", result.ok, result.duration, len(result.tool_calls))
        return "\n".join(lines)

    # ── 会话辅助 ───────────────────────────────────────────────
    def _recent_sessions(self, limit: int = 8) -> list[tuple[str, float]]:
        """列出当前工作区里最近的几个 DSH 会话（id, 修改时间戳）。"""
        base = os.path.join(
            os.path.expanduser("~"), ".dsh", "sessions", _workspace_session_key(self._workspace())
        )
        found: list[tuple[str, float]] = []
        if not os.path.isdir(base):
            return found
        for name in os.listdir(base):
            path = os.path.join(base, name)
            if not name.startswith("session-") or not os.path.isdir(path):
                continue
            newest = 0.0
            for root, _dirs, files in os.walk(path):
                for fn in files:
                    try:
                        newest = max(newest, os.path.getmtime(os.path.join(root, fn)))
                    except OSError:
                        pass
            found.append((name, newest or os.path.getmtime(path)))
        found.sort(key=lambda item: item[1], reverse=True)
        return found[:limit]



    # ── 操作手模式开关 ─────────────────────────────────────────
    def _operator_state_path(self) -> str:
        return os.path.join(self._data_dir, "operator.json")

    def _operator_state(self) -> dict:
        try:
            with open(self._operator_state_path(), encoding="utf-8") as fh:
                return json.load(fh) or {}
        except Exception:  # noqa: BLE001
            return {}

    def _operator_on(self) -> bool:
        state = self._operator_state()
        if not state:
            return bool(self._conf("operator_default"))
        return bool(state.get("on"))

    def _set_operator(self, on: bool, event: AstrMessageEvent) -> str:
        state = {
            "on": bool(on),
            "by": str(event.get_sender_id()),
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            with open(self._operator_state_path(), "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001
            return f"写状态失败：{exc}"
        return ""

    # ── 会话记忆与回看 ─────────────────────────────────────────
    def _session_state_path(self) -> str:
        return os.path.join(self._data_dir, "sessions.json")

    def _load_session_state(self) -> dict:
        try:
            with open(self._session_state_path(), encoding="utf-8") as fh:
                return json.load(fh) or {}
        except Exception:
            return {}

    def _remember_session(self, event: AstrMessageEvent, session_id: str) -> None:
        if not session_id:
            return
        state = self._load_session_state()
        state[self._chat_key(event)] = {"session_id": session_id, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        try:
            with open(self._session_state_path(), "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DshBridge] 记会话失败: %s", exc)

    def _chat_key(self, event: AstrMessageEvent) -> str:
        try:
            return f"{event.get_platform_id()}:{event.get_group_id() or 'private'}:{event.get_sender_id()}"
        except Exception:  # noqa: BLE001
            return "default"

    def _last_session(self, event: AstrMessageEvent) -> str:
        return str((self._load_session_state().get(self._chat_key(event)) or {}).get("session_id") or "")

    def _read_session_events(self, session_id: str, limit: int = 40) -> list[dict]:
        """把某个会话的记录读出来（zstd 压缩的 jsonl），用于回看它到底干了什么。"""
        base = os.path.join(os.path.expanduser("~"), ".dsh", "sessions", _workspace_session_key(self._workspace()))
        path = os.path.join(base, session_id)
        if not os.path.isdir(path):
            return []
        blob = b""
        for root, _dirs, files in os.walk(path):
            for fn in files:
                if fn.endswith(".zstd") or fn.endswith(".jsonl"):
                    try:
                        blob += open(os.path.join(root, fn), "rb").read()
                    except Exception:  # noqa: BLE001
                        continue
        if not blob:
            return []
        text = ""
        try:
            if blob[:4] == b"\x28\xb5\x2f\xfd":
                import io

                import zstandard

                # 会话文件是逐次 flush 追加的多帧 zstd，必须跨帧读，单帧解只能拿到开头那行
                with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(blob), read_across_frames=True) as reader:
                    text = reader.read().decode("utf-8", "ignore")
            else:
                text = blob.decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            # 兜底：一帧一帧往前解，遇到坏帧就停
            try:
                import zstandard

                pos, chunks = 0, []
                while pos < len(blob):
                    obj = zstandard.ZstdDecompressor().decompressobj()
                    chunks.append(obj.decompress(blob[pos:]))
                    unused = getattr(obj, "unused_data", b"") or b""
                    if not unused:
                        break
                    pos = len(blob) - len(unused)
                text = b"".join(chunks).decode("utf-8", "ignore")
            except Exception:  # noqa: BLE001
                text = ""
        events = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
        return events[-limit:]

    @staticmethod
    def _format_trace(events: list[dict], limit: int = 40) -> str:
        """把会话记录排成"它调了什么、结果如何、它说了什么"。"""
        lines: list[str] = []
        for ev in events:
            kind = ev.get("type")
            data = ev.get("data") or {}
            if kind == "tool/call":
                args = data.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:  # noqa: BLE001
                        args = {"raw": args}
                lines.append("→ " + _summarize_tool_input(str(data.get("name") or "?"), args))
            elif kind == "tool/result":
                message = data.get("message") or {}
                text = ""
                for block in message.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = str(block.get("text") or "")
                        break
                bad = bool(message.get("isError")) or bool(data.get("error"))
                lines.append(("   ✗ " if bad else "   ✓ ") + " ".join(text.split())[:220])
            elif kind == "assistant/message":
                for block in (data.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                        lines.append("说：" + " ".join(str(block["text"]).split())[:160])
            elif kind == "user/message":
                for block in (data.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                        lines.append("派：" + " ".join(str(block["text"]).split())[:120])
        if not lines:
            return "这个会话里没读到工具调用记录"
        return chr(10).join(lines[-limit:])

    # ── WebUI 启停 ─────────────────────────────────────────────
    def _web_state_path(self) -> str:
        return os.path.join(self._data_dir, "dsh_web.json")

    def _load_web_state(self) -> dict:
        try:
            with open(self._web_state_path(), encoding="utf-8") as fh:
                return json.load(fh) or {}
        except Exception:
            return {}

    def _save_web_state(self, state: dict) -> None:
        try:
            with open(self._web_state_path(), "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DshBridge] 存 web 状态失败: %s", exc)

    @staticmethod
    def _port_alive(port: int) -> bool:
        import socket

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.5)
                return sock.connect_ex(("127.0.0.1", int(port))) == 0
        except Exception:  # noqa: BLE001
            return False

    async def _ensure_web(self) -> tuple[bool, str]:
        """确保 dsh web 在跑。返回 (成功?, 给用户看的话)。"""
        port = int(self._conf("web_port") or 3939)
        state = self._load_web_state()
        if state.get("url") and self._port_alive(port):
            return True, str(state["url"])
        if not self._conf("web_auto_start"):
            return False, f"dsh web 现在没在跑（端口 {port} 没人监听），想去插件配置里把 web_auto_start 打开我就能顺手起一个"
        ws = str(self._conf("web_workspace") or "").strip() or self._workspace()
        os.makedirs(ws, exist_ok=True)
        if os.name == "nt":
            argv = ["cmd", "/c", f"dsh web --port {port} --no-open"]
        else:
            argv = ["dsh", "web", "--port", str(port), "--no-open"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=ws, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"启动 dsh web 失败：{exc}"
        self._web_proc = proc  # 别让 gc 收掉
        url = ""
        tail: list[str] = []
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=max(1.0, deadline - time.time()))
            except asyncio.TimeoutError:
                break
            except Exception:  # noqa: BLE001
                break
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if text:
                tail.append(text)
                del tail[:-6]
            if "http://127.0.0.1" in text:
                part = text.split("http://", 1)[1].split()[0]
                url = "http://" + part
                break
        if not url:
            return False, "dsh web 起来了但没读到地址，输出：\n" + ("\n".join(tail) or "（空）")
        self._save_web_state(
            {
                "pid": proc.pid,
                "port": port,
                "url": url,
                "workspace": ws,
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        return True, url

    # ── 给模型用：LLM 工具 ─────────────────────────────────────
    @filter.llm_tool()
    async def dsh_task(
        self,
        event: AstrMessageEvent,
        task: str,
        timeout_seconds: int = 0,
        session_id: str = "",
        continue_last: bool = False,
    ):
        """
        把一条任务交给本机的 DeepSeek Harness 子代理去执行。它能读写工作区里的文件、运行命令、调用它自己安装的插件工具（含生图）。
        适合较重的活儿：查代码、批量改文件、生图、跑脚本。轻的一句话问答不要用这个。

        Args:
            task(string): 交给子代理的任务描述，越具体越好，可以带文件路径
            timeout_seconds(number): 最多等多少秒，0 表示用默认值
            session_id(string): 可选，填上次返回的会话 id 就能接着那段上下文继续
            continue_last(boolean): 可选，true 表示接这个会话上一次的 DSH 会话继续，适合多轮打磨同一个任务
        """
        if not self._conf("enabled"):
            return "DSH 桥被关掉了，先在插件配置里打开 enabled"
        if not self._allowed(event):
            return "这个账号没有调用子代理的权限"
        if not session_id and continue_last:
            session_id = self._last_session(event)
        reply = await self._run(task, timeout_seconds, session_id)
        match = re.search(r"session-[0-9a-f-]{8,}", reply)
        if match:
            self._remember_session(event, match.group(0))
        return reply

    # ── 给模型用：回看过程 ─────────────────────────────────────
    @filter.llm_tool()
    async def dsh_trace(self, event: AstrMessageEvent, session_id: str = "", limit: int = 40):
        """
        回看某个 DSH 会话里子代理到底跑了什么：工具调用、命令、执行结果、它说过的话。
        派单之后先用它自查结果，再决定是继续派第二轮还是汇报。

        Args:
            session_id(string): 会话 id，留空表示这个会话最近一次派出去的 DSH 会话
            limit(number): 最多回看多少条记录，默认 40
        """
        if not self._allowed(event):
            return "这个账号没有调用子代理的权限"
        sid = session_id.strip() or self._last_session(event)
        if not sid:
            return "还没有派过任务，先用 dsh_task 派一条"
        events = self._read_session_events(sid, int(limit) or 40)
        if not events:
            return f"没读到会话 {sid} 的记录"
        return f"会话 {sid} 的过程（最近的记录）：\n" + self._format_trace(events, int(limit) or 40)

    @filter.llm_tool()
    async def dsh_sessions(self, event: AstrMessageEvent, limit: int = 6):
        """
        列出当前工作区里最近的 DSH 会话，用来挑一个会话继续打磨或回看。

        Args:
            limit(number): 列出几个，默认 6
        """
        if not self._allowed(event):
            return "这个账号没有调用子代理的权限"
        items = self._recent_sessions(int(limit) or 6)
        if not items:
            return "还没有会话记录"
        current = self._last_session(event)
        rows = []
        for sid, ts in items:
            rows.append(f"{_human_time(ts)}  {sid}" + ("（本会话最近用的）" if sid == current else ""))
        return "当前工作区最近的 DSH 会话：\n" + chr(10).join(rows)

    # ── 给我自己看：把操作手册注入系统提示 ──────────────────────
    @filter.on_llm_request()
    async def inject_manual(self, event: AstrMessageEvent, req):
        """让主智能体按"计划→派单→自查→迭代→汇报"来用 DSH，而不是当传声筒。"""
        if not self._conf("inject_manual"):
            return
        if not self._allowed(event):
            return
        # 只有操作手模式开着才注入手册，平时我就是正常的洛水
        if not self._operator_on():
            return
        text = str(self._conf("manual_text") or "").strip() or MANUAL
        try:
            current = getattr(req, "system_prompt", "") or ""
            if "DSH 子代理操作手册" in current:
                return
            req.system_prompt = current + chr(10) + text
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DshBridge] 注入操作手册失败: %s", exc)

    # ── 给人用：命令 ───────────────────────────────────────────
    @filter.command("dsh")
    async def dsh_cmd(self, event: AstrMessageEvent, task: str = ""):
        """把后面的内容交给 DSH 子代理跑：/dsh 列出工作区里的文件"""
        if not self._conf("enabled"):
            yield event.plain_result("DSH 桥被关掉了，先在插件配置里打开 enabled")
            return
        if not self._allowed(event):
            yield event.plain_result("你没有调用子代理的权限")
            return
        if not task.strip():
            yield event.plain_result("用法：/dsh 任务内容")
            return
        yield event.plain_result(f"收到，派给子代理了，工作区 {self._workspace()}")
        reply = await self._run(task)
        yield event.plain_result(reply)

    @filter.command("dsh续")
    async def dsh_resume_cmd(self, event: AstrMessageEvent, payload: str = ""):
        """接着某次会话继续跑：/dsh续 会话id 任务内容"""
        if not self._conf("enabled"):
            yield event.plain_result("DSH 桥被关掉了，先在插件配置里打开 enabled")
            return
        if not self._allowed(event):
            yield event.plain_result("你没有调用子代理的权限")
            return
        parts = (payload or "").strip().split(maxsplit=1)
        if len(parts) < 2 or not parts[0].startswith("session-"):
            yield event.plain_result("用法：/dsh续 session-xxxx 任务内容（会话 id 可以用 /dsh会话 查）")
            return
        session_id, task = parts[0], parts[1]
        yield event.plain_result(f"接着 {session_id} 跑，工作区 {self._workspace()}")
        yield event.plain_result(await self._run(task, session_id=session_id))

    @filter.command("dsh会话")
    async def dsh_sessions_cmd(self, event: AstrMessageEvent):
        """看看当前工作区里最近的几个会话，拿 id 用于 /dsh续"""
        if not self._allowed(event):
            yield event.plain_result("你没有调用子代理的权限")
            return
        items = self._recent_sessions(8)
        if not items:
            yield event.plain_result(f"工作区 {self._workspace()} 里还没有会话记录")
            return
        lines = [f"工作区 {self._workspace()} 最近的会话："]
        for sid, ts in items:
            lines.append(f"{_human_time(ts)}  {sid}")
        lines.append("接着跑：/dsh续 会话id 任务内容")
        yield event.plain_result(chr(10).join(lines))

    @filter.command("操作手")
    async def operator_cmd(self, event: AstrMessageEvent, action: str = ""):
        """切换操作手模式：/操作手 开 | /操作手 关 | /操作手 状态"""
        if not self._allowed(event):
            yield event.plain_result("你没有切这个模式的权限")
            return
        word = (action or "").strip().lower()
        if word in ("开", "on", "1", "true", "开启"):
            err = self._set_operator(True, event)
            yield event.plain_result(err or "操作手模式已开：接下来我先判断要不要动用 Harness，用的话我当用户去派单、自查、迭代，跑完你说一声 /操作手 关")
            return
        if word in ("关", "off", "0", "false", "关闭"):
            err = self._set_operator(False, event)
            yield event.plain_result(err or "操作手模式已关，回到平时的聊天；要临时派活还是可以发 /dsh 任务")
            return
        state = self._operator_state()
        now = "开着" if self._operator_on() else "关着"
        extra = ""
        if state.get("at"):
            extra = f"（{state.get('by')} 在 {state.get('at')} 切的）"
        yield event.plain_result(f"操作手模式现在{now}{extra}\n用法：/操作手 开 或 /操作手 关")

    @filter.command("dsh过程")
    async def dsh_trace_cmd(self, event: AstrMessageEvent, payload: str = ""):
        """看子代理刚跑那轮到底做了什么：/dsh过程 [会话id]"""
        if not self._allowed(event):
            yield event.plain_result("你没有调用子代理的权限")
            return
        sid = payload.strip() or self._last_session(event)
        if not sid:
            yield event.plain_result("还没有派过任务")
            return
        events = self._read_session_events(sid, 40)
        yield event.plain_result(self._format_trace(events, 40) if events else f"没读到会话 {sid}")

    @filter.command("dsh网页")
    async def dsh_web_cmd(self, event: AstrMessageEvent):
        """打开 dsh web 的界面（没开就顺手起一个）"""
        if not self._allowed(event):
            yield event.plain_result("你没有调用子代理的权限")
            return
        yield event.plain_result("看一下 web 在不在跑，不在我就起一个")
        ok, info = await self._ensure_web()
        if ok:
            yield event.plain_result("界面地址（令牌在链接里，直接点开）：\n" + info)
        else:
            yield event.plain_result(info)

    @filter.command("dsh停web")
    async def dsh_web_stop_cmd(self, event: AstrMessageEvent):
        """关掉 dsh web"""
        if not self._allowed(event):
            yield event.plain_result("你没有调用子代理的权限")
            return
        state = self._load_web_state()
        pid = state.get("pid")
        if not pid:
            yield event.plain_result("没有我起过的 web 进程记录")
            return
        try:
            import subprocess

            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=15)
            self._save_web_state({})
            yield event.plain_result(f"已经把 web 进程 {pid} 关了")
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"关闭失败：{exc}")

    @filter.command("dsh装dsh")
    async def dsh_install_cmd(self, event: AstrMessageEvent):
        """一键装一份便携 DSH（没装过 DSH 的机器用）"""
        if not self._allowed(event):
            yield event.plain_result("你没有调用子代理的权限")
            return
        target = self._install_dir()
        yield event.plain_result("开始装便携 DSH，装到 " + target + "\n可能要几分钟到十几分钟，装完我告诉你")
        ok, info = await asyncio.to_thread(install_portable_dsh, target, str(self._conf("dsh_version") or "latest"))
        yield event.plain_result(("装好了" if ok else "没装成") + "：" + info)

    @filter.command("dsh体检")
    async def dsh_doctor_cmd(self, event: AstrMessageEvent):
        """自检：这台机器上跑不跑得起来（换电脑装完先发这个）"""
        yield event.plain_result("体检中，稍等十几秒")
        probe = await asyncio.to_thread(
            probe_environment,
            str(self._conf("dsh_cmd") or ""),
            str(self._conf("profile")),
            self._workspace(),
        )
        key, source = self._api_key()
        probe["api_key_source"] = source or "没找到（可填一个，或在本机登录 DSH）"
        probe["portable_target"] = self._install_dir()
        if self._conf("auto_fix_acl") and os.name == "nt":
            ok, detail = await asyncio.to_thread(ensure_workspace_acl, self._workspace())
            probe["acl"] = f"{'已授予当前用户完全控制' if ok else '授权失败'}（{detail}）"
        yield event.plain_result(format_probe(probe))

    @filter.command("dsh状态")
    async def dsh_status(self, event: AstrMessageEvent):
        """看一眼桥的状态"""
        busy = "有任务在跑" if self._lock.locked() else "空闲"
        yield event.plain_result(
            f"DSH 桥：{'开启' if self._conf('enabled') else '关闭'} / {busy}\n"
            f"profile：{self._conf('profile')}\n"
            f"工作区：{self._workspace()}\n"
            f"超时：{self._conf('timeout_seconds')}s\n"
            f"白名单：{self._conf('allow_user_ids') or '（所有人）'}"
        )
