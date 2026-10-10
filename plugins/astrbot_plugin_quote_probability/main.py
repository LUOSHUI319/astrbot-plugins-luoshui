"""按概率给机器人回复加上引用（Reply）的 AstrBot 插件。

用法：
1. 在 WebUI 把 AstrBot 全局配置 platform_settings.reply_with_quote 关掉，
   否则框架会在发送前无条件引用一次，概率控制会被覆盖。
2. 本插件默认引用概率 0.1，只在 aiocqhttp（OneBot）生效，可在配置里改。
"""

from __future__ import annotations

import random
import time
import traceback
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import Image, Plain, Record, Reply

PLUGIN_NAME = "astrbot_plugin_quote_probability"
PLUGIN_VERSION = "1.0.0"

# 发送前钩子优先级：站在 private_companion 清理类钩子（-1000 及更小）之后，
# 避免刚加上的引用被后续去重、去引用逻辑摘掉。
DECORATE_PRIORITY = -5000

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "probability": 0.1,
    "cooldown_seconds": 0,
    "only_llm_result": True,
    "min_text_chars": 0,
    "enable_private": True,
    "enable_group": True,
    "platforms": ["aiocqhttp"],
    "debug_log": False,
}

CONFIG_GROUPS = ("quote_settings", "scope_settings", "debug_settings")


@register(PLUGIN_NAME, "洛水", "按概率给机器人回复加上引用", PLUGIN_VERSION)
class QuoteProbabilityPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._last_quote_ts: dict[str, float] = {}
        self._warned_core_quote = False
        self._warn_core_quote_once()
        logger.info(
            f"[引用概率] 已加载，概率={self._cfg_float('probability', 0.1)} "
            f"平台={self._cfg('platforms')}"
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

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self._cfg(key))
        except (TypeError, ValueError):
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

    def _warn_core_quote_once(self) -> None:
        """框架的全局引用开关会无条件加引用，和概率控制冲突，这里只提醒一次。"""
        if self._warned_core_quote:
            return
        try:
            cfg = self.context.get_config()
            platform_settings = cfg.get("platform_settings") if hasattr(cfg, "get") else None
            if isinstance(platform_settings, dict) and platform_settings.get("reply_with_quote"):
                self._warned_core_quote = True
                logger.warning(
                    "[引用概率] 检测到全局配置 platform_settings.reply_with_quote 仍然开启，"
                    "框架会在发送前无条件引用原消息，概率会失效。请在 WebUI 关闭"
                    "「回复时引用原消息」后再使用本插件。",
                )
        except Exception:
            pass

    # ------------------------------------------------------------------ 主逻辑
    @filter.on_decorating_result(priority=DECORATE_PRIORITY)
    async def apply_probability_quote(self, event: AstrMessageEvent, *args, **kwargs):
        try:
            self._maybe_quote(event)
        except Exception:
            logger.error(f"[引用概率] 处理失败: {traceback.format_exc()}")

    def _maybe_quote(self, event: AstrMessageEvent) -> None:
        if not self._cfg_bool("enabled", True):
            return

        result = event.get_result()
        if result is None or not list(getattr(result, "chain", []) or []):
            return

        chain = list(result.chain)
        if any(isinstance(comp, Reply) for comp in chain):
            return

        if self._cfg_bool("only_llm_result", True):
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

        min_chars = max(self._cfg_int("min_text_chars", 0), 0)
        if min_chars > 0:
            text = "".join(comp.text for comp in chain if isinstance(comp, Plain))
            if len(text.strip()) < min_chars:
                return

        message_obj = getattr(event, "message_obj", None)
        message_id = str(getattr(message_obj, "message_id", "") or "").strip()
        if not message_id:
            return

        umo = str(getattr(event, "unified_msg_origin", "") or "")
        now = time.time()
        cooldown = max(self._cfg_int("cooldown_seconds", 0), 0)
        if cooldown > 0 and now - self._last_quote_ts.get(umo, 0.0) < cooldown:
            return

        probability = self._cfg_float("probability", 0.1)
        if probability <= 0:
            return
        if probability < 1 and random.random() >= probability:
            return

        result.chain.insert(self._insert_index(result.chain), Reply(id=message_id))
        self._last_quote_ts[umo] = now
        self._prune_cache(now)
        if self._cfg_bool("debug_log", False):
            logger.info(
                f"[引用概率] 命中引用: chat={umo or 'unknown'} "
                f"message_id={message_id} probability={probability}",
            )

    @staticmethod
    def _insert_index(chain: list[Any]) -> int:
        """引用尽量落在文本前面；语音、图片打头的消息把引用放到它们之后。"""
        index = 0
        for comp in chain:
            if isinstance(comp, (Record, Image)):
                index += 1
                continue
            break
        return index

    def _prune_cache(self, now: float) -> None:
        if len(self._last_quote_ts) <= 512:
            return
        self._last_quote_ts = {
            key: ts for key, ts in self._last_quote_ts.items() if now - ts < 86400
        }

    async def terminate(self):
        self._last_quote_ts.clear()