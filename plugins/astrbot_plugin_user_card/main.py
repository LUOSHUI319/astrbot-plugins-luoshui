"""用户卡片插件：查询 QQ 用户资料并渲染成资料卡图片。"""

from __future__ import annotations

import asyncio
import io
import json
import os
import random
import re
import time
import urllib.request
from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.star import Context, Star, register

try:
    from .card_renderer import render_box_card, render_card, save_card
except ImportError:  # 兼容非包方式加载
    from card_renderer import render_box_card, render_card, save_card

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
EXTRA_STORE = os.path.join(PLUGIN_DIR, "data", "extra.json")
OUTPUT_DIR = os.path.join(PLUGIN_DIR, "data", "out")

FIELD_ORDER = [
    "QQ号",
    "昵称",
    "群名片",
    "性别",
    "生日",
    "星座",
    "生肖",
    "血型",
    "年龄",
    "现居",
    "职业",
    "QQVIP",
    "VIP等级",
    "群身份",
    "群等级",
    "入群时间",
    "QQ等级",
    "注册时间",
    "签名",
]

FIELD_LABELS = {
    "群名片": "群名片",
    "群身份": "群身份",
}

ZODIAC = [
    ("摩羯座", (12, 22), (1, 19)),
    ("水瓶座", (1, 20), (2, 18)),
    ("双鱼座", (2, 19), (3, 20)),
    ("白羊座", (3, 21), (4, 19)),
    ("金牛座", (4, 20), (5, 20)),
    ("双子座", (5, 21), (6, 21)),
    ("巨蟹座", (6, 22), (7, 22)),
    ("狮子座", (7, 23), (8, 22)),
    ("处女座", (8, 23), (9, 22)),
    ("天秤座", (9, 23), (10, 23)),
    ("天蝎座", (10, 24), (11, 22)),
    ("射手座", (11, 23), (12, 21)),
]

SHENGXIAO = "鼠牛虎兔龙蛇马羊猴鸡狗猪"


def _raw_get(raw: Any, key: str) -> Any:
    if raw is None:
        return None
    try:
        if hasattr(raw, "get"):
            return raw.get(key)
        return getattr(raw, key, None)
    except Exception:
        return None


def _ts_to_str(value: Any) -> str:
    try:
        ts = int(value)
    except Exception:
        return ""
    if ts <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _date_to_str(value: Any) -> str:
    """生日可能是 1998-05-20 / 19980520 / 时间戳，统一成 1998-05-20。"""
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        ts = int(value)
        if ts > 1000000000:
            return _ts_to_str(ts)[:10]
        return ""
    text = str(value).strip()
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    return text


def _zodiac_of(birthday: str) -> str:
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", birthday or "")
    if not m:
        return ""
    month, day = int(m.group(2)), int(m.group(3))
    for name, start, end in ZODIAC:
        if start[0] == 12 and end[0] == 1:
            if (month == 12 and day >= start[1]) or (month == 1 and day <= end[1]):
                return name
        elif (month == start[0] and day >= start[1]) or (month == end[0] and day <= end[1]):
            return name
    return ""


def _shengxiao_of(birthday: str) -> str:
    m = re.match(r"(\d{4})-", birthday or "")
    if not m:
        return ""
    year = int(m.group(1))
    return SHENGXIAO[(year - 1900) % 12]


def _age_of(birthday: str) -> str:
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", birthday or "")
    if not m:
        return ""
    try:
        born = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except Exception:
        return ""
    today = datetime.now()
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return f"{age} 岁" if 0 <= age < 150 else ""


def _level_icons(level) -> str:
    """QQ 等级换算：4 星 = 1 月，4 月 = 1 太阳，4 太阳 = 1 皇冠。"""
    try:
        value = int(level)
    except Exception:
        return ""
    if value <= 0:
        return ""
    out = ""
    for icon, unit in (("👑", 64), ("☀️", 16), ("🌙", 4), ("⭐", 1)):
        count = value // unit
        if count:
            out += icon * count
        value %= unit
    return out


def _qq_from_group(raw: Any) -> str:
    gid = _raw_get(raw, "group_id")
    return str(gid) if gid else ""


class UserCardPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._cache: dict[str, tuple[float, dict]] = {}
        self._store: dict[str, dict] = {}
        self._load_store()

    # ---------- 配置 ----------
    def _opt(self, section: str, key: str, default=None):
        cfg = self.config.get(section) or {}
        if isinstance(self.config, dict) and section not in self.config and key in self.config:
            cfg = self.config
        if isinstance(cfg, dict) and key in cfg:
            return cfg[key]
        return default

    @property
    def enabled(self) -> bool:
        return bool(self._opt("basic", "enable", True))

    @property
    def theme(self) -> str:
        return str(self._opt("style", "theme", "cute") or "cute")

    @property
    def unknown(self) -> str:
        return str(self._opt("style", "unknown_text", "未知") or "未知")

    # ---------- 本地补充库 ----------
    def _load_store(self) -> None:
        try:
            if os.path.isfile(EXTRA_STORE):
                with open(EXTRA_STORE, encoding="utf-8") as fh:
                    self._store = json.load(fh) or {}
        except Exception as exc:
            logger.warning(f"[user_card] 读取本地补充库失败: {exc}")
            self._store = {}

    def _save_store(self) -> None:
        try:
            os.makedirs(os.path.dirname(EXTRA_STORE), exist_ok=True)
            with open(EXTRA_STORE, "w", encoding="utf-8") as fh:
                json.dump(self._store, fh, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning(f"[user_card] 写入本地补充库失败: {exc}")

    def set_extra(self, qq: str, key: str, value: str) -> None:
        item = self._store.setdefault(str(qq), {})
        item[key] = value
        self._save_store()

    # ---------- 取数 ----------
    async def _call_action(self, event: AstrMessageEvent, action: str, **params):
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            return None
        try:
            return await bot.call_action(action, **params)
        except Exception as exc:
            logger.debug(f"[user_card] {action} 调用失败: {exc}")
            return None

    async def _download_avatar(self, qq: str):
        url = f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"
        try:
            data = await asyncio.to_thread(self._http_get, url)
            from PIL import Image as PILImage

            return PILImage.open(io.BytesIO(data))
        except Exception as exc:
            logger.debug(f"[user_card] 头像下载失败 {qq}: {exc}")
            return None

    @staticmethod
    def _http_get(url: str, timeout: int = 10) -> bytes:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    async def collect(self, event: AstrMessageEvent, qq: str, group_id: str = "") -> dict:
        cache_seconds = int(self._opt("basic", "cache_seconds", 600) or 0)
        now = time.time()
        cached = self._cache.get(qq)
        if cache_seconds > 0 and cached and now - cached[0] < cache_seconds:
            data = dict(cached[1])
            data["群号"] = group_id or data.get("群号", "")
            return data

        data: dict[str, Any] = {"QQ号": qq}
        stranger = await self._call_action(event, "get_stranger_info", user_id=int(qq), no_cache=True)
        if isinstance(stranger, dict):
            nick = (
                stranger.get("nickname")
                or stranger.get("nick")
                or stranger.get("long_nick")
            )
            if nick:
                data["昵称"] = str(nick)
            sex = stranger.get("sex")
            if sex:
                data["性别"] = {"male": "男", "female": "女"}.get(str(sex), str(sex))
            age = stranger.get("age")
            if age:
                data["年龄"] = f"{age} 岁"
            level = stranger.get("qqLevel") or stranger.get("qq_level") or stranger.get("level")
            if level:
                icons = _level_icons(level)
                data["QQ等级"] = f"{icons}({level})" if icons else str(level)
            reg = stranger.get("regTime") or stranger.get("reg_time")
            if reg:
                data["注册时间"] = _ts_to_str(reg)
            if stranger.get("qid"):
                data["QID"] = str(stranger["qid"])
            sign = stranger.get("longNick") or stranger.get("long_nick")
            if sign:
                data["签名"] = str(sign)
            if isinstance(stranger.get("is_vip"), bool) or stranger.get("is_vip") in (0, 1):
                data["QQVIP"] = "已开" if stranger.get("is_vip") else "未开"
            if stranger.get("is_years_vip"):
                data["年费会员"] = "是"
            vip_level = stranger.get("vip_level")
            if vip_level:
                data["VIP等级"] = str(vip_level)
            year = stranger.get("birthday_year")
            month = stranger.get("birthday_month")
            day = stranger.get("birthday_day")
            if year and month and day:
                try:
                    data["生日"] = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
                except Exception:
                    pass
            blood = stranger.get("kBloodType") or stranger.get("blood_type")
            if blood:
                blood_map = {1: "A 型", 2: "B 型", 3: "O 型", 4: "AB 型"}
                try:
                    key = int(blood)
                    if key in blood_map:
                        data["血型"] = blood_map[key]
                except Exception:
                    data["血型"] = str(blood)
            place = " ".join(
                str(item)
                for item in (
                    stranger.get("country"),
                    stranger.get("province"),
                    stranger.get("city"),
                )
                if item
            )
            if place:
                data["现居"] = place
            college = stranger.get("college")
            if college:
                data["学校"] = str(college)
            if stranger.get("position") or stranger.get("pos"):
                data["职业"] = str(stranger.get("position") or stranger.get("pos"))

        if group_id:
            member = await self._call_action(
                event,
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(qq),
                no_cache=True,
            )
            if isinstance(member, dict):
                card = member.get("card")
                nickname = member.get("nickname")
                if nickname and not data.get("昵称"):
                    data["昵称"] = str(nickname)
                if card:
                    data["群名片"] = str(card)
                if member.get("area") and not data.get("现居"):
                    data["现居"] = str(member["area"])
                if member.get("level"):
                    data["群等级"] = str(member["level"])
                if member.get("join_time"):
                    data["入群时间"] = _ts_to_str(member["join_time"])
                if not data.get("QQ等级") and member.get("qq_level"):
                    icons = _level_icons(member["qq_level"])
                    data["QQ等级"] = (
                        f"{icons}({member['qq_level']})" if icons else str(member["qq_level"])
                    )
                role = {"owner": "群主", "admin": "管理员", "member": "群员"}.get(
                    str(member.get("role") or ""), ""
                )
                if role:
                    data["群身份"] = role

        extra = self._store.get(qq) or {}
        for key, value in extra.items():
            if value:
                data[key] = value

        await self._fill_from_api(qq, data)

        data["星座"] = data.get("星座") or _zodiac_of(data.get("生日", ""))
        data["生肖"] = data.get("生肖") or _shengxiao_of(data.get("生日", ""))
        if not data.get("年龄"):
            data["年龄"] = _age_of(data.get("生日", ""))
        data["群号"] = group_id or data.get("群号", "")

        if cache_seconds > 0:
            self._cache[qq] = (now, dict(data))
        return data

    async def _fill_from_api(self, qq: str, data: dict) -> None:
        endpoint = str(self._opt("basic", "extra_api", "") or "").strip()
        if not endpoint:
            return
        url = endpoint.replace("{qq}", qq)
        if "{qq}" not in endpoint:
            url = f"{endpoint}{'&' if '?' in endpoint else '?'}qq={qq}"
        token = str(self._opt("basic", "extra_api_token", "") or "").strip()

        def _fetch():
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8", "ignore"))

        try:
            payload = await asyncio.to_thread(_fetch)
        except Exception as exc:
            logger.debug(f"[user_card] 第三方接口失败: {exc}")
            return
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            payload = payload["data"]
        if not isinstance(payload, dict):
            return
        mapping = {
            "birthday": "生日",
            "birth": "生日",
            "constellation": "星座",
            "zodiac": "星座",
            "shengxiao": "生肖",
            "city": "现居",
            "area": "现居",
            "location": "现居",
            "job": "职业",
            "profession": "职业",
            "vip": "QQVIP",
            "vip_level": "VIP等级",
            "sign": "签名",
            "signature": "签名",
            "longnick": "签名",
        }
        for key, target in mapping.items():
            value = payload.get(key)
            if value not in (None, "", 0, "0", []):
                if target not in data or not data.get(target):
                    data[target] = str(value)

    # ---------- 渲染 ----------
    async def build_card(self, event: AstrMessageEvent, qq: str, group_id: str = "") -> str:
        data = await self.collect(event, qq, group_id)
        fields = []
        for name in FIELD_ORDER:
            value = data.get(name)
            if name == "签名" and not self._opt("style", "show_sign", True):
                continue
            fields.append((name, str(value) if value not in (None, "") else self.unknown))
        avatar = await self._download_avatar(qq)
        footer = f"生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        if self.theme == "box":
            image = await asyncio.to_thread(
                render_box_card, fields, avatar, random.Random()
            )
        else:
            image = await asyncio.to_thread(render_card, fields, avatar, self.theme, footer)
        return await asyncio.to_thread(save_card, image, OUTPUT_DIR, f"card_{qq}_{int(time.time())}.png")

    # ---------- 目标解析 ----------
    def _extract_target(self, event: AstrMessageEvent) -> tuple[str, str]:
        """返回 (目标 QQ, 群号)。"""
        group_id = str(event.get_group_id() or "")
        self_id = str(event.get_self_id() or "")
        messages = event.get_messages() or []
        for comp in messages:
            if isinstance(comp, At):
                qq = str(getattr(comp, "qq", "") or "")
                if qq and qq != "all" and qq != self_id:
                    return qq, group_id
        for comp in messages:
            if isinstance(comp, Reply):
                sender = str(getattr(comp, "sender_id", "") or "")
                if sender and sender != "0" and sender != self_id:
                    return sender, group_id
        matched = re.search(r"\b(\d{5,12})\b", event.message_str or "")
        if matched and matched.group(1) != self_id:
            return matched.group(1), group_id
        return str(event.get_sender_id() or ""), group_id

    # ---------- 指令 ----------
    @filter.command("用户卡片")
    async def user_card(self, event: AstrMessageEvent):
        """用户卡片 @某人 / 回复某人 / 用户卡片 QQ号"""
        if not self.enabled:
            return
        group_id = str(event.get_group_id() or "")
        blocked = str(self._opt("permissions", "blocked_groups", "") or "")
        if group_id and group_id in re.split(r"[,，\s]+", blocked.strip()):
            return
        qq, group_id = self._extract_target(event)
        if not qq:
            yield event.plain_result("没认出要查谁，@一位或者直接发 QQ 号。")
            return
        if (
            qq != str(event.get_sender_id())
            and not self._opt("permissions", "allow_other_query", True)
        ):
            yield event.plain_result("这个群只允许查自己。")
            return
        try:
            path = await self.build_card(event, qq, group_id)
        except Exception as exc:
            logger.error(f"[user_card] 生成卡片失败: {exc}")
            yield event.plain_result("卡片没生成出来，等会儿再试。")
            return
        yield event.chain_result([Image.fromFileSystem(path)])

    @filter.command("卡片诊断")
    async def card_diag(self, event: AstrMessageEvent):
        """卡片诊断 @某人：查看 OneBot 实际返回了哪些字段"""
        if not self.enabled:
            return
        qq, group_id = self._extract_target(event)
        if not qq:
            yield event.plain_result("没认出要查谁。")
            return
        lines = [f"目标：{qq}", f"群号：{group_id or '无'}"]
        stranger = await self._call_action(event, "get_stranger_info", user_id=int(qq), no_cache=True)
        if isinstance(stranger, dict):
            lines.append("stranger_info 字段：" + "、".join(sorted(stranger.keys())) or "空")
            lines.append("原始：" + json.dumps(stranger, ensure_ascii=False)[:600])
        else:
            lines.append("stranger_info：调用失败或返回空")
        if group_id:
            member = await self._call_action(
                event, "get_group_member_info", group_id=int(group_id), user_id=int(qq), no_cache=True
            )
            if isinstance(member, dict):
                lines.append("member_info 字段：" + "、".join(sorted(member.keys())))
                lines.append("原始：" + json.dumps(member, ensure_ascii=False)[:600])
            else:
                lines.append("member_info：调用失败或返回空")
        yield event.plain_result(chr(10).join(lines))

    @filter.command("卡片设置")
    async def card_setting(self, event: AstrMessageEvent):
        """卡片设置 生日 2005-05-20（补全接口拿不到的字段）"""
        if not self.enabled:
            return
        text = (event.message_str or "").strip()
        parts = text.split()
        if len(parts) < 3:
            yield event.plain_result("用法：/卡片设置 生日 2005-05-20（可设置：生日/现居/职业/QQVIP/VIP等级/签名）")
            return
        key, value = parts[1], " ".join(parts[2:])
        allowed = {"生日", "现居", "职业", "QQVIP", "VIP等级", "签名"}
        if key not in allowed:
            yield event.plain_result("只能设置：" + "、".join(sorted(allowed)))
            return
        if key == "生日":
            value = _date_to_str(value)
            if not re.match(r"\d{4}-\d{2}-\d{2}", value):
                yield event.plain_result("生日格式应为 2005-05-20。")
                return
        self.set_extra(str(event.get_sender_id()), key, value)
        self._cache.pop(str(event.get_sender_id()), None)
        yield event.plain_result(f"记下了：{key} = {value}")

    # ---------- 事件 ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_notice(self, event: AstrMessageEvent):
        """新人进群自动发卡"""
        if not self.enabled or not self._opt("basic", "auto_card_on_join", True):
            return
        raw = getattr(event.message_obj, "raw_message", None)
        if _raw_get(raw, "post_type") != "notice":
            return
        if _raw_get(raw, "notice_type") != "group_increase":
            return
        group_id = _raw_get(raw, "group_id")
        user_id = _raw_get(raw, "user_id")
        if not group_id or not user_id:
            return
        blocked = str(self._opt("permissions", "blocked_groups", "") or "")
        if str(group_id) in re.split(r"[,，\s]+", blocked.strip()):
            return
        try:
            path = await self.build_card(event, str(user_id), str(group_id))
        except Exception as exc:
            logger.error(f"[user_card] 新人卡片失败: {exc}")
            return
        yield event.chain_result([Image.fromFileSystem(path)])

    async def terminate(self):
        self._save_store()
