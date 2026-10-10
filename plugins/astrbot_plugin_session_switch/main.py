"""会话开关插件。

用指令把某个会话的 AI 关掉 / 打开：关掉之后 AstrBot 不会再为这个会话请求大模型，
也就不会再回复、不会再烧 token。开和关都能定时，到点自动切换。

底子用的是 AstrBot 自带的会话服务开关
（astrbot.core.star.session_llm_manager.SessionServiceManager），
所以面板里的会话状态也会跟着变；插件自己只额外记一份「什么时候切换」。

权限：只有管理员（配置里的 admins_id、本插件白名单、群主）能用，
普通成员发这个指令只会收到一句「没权限」。

指令：
- /会话 关               关掉本会话（默认到今天结束）
- /会话 关 2h            关 2 小时，也支持 30m / 1d / 今天 / 永久
- /会话 关 到 8:00       关到明天早上八点，也可以写 明天8点 / 8点半
- /会话 开 2h            打开本会话，2 小时后自动关上（到点再手动开）
- /会话 关 群 691314843  关指定群
- /会话 开 群 691314843 30m
- /会话 状态             看本会话是开还是关
- /会话 列表             看所有还挂着定时切换的会话
- /会话 全开             一次性全开
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
import time
import traceback
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.star.session_llm_manager import SessionServiceManager
except Exception:  # pragma: no cover
    SessionServiceManager = None

PLUGIN_NAME = "astrbot_plugin_session_switch"
PLUGIN_VERSION = "v1.2.0"

COMMAND_NAMES = {"会话", "session", "session_switch", "会话开关", "开关"}

USAGE = (
    "会话开关用法（管理员指令）\n"
    "/会话 关                关掉本会话，默认关到今天结束\n"
    "/会话 关 2h             关 2 小时，也可以 30m / 1d / 今天 / 永久\n"
    "/会话 关 到 8:00        关到早上八点，也认 明天8点 / 8点半\n"
    "/会话 开                打开本会话，参数写法和关一样\n"
    "/会话 开 2h             现在打开，2 小时后自动关上\n"
    "/会话 关 群 691314843   关掉指定群\n"
    "/会话 关 私聊 3190568934\n"
    "/会话 关 default:GroupMessage:691314843\n"
    "/会话 状态              看本会话是开还是关\n"
    "/会话 列表              看所有挂着定时切换的会话\n"
    "/会话 全开              一次性全部都开\n"
    "/会话 帮助              看这句"
)

DEFAULTS: dict[str, Any] = {
    "admin_only": True,
    "allowed_users": ["3190568934"],
    "default_duration": "today",
    "check_interval": 30,
    "also_disable_tts": False,
}

NO_PERMISSION = "这个要管理员才能用，普通成员碰不了"

CONFIG_GROUPS = ("switch_settings", None)

_DUR_RE = re.compile(
    r"^(\d+)\s*(s|sec|秒|m|min|分|分钟|h|hour|小时|d|day|天)?$",
    re.IGNORECASE,
)

_CLOCK_RE = re.compile(
    r"^(\d{1,2})(?:[:：](\d{1,2})|[点時时](?:(半)|(\d{1,2})\s*分?)?)$",
)

_TIME_PREFIXES = ("直到", "until", "到", "at", "在")
_TOMORROW_PREFIXES = ("明天", "明日", "tomorrow")


# --------------------------------------------------------------------- 工具函数
def _data_root() -> str:
    candidates: list[str] = []
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        candidates.append(get_astrbot_data_path())
    except Exception:
        pass
    try:
        root = os.environ.get("ASTRBOT_ROOT")
        if root:
            candidates.append(os.path.join(root, "data"))
    except Exception:
        pass
    try:
        import astrbot

        pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(astrbot.__file__)))
        candidates.append(os.path.join(pkg_root, "data"))
    except Exception:
        pass
    candidates.append(os.path.join(os.getcwd(), "data"))
    for path in candidates:
        if path and os.path.isdir(path):
            return os.path.realpath(path)
    return os.path.realpath(candidates[0]) if candidates else "data"


def _store_path() -> str:
    return os.path.join(_data_root(), "plugin_data", PLUGIN_NAME, "sessions.json")


def _clean_text(raw: str) -> str:
    """剥掉 AstrBot 挂在消息尾巴上的各种标记。"""
    text = str(raw or "").strip()
    text = re.sub(
        r"\[\s*msg[\s_-]*id\s*[:：]\s*\d+\s*\]",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"msg[\s_-]*id\s*[:：]\s*\d+",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    return text.strip()


def _today_end() -> float:
    now = datetime.datetime.now()
    tomorrow = (now + datetime.timedelta(days=1)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    return tomorrow.timestamp()


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "永久（要手动开）"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _parse_duration(word: str) -> tuple[float | None, str] | None:
    """解析时长写法。

    返回 (切换时间戳, 说明文字)；时间戳为 None 表示永久。
    返回 None 表示这个词根本不是时长。
    """
    w = (word or "").strip().lower()
    if not w:
        return None
    if w in {"今天", "今日", "today", "今天结束", "到明天"}:
        return _today_end(), "今天结束"
    if w in {"永久", "永远", "一直", "forever", "always"}:
        return None, "永久"
    m = _DUR_RE.match(w)
    if not m:
        return None
    digits = m.group(1)
    unit = (m.group(2) or "").lower()
    if not unit and len(digits) >= 6:
        # 这么长的纯数字多半是群号，不是分钟
        return None
    num = int(digits)
    if unit in {"s", "sec", "秒"}:
        secs, label = num, f"{num} 秒"
    elif unit in {"m", "min", "分", "分钟"}:
        secs, label = num * 60, f"{num} 分钟"
    elif unit in {"h", "hour", "小时"}:
        secs, label = num * 3600, f"{num} 小时"
    elif unit:
        secs, label = num * 86400, f"{num} 天"
    else:
        secs, label = num * 60, f"{num} 分钟"
    return time.time() + secs, f"{label}后"


def _parse_clock(word: str) -> tuple[int, int] | None:
    """解析 8:00 / 8点 / 8点半 / 20点30分。"""
    m = _CLOCK_RE.match((word or "").strip())
    if not m:
        return None
    hh = int(m.group(1))
    if m.group(2) is not None:
        mm = int(m.group(2))
    elif m.group(3):
        mm = 30
    elif m.group(4):
        mm = int(m.group(4))
    else:
        mm = 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm


def _parse_when(word: str) -> tuple[float | None, str] | None:
    """解析切换时间：时长、钟点、明天几点、永久。

    返回 (切换时间戳, 说明)；时间戳为 None 表示永久。返回 None 表示不是时间。
    """
    w = _clean_text(word).strip()
    if not w:
        return None
    low = w.lower()

    for pre in _TIME_PREFIXES:
        if low.startswith(pre) and len(w) > len(pre):
            got = _parse_when(w[len(pre):].strip())
            if got is not None:
                return got

    for pre in _TOMORROW_PREFIXES:
        if low.startswith(pre) and len(w) > len(pre):
            got = _parse_when(w[len(pre):].strip())
            if got is not None:
                ts, label = got
                if ts is None:
                    return got
                return ts + 86400, "明天 " + label.replace("明天 ", "")

    clock = _parse_clock(w)
    if clock:
        hh, mm = clock
        now = datetime.datetime.now()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        label = f"{hh:02d}:{mm:02d}"
        if target <= now:
            target += datetime.timedelta(days=1)
            label = "明天 " + label
        return target.timestamp(), label

    return _parse_duration(w)


@register(
    PLUGIN_NAME,
    "洛水",
    "用指令开关会话：关掉后该会话不再请求大模型，也不再回复；开关都支持定时",
    PLUGIN_VERSION,
)
class SessionSwitchPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.store_path = _store_path()
        self.sessions: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task | None = None
        self._load()
        logger.info(f"[会话开关] 已加载，记录文件：{self.store_path}")

    # ------------------------------------------------------------------ 配置
    def _cfg(self, key: str) -> Any:
        for group in CONFIG_GROUPS:
            if group is None:
                holder = self.config
            else:
                holder = self.config.get(group) if isinstance(self.config, dict) else None
            if isinstance(holder, dict) and holder.get(key) is not None:
                return holder[key]
        return DEFAULTS.get(key)

    def _cfg_int(self, key: str) -> int:
        try:
            return int(self._cfg(key))
        except (TypeError, ValueError):
            return int(DEFAULTS.get(key, 0))

    def _cfg_bool(self, key: str) -> bool:
        value = self._cfg(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return False

    # ------------------------------------------------------------------ 权限
    def _allowed_ids(self) -> set[str]:
        raw = self._cfg("allowed_users")
        if isinstance(raw, str):
            raw = re.split(r"[,\s;，；]+", raw)
        if not isinstance(raw, (list, tuple, set)):
            return set()
        return {str(item).strip() for item in raw if str(item).strip()}

    def _is_operator(self, event: AstrMessageEvent) -> bool:
        """管理员、白名单 QQ、群主才算数。"""
        if not self._cfg_bool("admin_only"):
            return True
        sender = ""
        try:
            sender = str(event.get_sender_id())
        except Exception:
            sender = str(getattr(event, "sender_id", "") or "")
        if sender and sender in self._allowed_ids():
            return True
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        return str(getattr(event, "role", "") or "") in {"admin", "owner"}

    # ------------------------------------------------------------------ 存储
    def _load(self) -> None:
        try:
            if os.path.exists(self.store_path):
                with open(self.store_path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self.sessions = {
                        str(k): v for k, v in data.items() if isinstance(v, dict)
                    }
        except Exception:
            logger.error(f"[会话开关] 读取记录失败：{traceback.format_exc()}")
            self.sessions = {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.store_path), exist_ok=True)
            tmp = self.store_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.sessions, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.store_path)
        except Exception:
            logger.error(f"[会话开关] 保存记录失败：{traceback.format_exc()}")

    def _rec_state(self, umo: str) -> str:
        rec = self.sessions.get(umo)
        if not rec:
            return "open"
        return "open" if rec.get("state") == "open" else "closed"

    def _is_closed(self, umo: str) -> bool:
        rec = self.sessions.get(umo)
        if not rec:
            return False
        if rec.get("state") == "open":
            return False
        until = rec.get("until")
        if until and time.time() >= float(until):
            return False
        return True

    def _due_records(self) -> list[tuple[str, dict[str, Any]]]:
        now = time.time()
        return [
            (umo, rec)
            for umo, rec in list(self.sessions.items())
            if rec.get("until") and now >= float(rec["until"])
        ]

    # ------------------------------------------------------------------ 开关
    async def _call_manager(self, umo: str, status: bool) -> None:
        if SessionServiceManager is None:
            return
        try:
            await SessionServiceManager.set_llm_status_for_session(umo, status)
            if self._cfg_bool("also_disable_tts"):
                await SessionServiceManager.set_tts_status_for_session(umo, status)
        except Exception:
            logger.error(f"[会话开关] 切换会话失败：{traceback.format_exc()}")

    async def _apply_close(self, umo: str, until: float | None, label: str) -> None:
        self.sessions[umo] = {
            "state": "closed",
            "until": until,
            "label": label,
            "created": time.time(),
        }
        self._save()
        await self._call_manager(umo, False)

    async def _apply_open(
        self,
        umo: str,
        until: float | None = None,
        label: str = "",
    ) -> None:
        if until:
            self.sessions[umo] = {
                "state": "open",
                "until": until,
                "label": label,
                "created": time.time(),
            }
        else:
            self.sessions.pop(umo, None)
        self._save()
        await self._call_manager(umo, True)

    async def _run_due(self) -> list[str]:
        """处理到点的记录，返回被处理过的会话号。"""
        handled: list[str] = []
        for umo, rec in self._due_records():
            if rec.get("state") == "open":
                await self._apply_close(umo, None, "定时关闭")
                logger.info(f"[会话开关] {umo} 到点自动关闭")
            else:
                await self._apply_open(umo)
                logger.info(f"[会话开关] {umo} 到点自动打开")
            handled.append(umo)
        return handled

    async def _watch_loop(self) -> None:
        interval = max(self._cfg_int("check_interval"), 5)
        while True:
            await asyncio.sleep(interval)
            try:
                await self._run_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error(f"[会话开关] 定时检查出错：{traceback.format_exc()}")

    def _ensure_task(self) -> None:
        try:
            if self._task is None or self._task.done():
                self._task = asyncio.get_running_loop().create_task(self._watch_loop())
        except RuntimeError:
            pass

    # ------------------------------------------------------------------ 指令
    def _resolve_target(self, event: AstrMessageEvent, kind: str, num: str) -> str:
        umo = str(getattr(event, "unified_msg_origin", "") or "default:Unknown:0")
        platform = umo.split(":", 1)[0] if ":" in umo else "default"
        middle = "GroupMessage" if kind == "group" else "FriendMessage"
        return f"{platform}:{middle}:{num.strip()}"

    def _describe(self, umo: str) -> str:
        rec = self.sessions.get(umo)
        if not rec:
            return f"{umo}\n现在是开着的"
        if rec.get("state") == "open":
            return (
                f"{umo}\n现在是开着的\n"
                f"定时关闭：{_fmt_ts(rec.get('until'))}（{rec.get('label') or ''}）"
            )
        if self._is_closed(umo):
            return f"{umo}\n现在是关着的\n恢复时间：{_fmt_ts(rec.get('until'))}"
        return f"{umo}\n现在是开着的"

    @filter.command("会话", alias={"session", "会话开关", "开关"})
    async def session_cmd(self, event: AstrMessageEvent):
        try:
            self._ensure_task()
            if not self._is_operator(event):
                event.set_extra("session_switch_allow", True)
                yield event.plain_result(NO_PERMISSION)
                event.stop_event()
                return
            # 打了这个标记，本轮结果不会被下面的兜底清掉
            event.set_extra("session_switch_allow", True)
            text = _clean_text(getattr(event, "message_str", ""))
            plain = re.sub(r"^[/／#＃!！\s]+", "", text).strip()
            parts = plain.split()
            if parts and parts[0].lower() in COMMAND_NAMES:
                parts = parts[1:]

            action = parts[0] if parts else "状态"
            rest = parts[1:]

            if action in {"帮助", "用法", "help", "?", "？", "说明"}:
                yield event.plain_result(USAGE)
                return

            if action in {"全开", "全部开", "全打开", "清空", "清除", "清"}:
                targets = list(self.sessions.keys())
                for umo in targets:
                    await self._apply_open(umo)
                if not targets:
                    yield event.plain_result("现在没有挂着定时切换的会话")
                else:
                    yield event.plain_result(
                        f"已经全部打开，清了 {len(targets)} 条记录\n"
                        + "\n".join(targets[:20]),
                    )
                return

            if action in {"列表", "list", "全部", "所有"}:
                await self._run_due()
                if not self.sessions:
                    yield event.plain_result("现在没有挂着定时切换的会话")
                    return
                lines = [f"还挂着定时切换的会话（{len(self.sessions)}）"]
                for idx, (umo, rec) in enumerate(self.sessions.items(), 1):
                    if rec.get("state") == "open":
                        lines.append(
                            f"{idx}. {umo}\n   现在：开着\n"
                            f"   自动关闭：{_fmt_ts(rec.get('until'))}",
                        )
                    else:
                        lines.append(
                            f"{idx}. {umo}\n   现在：关着\n"
                            f"   自动打开：{_fmt_ts(rec.get('until'))}",
                        )
                yield event.plain_result("\n".join(lines))
                return

            close = action in {"关", "关闭", "关掉", "停", "停用", "禁用", "off"}
            open_ = action in {"开", "开启", "打开", "启用", "恢复", "on"}
            status = action in {"状态", "status", "查", "查询", ""}

            if not (close or open_ or status):
                yield event.plain_result(f"没看懂「{action}」\n" + USAGE)
                return

            # 解析目标与时间
            target = ""
            when_word = ""
            idx = 0
            while idx < len(rest):
                word = rest[idx]
                low = word.lower()
                if low in {"群", "群聊", "group"} and idx + 1 < len(rest):
                    target = self._resolve_target(event, "group", rest[idx + 1])
                    idx += 2
                    continue
                if low in {"私聊", "好友", "friend", "qq"} and idx + 1 < len(rest):
                    target = self._resolve_target(event, "friend", rest[idx + 1])
                    idx += 2
                    continue
                if _parse_when(word) is not None:
                    when_word = word
                    idx += 1
                    continue
                if ":" in word:
                    target = word
                    idx += 1
                    continue
                if re.fullmatch(r"\d{5,}", word) and not target:
                    target = self._resolve_target(event, "group", word)
                    idx += 1
                    continue
                idx += 1

            umo = target or str(getattr(event, "unified_msg_origin", "") or "")
            if not umo:
                yield event.plain_result("拿不到会话号，搞不了")
                return

            if status:
                yield event.plain_result(self._describe(umo))
                return

            if open_:
                until: float | None = None
                label = ""
                if when_word:
                    parsed = _parse_when(when_word)
                    if parsed is not None:
                        until, label = parsed
                if not until:
                    await self._apply_open(umo)
                    yield event.plain_result(f"已打开，一直开着\n{umo}")
                    return
                await self._apply_open(umo, until, label)
                yield event.plain_result(
                    f"已打开\n{umo}\n"
                    f"会在 {_fmt_ts(until)}（{label}）自动关上，"
                    f"到时候想接着用就再说一声\n想取消这个定时：/会话 开",
                )
                return

            # 关闭
            if when_word:
                parsed = _parse_when(when_word)
                until, label = parsed if parsed else (None, "永久")
            else:
                default_duration = str(self._cfg("default_duration") or "today").lower()
                if default_duration in {"forever", "永久"}:
                    until, label = None, "永久"
                else:
                    until, label = _today_end(), "今天结束"
            await self._apply_close(umo, until, label)
            when = f"{_fmt_ts(until)}（{label}）" if until else "永久，想用再开"
            yield event.plain_result(
                f"已关闭\n{umo}\n恢复时间：{when}\n想提前开：/会话 开",
            )
        except Exception:
            logger.error(f"[会话开关] 处理失败：{traceback.format_exc()}")
            yield event.plain_result("会话开关出错了，看一眼日志")

    # ------------------------------------------------------------------ 兜底拦截
    @filter.on_waiting_llm_request()
    async def block_waiting_llm(self, event: AstrMessageEvent, *args, **kwargs):
        """会话关着的时候，把 LLM 请求掐在排队前。"""
        self._ensure_task()
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if umo and self._is_closed(umo):
            event.stop_event()

    @filter.on_llm_request()
    async def block_llm_request(self, event: AstrMessageEvent, *args, **kwargs):
        """兜住插件自己发起的 LLM 请求。"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if umo and self._is_closed(umo):
            event.stop_event()

    @filter.on_decorating_result()
    async def block_send(self, event: AstrMessageEvent, *args, **kwargs):
        """会话关着的时候，已经有内容了也别发出去。"""
        if event.get_extra("session_switch_allow"):
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo or not self._is_closed(umo):
            return
        result = event.get_result()
        if result is not None and result.chain:
            result.chain = []

    async def terminate(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None
        try:
            await self._run_due()
        except Exception:
            pass
