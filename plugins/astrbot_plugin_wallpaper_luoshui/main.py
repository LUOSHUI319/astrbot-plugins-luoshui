"""洛水壁纸工坊 —— AstrBot 插件入口。

把 AstrBot 面板（WebUI）的背景换成静态图或动态 mp4，并且全部参数都能在
插件页面 pages/manager 里调：扫描壁纸文件夹、上传壁纸、轮换集、遮罩透明度、
字体颜色、模糊程度、切换过渡时长。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .wp_api import WallpaperAPI
from .wp_core import (
    DEFAULTS,
    PLUGIN_NAME,
    BUNDLED_FFMPEG_DIR,
    JobManager,
    WallpaperStore,
    flatten_config,
    safe_name,
)

PLUGIN_VERSION = "v1.0.7"

UI_THEME_KEYS = ("ui_text_color", "ui_sub_color")


def _find_astrbot_root(start: Path) -> Path:
    """从插件数据目录往上找带 data/dist 的 AstrBot 根目录。"""
    for candidate in [start, *list(start.parents)]:
        try:
            if (candidate / "data" / "dist").exists():
                return candidate
        except Exception:
            continue
    return Path(start)


@register(
    PLUGIN_NAME,
    "洛水",
    "给 AstrBot 面板换壁纸：扫描/上传壁纸、轮换集、遮罩透明度、字体颜色、模糊、切换过渡都能在插件页面调",
    PLUGIN_VERSION,
)
class WallpaperStudio(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}
        self.base_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.root = _find_astrbot_root(self.base_dir)
        self.config_path = self.root / "data" / "config" / f"{PLUGIN_NAME}_config.json"
        self.theme_path = self.base_dir / "ui_theme.json"
        self.store = WallpaperStore(self.base_dir, self.root, JobManager())
        self.api = WallpaperAPI(self)
        self.api.register(context)

        self.config_flat: dict[str, Any] = flatten_config(self.config)
        self.last_apply: dict | None = None
        self._sig = ""
        self._task: asyncio.Task | None = None
        self._ff_task: asyncio.Task | None = None
        self._closed = False

    # ------------------------------------------------------------------ 配置

    def read_ui_theme(self) -> dict:
        """插件页面自己那两个文字颜色单独存一份，免得被其它写配置的流程冲掉。"""
        try:
            data = json.loads(self.theme_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {k: str(data.get(k) or "") for k in UI_THEME_KEYS}
        except Exception:
            pass
        return {}

    def write_ui_theme(self, theme: dict) -> None:
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            self.theme_path.write_text(
                json.dumps(
                    {k: str(theme.get(k) or "") for k in UI_THEME_KEYS},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.error(f"[壁纸工坊] 写页面主题失败: {exc}")

    def _fill_ui_theme(self, raw: dict) -> bool:
        """配置里这两项缺失或被清空时，用单独那份补回来。"""
        theme = self.read_ui_theme()
        if not theme:
            return False
        node = raw.get("appearance")
        if not isinstance(node, dict):
            node = {}
        changed = False
        for key in UI_THEME_KEYS:
            if not str(node.get(key) or "").strip() and str(theme.get(key) or "").strip():
                node[key] = theme[key]
                changed = True
        if changed:
            raw["appearance"] = node
        return changed

    def _dump_config(self, raw: dict) -> bool:
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.config_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.config_path)
            return True
        except Exception as exc:
            logger.error(f"[壁纸工坊] 写配置失败: {exc}")
            return False

    def read_config_raw(self) -> dict:
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raw = {}
        except Exception:
            raw = dict(self.config) if isinstance(self.config, dict) else {}
        if self._fill_ui_theme(raw):
            self._dump_config(raw)
        return raw

    def write_config(self, grouped: dict[str, dict]) -> bool:
        raw = self.read_config_raw()
        for group, payload in (grouped or {}).items():
            if not isinstance(payload, dict):
                continue
            node = raw.get(group)
            if not isinstance(node, dict):
                node = {}
            node.update(payload)
            raw[group] = node
        for key, value in (grouped or {}).items():
            if key in DEFAULTS and not isinstance(value, dict):
                raw[key] = value
        theme_patch = (grouped or {}).get("appearance")
        if isinstance(theme_patch, dict) and any(k in theme_patch for k in UI_THEME_KEYS):
            theme = self.read_ui_theme()
            for key in UI_THEME_KEYS:
                if key in theme_patch:
                    theme[key] = str(theme_patch.get(key) or "")
            self.write_ui_theme(theme)
        if not self._dump_config(raw):
            return False
        self.config_flat = flatten_config(raw)
        self._sig = json.dumps(raw, sort_keys=True, ensure_ascii=False)
        return True

    # ------------------------------------------------------------------ 应用

    def apply_now(self) -> dict:
        try:
            result = self.store.apply(self.config_flat)
            result["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.last_apply = result
            if self.config_flat.get("debug_log"):
                logger.info(f"[壁纸工坊] 应用结果: {result}")
            return result
        except Exception as exc:
            logger.error(f"[壁纸工坊] 应用失败: {exc}")
            return {"applied": False, "reason": str(exc), "count": 0, "items": []}

    async def _watch_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(3)
            try:
                raw = self.read_config_raw()
                sig = json.dumps(raw, sort_keys=True, ensure_ascii=False)
                if sig != self._sig:
                    self._sig = sig
                    self.config_flat = flatten_config(raw)
                    self.apply_now()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.debug(f"[壁纸工坊] 配置监听异常: {exc}")

    # ------------------------------------------------------------------ 生命周期

    async def _ensure_ffmpeg(self) -> None:
        """第一次装插件时检查有没有 ffmpeg：插件包自带就直接用，没有就自动装一份。"""

        def log(message: str) -> None:
            logger.info(f"[壁纸工坊] {message}")

        try:
            loop = asyncio.get_running_loop()
            got = await loop.run_in_executor(None, lambda: self.store.install_ffmpeg(log))
            if got:
                logger.info(f"[壁纸工坊] ffmpeg 就绪（{self.store.ffmpeg_source()}）：{got}")
            else:
                logger.warning(
                    f"[壁纸工坊] 没找到 ffmpeg，动态壁纸转不了码。"
                    f"手动把 ffmpeg.exe 放进 {BUNDLED_FFMPEG_DIR} 或者 AstrBot 根目录，"
                    f"也可以在插件页面的「上传壁纸」里点自动安装"
                )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error(f"[壁纸工坊] 检查 ffmpeg 出错: {exc}")

    async def initialize(self) -> None:
        raw = self.read_config_raw()
        if not raw:
            raw = dict(self.config) if isinstance(self.config, dict) else {}
        self.config_flat = flatten_config(raw)
        self._sig = json.dumps(raw, sort_keys=True, ensure_ascii=False)
        logger.info(
            f"[壁纸工坊] 已加载，壁纸 {self.store.stats()['total']} 张，"
            f"ffmpeg={'有' if self.store.ffmpeg else '无'}，根目录={self.root}"
        )
        if not self.store.ffmpeg and self.config_flat.get("ffmpeg_auto_install", True):
            self._ff_task = asyncio.create_task(self._ensure_ffmpeg())
        if self.config_flat.get("apply_on_start", True):
            self.apply_now()
        if self.config_flat.get("watch_config", True):
            self._task = asyncio.create_task(self._watch_loop())

    async def terminate(self) -> None:
        self._closed = True
        if self._task:
            self._task.cancel()
            self._task = None
        if self._ff_task:
            self._ff_task.cancel()
            self._ff_task = None

    # ------------------------------------------------------------------ 命令

    @filter.command("壁纸")
    async def wallpaper_command(self, event: AstrMessageEvent):
        """壁纸工坊状态查询与控制。"""
        text = (event.message_str or "").strip()
        arg = text.replace("壁纸", "", 1).strip()
        cfg = self.config_flat
        stats = self.store.stats()

        if arg in {"", "状态", "status"}:
            yield event.plain_result(
                f"壁纸工坊｜库 {stats['total']} 张（动态 {stats['videos']}／静态 {stats['images']}）\n"
                f"模式 {cfg.get('mode')}，轮换集 {cfg.get('active_collection') or '全部'}\n"
                f"遮罩 {cfg.get('veil_opacity')}，模糊 {cfg.get('blur')}px，"
                f"过渡 {cfg.get('fade_ms', 900)}ms，"
                f"字体色 {cfg.get('text_color') or '默认'}"
            )
            return

        if arg in {"刷新", "应用", "reload"}:
            result = self.apply_now()
            yield event.plain_result(
                f"重新应用{'成功' if result.get('applied') else '失败'}："
                f"{result.get('count', 0)} 张壁纸"
                + (f"，{result.get('reason')}" if result.get("reason") else "")
            )
            return

        if arg in {"下一张", "随机"}:
            pool = self.store.pool_for({**cfg, "mode": "random"})
            if not pool:
                yield event.plain_result("壁纸库是空的，先去插件页面上传或导入几张")
                return
            picked = random.choice(pool)
            self.write_config({"rotation": {"mode": "fixed", "fixed_item": picked.get("id")}})
            self.apply_now()
            yield event.plain_result(f"换成「{picked.get('name')}」了，刷新一下页面就能看到")
            return

        if arg in {"开启", "on"}:
            self.write_config({"enabled": True})
            self.apply_now()
            yield event.plain_result("壁纸已开启")
            return

        if arg in {"关闭", "off"}:
            self.write_config({"enabled": False})
            self.apply_now()
            yield event.plain_result("壁纸已关闭，面板恢复原生样式")
            return

        yield event.plain_result(
            "用法：/壁纸 状态｜刷新｜下一张｜开启｜关闭\n"
            "更多设置去 WebUI 插件页面的「壁纸工坊」里调"
        )
