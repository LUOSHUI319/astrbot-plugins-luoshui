"""随机延时发送插件。

需求：机器人每准备好一条要发出去的回复，先不发送，先在 min~max 之间随机抽一个
秒数，抽到多少就等多少秒再发。多条消息各抽各的，互不影响。

实现思路：
在「发送消息前」钩子（on_decorating_result）里，把当前这条消息事件对象
（AstrMessageEvent）的 send 方法替换成一个带随机延时的包装函数。
之后框架自己走发送流程时，不管是整条回复链、还是分段回复拆出来的每一段，
只要调用 event.send(...)，都会先抽一次随机延时再真正发出去。

为什么用替换实例方法而不是直接 sleep：
- 事件对象是每条消息独享的，替换实例属性不会污染别的会话；
- 分段回复、多段消息在框架里是多次 event.send 调用，所以天然做到「一条一抽」；
- 直接 sleep 只会把整条回复整体推后一次，多条消息之间的间隔就没了。

范围：默认只对 aiocqhttp 生效，私聊群聊都能开，可以配置。
不生效的：流式输出（streaming）、以及 tool 里 send_message_to_user 那种
绕过本事件对象直接下发的消息。
"""

from __future__ import annotations

import asyncio
import random
import traceback
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_reply_delay"
PLUGIN_VERSION = "v1.0.0"

# 站在所有清理类钩子之后（优先级越小越靠后执行），确保我们拿到的是最终消息链，
# 也确保我们对 event.send 的替换不会被后续钩子重置。
HOOK_PRIORITY = -100000

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "min_delay": 5,
    "max_delay": 20,
    "enable_private": True,
    "enable_group": True,
    "platforms": ["aiocqhttp"],
    "only_llm_result": False,
    "debug_log": False,
}

CONFIG_GROUPS = ("delay_settings", "scope_settings", "debug_settings")

PATCH_FLAG = "_reply_delay_patched"


@register(
    PLUGIN_NAME,
    "洛水",
    "每条回复先随机抽一个秒数，抽到多少就延时多少秒再发送",
    PLUGIN_VERSION,
)
class ReplyDelayPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        logger.info(
            f"[回复延时] 已加载，延时区间={self._cfg_int('min_delay', 5)}"
            f"~{self._cfg_int('max_delay', 20)}s，平台={self._cfg_list('platforms')}",
        )

    # ------------------------------------------------------------------ 配置
    def _cfg(self, key: str) -> Any:
        for group in (*CONFIG_GROUPS, None):
            holder = self.config if group is None else self.config.get(group)
            if isinstance(holder, dict) and key in holder and holder[key] is not None:
                return holder[key]
        return DEFAULTS.get(key)

    def _cfg_bool(self, key: str, default: bool = False) -> bool:
        value = self._cfg(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return default

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self._cfg(key))
        except (TypeError, ValueError):
            return default

    def _cfg_list(self, key: str) -> list[str]:
        value = self._cfg(key)
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    # ------------------------------------------------------------------ 主逻辑
    @filter.on_decorating_result(priority=HOOK_PRIORITY)
    async def patch_send_delay(self, event: AstrMessageEvent, *args, **kwargs):
        try:
            await self._patch(event)
        except Exception:
            logger.error(f"[回复延时] 处理失败: {traceback.format_exc()}")

    async def _patch(self, event: AstrMessageEvent) -> None:
        if not self._cfg_bool("enabled", True):
            return

        result = event.get_result()
        if result is None or not list(getattr(result, "chain", []) or []):
            return

        # 流式输出交给平台适配器自己处理，这里不碰
        content_type = str(getattr(result, "result_content_type", "") or "")
        if "STREAM" in content_type.upper():
            return

        if self._cfg_bool("only_llm_result", False):
            try:
                if not result.is_llm_result():
                    return
            except Exception:
                pass

        platform = ""
        try:
            platform = str(event.get_platform_name() or "")
        except Exception:
            platform = ""
        allowed_platforms = self._cfg_list("platforms")
        if allowed_platforms and platform not in allowed_platforms:
            return

        try:
            is_private = bool(event.is_private_chat())
        except Exception:
            is_private = False
        if is_private and not self._cfg_bool("enable_private", True):
            return
        if not is_private and not self._cfg_bool("enable_group", True):
            return

        if event.get_extra(PATCH_FLAG, False):
            return
        event.set_extra(PATCH_FLAG, True)

        min_delay = max(self._cfg_int("min_delay", 5), 0)
        max_delay = max(self._cfg_int("max_delay", 20), min_delay)

        original_send = event.send
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        debug = self._cfg_bool("debug_log", False)

        async def delayed_send(*args, **kwargs):
            delay = (
                random.randint(min_delay, max_delay)
                if max_delay > min_delay
                else min_delay
            )
            if debug:
                logger.info(f"[回复延时] {umo or 'unknown'} 抽到 {delay}s，延时后发送")
            if delay > 0:
                await asyncio.sleep(delay)
            return await original_send(*args, **kwargs)

        event.send = delayed_send  # type: ignore[method-assign]

    async def terminate(self):
        pass
