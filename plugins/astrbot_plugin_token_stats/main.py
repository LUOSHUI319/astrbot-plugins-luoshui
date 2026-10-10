"""会话 token 用量查询插件（含按模型估算花费）。

数据来源：AstrBot 主库 data/data_v4.db 的 provider_stats 表。
AstrBot 每次调用大模型都会往这张表落一条记录，字段含义：

- umo                    会话号，形如 default:FriendMessage:3190568934
- provider_id / model    用的哪个供应商、哪个模型
- token_input_other      没命中缓存的输入 token
- token_input_cached     命中缓存的输入 token（也就是「命中」的部分）
- token_output           输出 token
- created_at             记录时间，注意是 UTC，算北京时间要 +8 小时

所以：
- 当天消耗 = 按 created_at 过滤后的三段 token 各自求和
- 缓存命中率 = token_input_cached / (token_input_cached + token_input_other)
- 估算花费 = 按 model 分组，各段 token 乘上对应单价再求和

指令：
- /token                本会话 今天
- /token 7d             本会话 近 7 天
- /token all            本会话 全部
- /token 排行           所有会话 今天 的排行
- /token 价目           看当前生效的单价表
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
import sqlite3
import traceback
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

USAGE = (
    "用法：\n"
    "/token 今天        本会话今天（也可以直接发 /token）\n"
    "/token 7d          本会话近 7 天\n"
    "/token 30d         本会话近 30 天\n"
    "/token all         本会话全部时间\n"
    "/token 排行        今天所有会话的用量排行\n"
    "/token 价目        看当前生效的模型单价\n"
    "/token 帮助        看这句说明\n"
    "别名：用量 / 消耗 / 缓存命中"
)

PLUGIN_NAME = "astrbot_plugin_token_stats"
PLUGIN_VERSION = "v1.1.0"

DEFAULTS: dict[str, Any] = {
    "timezone_offset": 8,
    "rank_size": 10,
    "allow_group": True,
    "admin_only": False,
    "default_range": "today",
    "show_cost": True,
    "currency": "¥",
    "price_map": "",
    "default_input_price": 2.0,
    "default_cached_price": 0.5,
    "default_output_price": 8.0,
}

CONFIG_GROUPS = ("stats_settings", "scope_settings", "pricing_settings")

# 内置参考单价：元 / 百万 token。只是估值用，实际以供应商账单为准。
BUILTIN_PRICES: dict[str, dict[str, float]] = {
    "deepseek-chat": {"input": 2.0, "cached": 0.5, "output": 8.0},
    "deepseek-reasoner": {"input": 4.0, "cached": 1.0, "output": 16.0},
    "deepseek-v3": {"input": 2.0, "cached": 0.5, "output": 8.0},
    "deepseek-flash": {"input": 1.0, "cached": 0.2, "output": 4.0},
    "deepseek-v4.1-flash": {"input": 1.0, "cached": 0.2, "output": 4.0},
    "deepseek-v4.1": {"input": 4.0, "cached": 1.0, "output": 16.0},
    "deepseek-v4": {"input": 4.0, "cached": 1.0, "output": 16.0},
    "gpt-4o-mini": {"input": 1.1, "cached": 0.55, "output": 4.3},
    "gpt-4o": {"input": 18.0, "cached": 9.0, "output": 72.0},
    "claude-3-5-sonnet": {"input": 21.6, "cached": 2.16, "output": 108.0},
    "claude-3-7-sonnet": {"input": 21.6, "cached": 2.16, "output": 108.0},
    "gemini-1.5-pro": {"input": 9.0, "cached": 2.25, "output": 36.0},
    "gemini-2.0-flash": {"input": 0.7, "cached": 0.18, "output": 2.9},
    "qwen-max": {"input": 2.4, "cached": 2.4, "output": 9.6},
    "qwen-plus": {"input": 0.8, "cached": 0.8, "output": 2.0},
    "glm-4-plus": {"input": 5.0, "cached": 5.0, "output": 5.0},
    "glm-4-flash": {"input": 0.0, "cached": 0.0, "output": 0.0},
    "moonshot-v1-8k": {"input": 12.0, "cached": 12.0, "output": 12.0},
}


def _db_path() -> str:
    candidates: list[str] = []
    try:
        from astrbot.core.config.default import DB_PATH

        if DB_PATH:
            candidates.append(DB_PATH)
    except Exception:
        pass
    try:
        root = os.environ.get("ASTRBOT_ROOT")
        if root:
            candidates.append(os.path.join(root, "data", "data_v4.db"))
    except Exception:
        pass
    try:
        import astrbot

        pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(astrbot.__file__)))
        candidates.append(os.path.join(pkg_root, "data", "data_v4.db"))
    except Exception:
        pass
    candidates.append(os.path.join(os.getcwd(), "data", "data_v4.db"))
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        candidates.append(os.path.join(get_astrbot_data_path(), "data_v4.db"))
    except Exception:
        pass

    for path in candidates:
        if path and os.path.exists(path):
            return os.path.realpath(path)
    return os.path.realpath(candidates[0]) if candidates else "data/data_v4.db"


def _fmt(num: Any) -> str:
    try:
        n = float(num or 0)
    except (TypeError, ValueError):
        return "0"
    if n >= 100_000_000:
        return f"{n / 100_000_000:.2f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(int(n))


def _fmt_money(value: Any, currency: str = "¥") -> str:
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        n = 0.0
    if n <= 0:
        return f"{currency}0"
    if n < 0.01:
        return f"{currency}{n:.4f}"
    if n < 1:
        return f"{currency}{n:.3f}"
    return f"{currency}{n:.2f}"


@register(
    PLUGIN_NAME,
    "洛水",
    "查询某个会话当天/近7天/全部的 token 消耗、缓存命中率，并按模型估算花了多少钱",
    PLUGIN_VERSION,
)
class TokenStatsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.db_path = _db_path()
        self._prices = self._load_prices()
        logger.info(
            f"[用量统计] 已加载，数据库：{self.db_path}，单价表 {len(self._prices)} 项",
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

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self._cfg(key))
        except (TypeError, ValueError):
            return default

    def _currency(self) -> str:
        value = self._cfg("currency")
        return str(value).strip() if value else "¥"

    # ------------------------------------------------------------------ 单价
    def _load_prices(self) -> dict[str, dict[str, float]]:
        table: dict[str, dict[str, float]] = {
            key.lower(): dict(val) for key, val in BUILTIN_PRICES.items()
        }
        raw = self._cfg("price_map")
        if isinstance(raw, str) and raw.strip():
            try:
                raw = json.loads(raw)
            except Exception:
                logger.warning("[用量统计] price_map 不是合法 JSON，已忽略自定义单价")
                raw = None
        if isinstance(raw, dict):
            for name, val in raw.items():
                if not isinstance(val, dict):
                    continue
                try:
                    input_price = float(val.get("input", 0) or 0)
                except (TypeError, ValueError):
                    input_price = 0.0
                try:
                    cached_price = float(val.get("cached", input_price) or 0)
                except (TypeError, ValueError):
                    cached_price = input_price
                try:
                    output_price = float(val.get("output", 0) or 0)
                except (TypeError, ValueError):
                    output_price = 0.0
                table[str(name).strip().lower()] = {
                    "input": input_price,
                    "cached": cached_price,
                    "output": output_price,
                }
        return table

    def _reload_prices(self) -> None:
        self._prices = self._load_prices()

    def _price_for(self, model: str) -> tuple[dict[str, float], bool]:
        """返回 (单价, 是否是精确/模糊匹配到的)。"""
        name = str(model or "").strip().lower()
        table = self._prices
        if name and name in table:
            return table[name], True
        if name:
            # 长键优先，避免 deepseek-v4 抢在 deepseek-v4.1-flash 前面命中
            for key in sorted(table, key=len, reverse=True):
                if name.startswith(key) or key in name:
                    return table[key], True
        fallback = {
            "input": self._cfg_float("default_input_price", 2.0),
            "cached": self._cfg_float("default_cached_price", 0.5),
            "output": self._cfg_float("default_output_price", 8.0),
        }
        return fallback, False

    def _cost(self, other: Any, cached: Any, output: Any, price: dict[str, float]) -> float:
        try:
            return (
                float(other or 0) * float(price.get("input", 0) or 0)
                + float(cached or 0) * float(price.get("cached", 0) or 0)
                + float(output or 0) * float(price.get("output", 0) or 0)
            ) / 1_000_000
        except (TypeError, ValueError):
            return 0.0

    # ------------------------------------------------------------------ 时间
    def _since(self, days: int) -> str:
        """返回 UTC 字符串，表示本地时区往前 days 天（1 表示从本地今天 0 点）。"""
        offset = self._cfg_int("timezone_offset", 8)
        tz = datetime.timezone(datetime.timedelta(hours=offset))
        now_local = datetime.datetime.now(tz)
        if days <= 1:
            start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            start_local = now_local - datetime.timedelta(days=days - 1)
            start_local = start_local.replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = start_local.astimezone(datetime.timezone.utc)
        return start_utc.strftime("%Y-%m-%d %H:%M:%S")

    # ------------------------------------------------------------------ 查询
    def _query_session(self, umo: str, since: str) -> tuple:
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute(
                "SELECT COUNT(*), "
                "COALESCE(SUM(token_input_other),0), "
                "COALESCE(SUM(token_input_cached),0), "
                "COALESCE(SUM(token_output),0) "
                "FROM provider_stats WHERE umo = ? AND created_at >= ?",
                (umo, since),
            ).fetchone()
        finally:
            con.close()
        return row or (0, 0, 0, 0)

    def _query_session_by_model(self, umo: str, since: str) -> list[tuple]:
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute(
                "SELECT COALESCE(provider_model, '') AS m, COUNT(*), "
                "COALESCE(SUM(token_input_other),0), "
                "COALESCE(SUM(token_input_cached),0), "
                "COALESCE(SUM(token_output),0) "
                "FROM provider_stats WHERE umo = ? AND created_at >= ? "
                "GROUP BY m ORDER BY 2 DESC",
                (umo, since),
            ).fetchall()
        finally:
            con.close()
        return rows or []

    def _query_rank(self, since: str, limit: int) -> list[tuple]:
        """按 (umo, model) 拉，再在内存里按会话合并算钱。"""
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute(
                "SELECT umo, COALESCE(provider_model, '') AS m, COUNT(*), "
                "COALESCE(SUM(token_input_other),0), "
                "COALESCE(SUM(token_input_cached),0), "
                "COALESCE(SUM(token_output),0) "
                "FROM provider_stats WHERE created_at >= ? "
                "GROUP BY umo, m",
                (since,),
            ).fetchall()
        finally:
            con.close()

        merged: dict[str, dict[str, Any]] = {}
        for umo, model, cnt, other, cached, out in rows or []:
            item = merged.setdefault(
                umo,
                {"cnt": 0, "other": 0, "cached": 0, "out": 0, "cost": 0.0, "models": set()},
            )
            price, _known = self._price_for(model)
            item["cnt"] += cnt or 0
            item["other"] += other or 0
            item["cached"] += cached or 0
            item["out"] += out or 0
            item["cost"] += self._cost(other, cached, out, price)
            if model:
                item["models"].add(model)

        result = sorted(
            merged.items(),
            key=lambda kv: (kv[1]["other"] + kv[1]["cached"]),
            reverse=True,
        )
        return result[: max(int(limit or 0), 1)]

    # ------------------------------------------------------------------ 指令
    @filter.command("token", alias={"用量", "token用量", "消耗", "缓存命中"})
    async def token_stats(self, event: AstrMessageEvent):
        try:
            if not self._cfg_bool("allow_group", True) and not event.is_private_chat():
                yield event.plain_result("这个指令只在私聊里能用")
                return
            if self._cfg_bool("admin_only", False):
                role = str(getattr(event, "role", "member") or "member")
                if role != "admin":
                    yield event.plain_result("只有管理员能查用量")
                    return

            text = str(getattr(event, "message_str", "") or "").strip()
            # AstrBot 会在消息文本后面挂 [MSG_ID:xxx] 之类的标记，先剥干净
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
            plain = re.sub(r"^[/／#＃!！\s]+", "", text).strip()
            tokens = plain.split()
            if tokens and tokens[0].lower() in {
                "token",
                "token用量",
                "用量",
                "消耗",
                "缓存命中",
            }:
                tokens = tokens[1:]
            arg = "".join(tokens).strip().lower()
            arg = re.sub(r"[\[\]【】()（）\s]", "", arg)
            logger.info(
                f"[token_stats] message_str={text!r} arg={arg!r} "
                f"umo={getattr(event, 'unified_msg_origin', '')!r}",
            )

            # 参数判定放宽成子串匹配，防止被各种标记污染
            want_rank = any(w in arg for w in ("排行", "排名", "榜单", "榜", "rank"))
            days = 1
            label = "今天"
            show_cost = self._cfg_bool("show_cost", True)
            currency = self._currency()

            if any(w in arg for w in ("价目", "单价", "价格", "pricing", "price")):
                self._reload_prices()
                lines = [f"当前生效的单价（元 / 百万 token）· 兜底 {_fmt_money(self._cfg_float('default_input_price', 2.0), currency)} 输入 / {_fmt_money(self._cfg_float('default_cached_price', 0.5), currency)} 命中 / {_fmt_money(self._cfg_float('default_output_price', 8.0), currency)} 输出"]
                for name in sorted(self._prices):
                    p = self._prices[name]
                    lines.append(
                        f"{name}：输入 {p.get('input', 0)} / 命中 {p.get('cached', 0)} / 输出 {p.get('output', 0)}",
                    )
                lines.append("想改就去插件配置的「模型单价」里填 price_map")
                yield event.plain_result("\n".join(lines))
                return

            if not arg:
                days, label = 1, "今天"
            elif want_rank:
                days = 1
            elif arg in {"7", "7d"} or any(
                w in arg for w in ("7d", "7天", "近7", "本周", "周")
            ):
                days, label = 7, "近 7 天"
            elif arg in {"30", "30d"} or any(
                w in arg for w in ("30d", "30天", "近30", "本月", "月")
            ):
                days, label = 30, "近 30 天"
            elif any(w in arg for w in ("all", "全部", "总")):
                days, label = 36500, "全部"
            elif any(w in arg for w in ("今天", "今日", "today", "当天", "本日")):
                days, label = 1, "今天"
            elif arg in {"help", "帮助", "?", "？", "用法", "说明", "怎么用"}:
                yield event.plain_result(USAGE)
                return
            else:
                yield event.plain_result(
                    f"没认出来 {arg!r} 这个参数\n" + USAGE,
                )
                return

            since = self._since(days)
            self._reload_prices()

            if want_rank:
                limit = max(self._cfg_int("rank_size", 10), 1)
                rows = await asyncio.to_thread(self._query_rank, since, limit)
                if not rows:
                    yield event.plain_result("今天还没有调用记录")
                    return
                lines = [f"今天 token 用量排行（前 {limit}）"]
                total_cost = 0.0
                for idx, (umo, item) in enumerate(rows, 1):
                    total_in = (item["other"] or 0) + (item["cached"] or 0)
                    hit = (item["cached"] or 0) / total_in * 100 if total_in else 0
                    total_cost += item["cost"] or 0.0
                    names = "/".join(sorted(item["models"])) or "未知"
                    lines.append(
                        f"{idx}. {umo}\n"
                        f"   {item['cnt']} 次 输入{_fmt(total_in)} 输出{_fmt(item['out'])} "
                        f"命中{hit:.1f}%\n"
                        f"   {names} 约 {_fmt_money(item['cost'], currency)}",
                    )
                if show_cost:
                    lines.append(f"以上合计约 {_fmt_money(total_cost, currency)}")
                yield event.plain_result("\n".join(lines))
                return

            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if not umo:
                yield event.plain_result("拿不到当前会话号，查不了")
                return
            cnt, other, cached, out = await asyncio.to_thread(
                self._query_session,
                umo,
                since,
            )
            total_in = (other or 0) + (cached or 0)
            hit = (cached or 0) / total_in * 100 if total_in else 0.0
            if not cnt:
                yield event.plain_result(f"本会话{label}还没有调用记录")
                return

            msg = (
                f"本会话·{label}\n"
                f"调用 {cnt} 次\n"
                f"输入 {_fmt(total_in)}（命中 {_fmt(cached)} / 未命中 {_fmt(other)}）\n"
                f"输出 {_fmt(out)}\n"
                f"缓存命中率 {hit:.2f}%"
            )

            if show_cost:
                by_model = await asyncio.to_thread(
                    self._query_session_by_model,
                    umo,
                    since,
                )
                total_cost = 0.0
                detail: list[str] = []
                for model, mcnt, mother, mcached, mout in by_model or []:
                    price, known = self._price_for(model)
                    cost = self._cost(mother, mcached, mout, price)
                    total_cost += cost
                    mark = "" if known else "（按兜底单价）"
                    name = model or "未知模型"
                    detail.append(f"  {name}{mark} 约 {_fmt_money(cost, currency)}")
                if detail:
                    msg += f"\n\n估算花费 约 {_fmt_money(total_cost, currency)}"
                    if len(detail) > 1:
                        msg += "\n按模型拆：\n" + "\n".join(detail)
                    msg += "\n（只是估算，正式账单看供应商后台）"
                else:
                    msg += "\n\n估算花费 算不出来（没有模型信息）"

            yield event.plain_result(msg)
        except Exception:
            logger.error(f"[用量统计] 查询失败: {traceback.format_exc()}")
            yield event.plain_result("查用量的时候出错了，看一眼日志")

    async def terminate(self):
        pass
