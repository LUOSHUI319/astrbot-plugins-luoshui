"""群禁言小工具：群内发「禁言 @某人 60」即可禁言，同时注册成 LLM 工具。"""

import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At
from astrbot.api.star import Context, Star, register

DEFAULT_SECONDS = 60
MAX_SECONDS = 30 * 24 * 3600


@register("astrbot_plugin_group_mute", "洛水", "群禁言小工具：发「禁言 @某人 秒数」即可禁言", "1.0.0")
class GroupMutePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)

    @staticmethod
    def _extract_target(event: AstrMessageEvent) -> str:
        for seg in event.get_messages():
            if isinstance(seg, At):
                qq = str(getattr(seg, "qq", "") or "")
                if qq and qq != "all":
                    return qq
        return ""

    @staticmethod
    def _extract_seconds(text: str) -> int:
        nums = re.findall(r"\d+", text or "")
        if not nums:
            return DEFAULT_SECONDS
        return int(nums[-1])

    async def _do_mute(self, event: AstrMessageEvent, target: str, seconds: int) -> str:
        if not target or not target.isdigit():
            return "没找到要禁言的人，@ 一下再发一次"
        if target == str(event.get_self_id()):
            return "禁言失败：不能禁言我自己"
        bot = getattr(event, "bot", None)
        group_id = event.get_group_id()
        if bot is None or not group_id:
            return "禁言失败：当前平台不支持群禁言"
        seconds = max(1, min(int(seconds), MAX_SECONDS))
        try:
            await bot.call_action(
                "set_group_ban",
                group_id=int(group_id),
                user_id=int(target),
                duration=seconds,
            )
        except Exception as exc:
            logger.warning("[GroupMute] set_group_ban failed: %s", exc)
            return f"禁言失败：{exc}"
        if seconds >= 3600:
            return f"已禁言 {target}，{seconds / 3600:.1f} 小时"
        if seconds >= 60:
            return f"已禁言 {target}，{seconds / 60:.1f} 分钟"
        return f"已禁言 {target}，{seconds} 秒"

    @filter.command("禁言")
    async def mute_group_member_cmd(self, event: AstrMessageEvent):
        """禁言 @某人 秒数"""
        target = self._extract_target(event)
        seconds = self._extract_seconds(event.message_str)
        yield event.plain_result(await self._do_mute(event, target, seconds))

    @filter.command("解禁")
    async def unmute_group_member_cmd(self, event: AstrMessageEvent):
        """解禁 @某人"""
        target = self._extract_target(event)
        yield event.plain_result(await self._do_mute(event, target, 0))

    @filter.llm_tool()
    async def mute_group_member(self, event: AstrMessageEvent, user_id: str, seconds: int = 60):
        """
        在群里禁言指定成员，只有机器人为群管理时才能生效。
        Args:
            user_id(string): 要禁言的群成员 QQ 号，必须是纯数字
            seconds(number): 禁言时长，单位秒，默认 60
        """
        return await self._do_mute(event, str(user_id).strip(), int(seconds))
