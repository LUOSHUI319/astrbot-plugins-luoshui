"""新人进群欢迎插件。

aiocqhttp 适配器会把 OneBot 的 notice 事件转成一条 GROUP_MESSAGE 事件，
原始字段留在 event.message_obj.raw_message 里，所以这里从那个字段判断
notice_type == "group_increase"，命中就发一条欢迎，提醒先看群文件、
群公告和群相册。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_group_welcome"
PLUGIN_VERSION = "v1.0.0"

DEFAULT_TEXT = (
    "欢迎 {nickname} 进群\n"
    "先花两分钟把群文件、群公告和群相册翻一遍，"
    "新手教程和常见问题的答案都在里面，照着走一遍比直接提问快得多"
)

INCREASE_SUBTYPES = {"approve", "invite"}


@register(
    PLUGIN_NAME,
    "洛水",
    "新人进群自动欢迎，提醒先看群文件、群公告和群相册",
    PLUGIN_VERSION,
)
class GroupWelcomePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self._recent: dict[tuple[str, str], float] = {}

    # ---------- 配置 ----------

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            value = default
        return default if value is None else value

    @property
    def enabled(self) -> bool:
        return bool(self._cfg("enabled", True))

    @property
    def welcome_text(self) -> str:
        text = str(self._cfg("welcome_text", "") or "").strip()
        return text or DEFAULT_TEXT

    def _allow_group(self, group_id: str) -> bool:
        raw = str(self._cfg("group_whitelist", "") or "")
        raw = raw.replace("，", "\n").replace(",", "\n")
        allowed = {item.strip() for item in raw.split() if item.strip()}
        return not allowed or group_id in allowed

    # ---------- 工具 ----------

    @staticmethod
    def _raw_message(event: AstrMessageEvent) -> Any:
        obj = getattr(event, "message_obj", None)
        raw = getattr(obj, "raw_message", None)
        if raw is None:
            raw = getattr(event, "raw_message", None)
        return raw

    @staticmethod
    def _call(event: AstrMessageEvent, name: str, default: str = "") -> str:
        func = getattr(event, name, None)
        if not callable(func):
            return default
        try:
            value = func()
        except Exception:
            return default
        return default if value is None else str(value)

    def _seen(self, key: tuple[str, str]) -> bool:
        """同一个（群, 人）两分钟内只欢迎一次，防止重复事件刷屏。"""
        now = time.time()
        for saved, ts in list(self._recent.items()):
            if now - ts > 120:
                self._recent.pop(saved, None)
        if key in self._recent:
            return True
        self._recent[key] = now
        return False

    async def _nickname(self, event: AstrMessageEvent, group_id: str, user_id: str) -> str:
        bot = getattr(event, "bot", None)
        call = getattr(bot, "call_action", None)
        if callable(call):
            try:
                info = await call(
                    "get_group_member_info",
                    group_id=int(group_id),
                    user_id=int(user_id),
                    no_cache=True,
                )
                if isinstance(info, dict):
                    name = str(info.get("card") or info.get("nickname") or "").strip()
                    if name:
                        return name
            except Exception as exc:
                logger.debug(f"[group_welcome] 取昵称失败：{exc}")
        return "新朋友"

    # ---------- 主逻辑 ----------

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_increase(self, event: AstrMessageEvent):
        if not self.enabled:
            return

        raw = self._raw_message(event)
        getter = getattr(raw, "get", None)
        if not callable(getter):
            return

        try:
            if str(getter("post_type") or "").lower() != "notice":
                return
            if str(getter("notice_type") or "").lower() != "group_increase":
                return
            sub_type = str(getter("sub_type") or "").lower()
            group_id = str(getter("group_id") or "") or self._call(event, "get_group_id")
            user_id = str(getter("user_id") or "")
        except Exception:
            return

        if sub_type and sub_type not in INCREASE_SUBTYPES:
            return

        self_id = self._call(event, "get_self_id")
        if not group_id or not user_id:
            return
        if self_id and user_id == self_id:
            return
        if not self._allow_group(group_id):
            return
        if self._seen((group_id, user_id)):
            return

        nickname = await self._nickname(event, group_id, user_id)
        try:
            text = self.welcome_text.format(nickname=nickname)
        except Exception:
            text = DEFAULT_TEXT.format(nickname=nickname)

        try:
            await self.context.send_message(
                event.unified_msg_origin, MessageChain([Plain(text)])
            )
            logger.info(f"[group_welcome] 已欢迎 {user_id} 加入群 {group_id}")
        except Exception as exc:
            logger.warning(f"[group_welcome] 发送欢迎消息失败：{exc}")

    async def terminate(self):
        self._recent.clear()
