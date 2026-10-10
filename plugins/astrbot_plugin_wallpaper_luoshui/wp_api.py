"""洛水壁纸工坊 - 插件页面的 Web API。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.web import error_response, file_response, json_response, request

from .wp_core import (
    IMAGE_EXTS,
    PLUGIN_NAME,
    VIDEO_EXTS,
    WallpaperStore,
    flatten_config,
    safe_name,
)

CONFIG_GROUPS: dict[str, tuple[str, ...]] = {
    "appearance": (
        "text_color",
        "force_text_color",
        "veil_opacity",
        "blur",
        "background_color",
        "text_shadow_off",
        "ui_text_color",
        "ui_sub_color",
        "fade_ms",
    ),
    "rotation": ("mode", "active_collection", "fixed_item", "switch_interval"),
    "playback": ("enable_video", "video_sound", "video_width", "video_crf", "keep_audio"),
    "source": ("folders", "auto_scan", "recursive"),
    "advanced": ("apply_on_start", "watch_config", "debug_log", "ffmpeg_auto_install"),
}
TOP_LEVEL_KEYS = ("enabled",)


class WallpaperAPI:
    """把插件能力暴露成 Web API，路径前缀 /api/plug/astrbot_plugin_wallpaper_luoshui。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ------------------------------------------------------------ 注册

    def register(self, context) -> None:
        routes = [
            ("/state", self.handle_state, ["GET"]),
            ("/scan", self.handle_scan, ["POST"]),
            ("/import", self.handle_import, ["POST"]),
            ("/upload", self.handle_upload, ["POST"]),
            ("/job", self.handle_job, ["GET"]),
            ("/item/delete", self.handle_delete_item, ["POST"]),
            ("/items/batch", self.handle_batch_items, ["POST"]),
            ("/ffmpeg", self.handle_ffmpeg, ["GET", "POST"]),
            ("/item/update", self.handle_update_item, ["POST"]),
            ("/collection/save", self.handle_save_collection, ["POST"]),
            ("/collection/delete", self.handle_delete_collection, ["POST"]),
            ("/collection/items", self.handle_collection_items, ["POST"]),
            ("/config", self.handle_config, ["GET", "POST"]),
            ("/apply", self.handle_apply, ["POST"]),
            ("/live", self.handle_live, ["GET"]),
            ("/media", self.handle_media, ["GET"]),
            ("/thumb", self.handle_thumb, ["GET"]),
            ("/preview", self.handle_preview, ["GET"]),
        ]
        for route, handler, methods in routes:
            context.register_web_api(
                f"/{PLUGIN_NAME}{route}",
                handler,
                methods,
                f"洛水壁纸工坊 {route}",
            )

    # ------------------------------------------------------------ 工具

    @property
    def store(self) -> WallpaperStore:
        return self.plugin.store

    def _state(self) -> dict:
        items = []
        for item in self.store.items():
            entry = dict(item)
            entry["still_url"] = (
                f"/api/plug/{PLUGIN_NAME}/media?kind=still&id={item.get('id')}"
                if item.get("still")
                else ""
            )
            entry["video_url"] = (
                f"/api/plug/{PLUGIN_NAME}/media?kind=video&id={item.get('id')}"
                if item.get("video")
                else ""
            )
            entry["exists"] = bool(self.store.item_abs(item, "still") or self.store.item_abs(item, "video"))
            items.append(entry)
        cfg = getattr(self.plugin, "config_flat", None) or flatten_config(None)
        return {
            "items": items,
            "collections": self.store.collections(),
            "config": cfg,
            "stats": self.store.stats(),
            "last_apply": getattr(self.plugin, "last_apply", None),
        }

    # ------------------------------------------------------------ 基础接口

    async def handle_state(self):
        return json_response({"success": True, **self._state()})

    async def handle_scan(self):
        payload = await request.json(default={}) or {}
        cfg = getattr(self.plugin, "config_flat", None) or {}
        folders = payload.get("folders") or cfg.get("folders") or []
        if isinstance(folders, str):
            folders = [folders]
        recursive = bool(payload.get("recursive", cfg.get("recursive", True)))
        if not folders:
            return json_response({"success": False, "message": "先填一个壁纸文件夹路径"})
        loop = asyncio.get_running_loop()
        try:
            found = await loop.run_in_executor(None, self.store.scan, list(folders), recursive)
        except Exception as exc:
            logger.error(f"[壁纸工坊] 扫描失败: {exc}")
            return error_response(f"扫描失败：{exc}")
        return json_response({"success": True, "folders": list(folders), "results": found, "count": len(found)})

    async def handle_import(self):
        payload = await request.json(default={}) or {}
        paths = payload.get("paths") or []
        if isinstance(paths, str):
            paths = [paths]
        paths = [str(p) for p in paths if str(p).strip()]
        if not paths:
            return json_response({"success": False, "message": "没有选中任何文件"})
        cfg = getattr(self.plugin, "config_flat", None) or {}
        sound = bool(payload.get("sound", False))
        width = int(payload.get("width") or cfg.get("video_width") or 1920)
        crf = int(payload.get("video_crf") or cfg.get("video_crf") or 27)
        job_id = self.store.jobs.create("导入壁纸", total=len(paths))
        asyncio.create_task(self._run_import(job_id, paths, sound, width, crf))
        return json_response({"success": True, "job_id": job_id})

    async def _run_import(self, job_id: str, paths: list[str], sound: bool, width: int, crf: int) -> None:
        added, failed = [], []
        loop = asyncio.get_running_loop()
        for index, raw in enumerate(paths, start=1):
            path = Path(raw)
            self.store.jobs.update(job_id, current=path.name, done=index - 1)
            if not path.exists():
                failed.append({"path": raw, "error": "文件不存在"})
                continue
            if path.suffix.lower() not in IMAGE_EXTS | VIDEO_EXTS:
                failed.append({"path": raw, "error": "不支持的文件类型"})
                continue
            try:
                item = await loop.run_in_executor(
                    None,
                    lambda p=path: self.store.import_file(
                        p,
                        sound=sound,
                        width=width,
                        crf=crf,
                        origin="scan",
                        job_id=job_id,
                    ),
                )
                added.append(item)
            except Exception as exc:
                logger.error(f"[壁纸工坊] 导入失败 {raw}: {exc}")
                failed.append({"path": raw, "error": str(exc)})
        self.store.jobs.update(job_id, done=len(paths))
        result = self._state()
        self.store.jobs.finish(
            job_id,
            result={"added": added, "failed": failed, "state": result},
            message=f"导入完成：成功 {len(added)}，失败 {len(failed)}",
        )

    async def handle_upload(self):
        try:
            files = await request.files()
        except Exception:
            files = None
        upload = None
        if files is not None:
            upload = files.get("file") or files.get("files")
        form = await request.form()
        if upload is None:
            return json_response({"success": False, "message": "没有收到文件"})
        filename = safe_name(getattr(upload, "filename", "") or "upload.bin", "wallpaper")
        suffix = Path(filename).suffix.lower()
        if suffix not in IMAGE_EXTS | VIDEO_EXTS:
            return json_response({"success": False, "message": f"暂不支持 {suffix or '未知'} 格式"})
        sound = str(form.get("sound", "")).lower() in {"1", "true", "yes", "on"}
        cfg = getattr(self.plugin, "config_flat", None) or {}
        if not sound:
            sound = bool(cfg.get("keep_audio"))
        width = int(cfg.get("video_width") or 1920)
        crf = int(cfg.get("video_crf") or 27)

        target = self.store.uploads_dir / f"{int(time.time())}_{filename}"
        try:
            data = await upload.read()
            target.write_bytes(data)
        except Exception as exc:
            return error_response(f"保存上传文件失败：{exc}")

        job_id = self.store.jobs.create("上传壁纸", total=1)
        asyncio.create_task(self._run_upload(job_id, target, sound, width, crf))
        return json_response({"success": True, "job_id": job_id})

    async def _run_upload(self, job_id: str, target: Path, sound: bool, width: int, crf: int) -> None:
        loop = asyncio.get_running_loop()
        self.store.jobs.update(job_id, current=target.name)
        try:
            item = await loop.run_in_executor(
                None,
                lambda: self.store.import_file(
                    target,
                    sound=sound,
                    width=width,
                    crf=crf,
                    origin="upload",
                    job_id=job_id,
                    move=True,
                ),
            )
            self.store.jobs.finish(
                job_id,
                result={"added": [item], "failed": [], "state": self._state()},
                message=f"已导入 {item.get('name')}",
            )
        except Exception as exc:
            logger.error(f"[壁纸工坊] 上传导入失败: {exc}")
            self.store.jobs.finish(job_id, state="failed", message=str(exc))

    async def handle_job(self):
        job_id = str(request.query.get("id", "")).strip()
        job = self.store.jobs.get(job_id)
        if not job:
            return json_response({"success": False, "message": "任务不存在或已过期"})
        return json_response({"success": True, "job": job})

    async def handle_media(self):
        item_id = str(request.query.get("id", "")).strip()
        kind = str(request.query.get("kind", "still")).strip()
        item = self.store.find_item(item_id)
        if not item:
            return error_response("壁纸不存在")
        path = self.store.item_abs(item, "video" if kind == "video" else "still")
        if not path:
            return error_response("文件不存在")
        content_type = "video/mp4" if kind == "video" else "image/jpeg"
        return file_response(path, content_type=content_type, headers={"Cache-Control": "no-cache"})

    async def handle_thumb(self):
        """缩略图：面板用 sandbox iframe 加载插件页面，<img> 带不上凭据，
        所以这里直接回 base64 data URL，前端一张张填进去。"""
        item_id = str(request.query.get("id", "")).strip()
        raw_path = str(request.query.get("path", "")).strip()
        try:
            width = int(request.query.get("w", "480") or 480)
        except (TypeError, ValueError):
            width = 480
        width = max(120, min(width, 2560))
        import base64

        loop = asyncio.get_running_loop()
        if item_id:
            path = await loop.run_in_executor(None, self.store.make_thumb, item_id, width)
        elif raw_path:
            path = await loop.run_in_executor(None, self.store.make_thumb_for_file, raw_path, width)
        else:
            return json_response({"success": False, "message": "没给要预览的文件"})
        if not path or not Path(path).exists():
            return json_response({"success": False, "message": "没有可用的预览图"})
        try:
            raw = await loop.run_in_executor(None, lambda: Path(path).read_bytes())
        except Exception as exc:
            return error_response(f"读取缩略图失败：{exc}")
        return json_response(
            {
                "success": True,
                "id": item_id,
                "width": width,
                "bytes": len(raw),
                "data_url": "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii"),
            }
        )

    async def handle_preview(self):
        """预览：静态图给 1600 宽的大图，动态壁纸给整个 mp4 的 data URL。"""
        import base64

        item_id = str(request.query.get("id", "")).strip()
        kind = str(request.query.get("kind", "still")).strip()
        item = self.store.find_item(item_id)
        if not item:
            return json_response({"success": False, "message": "壁纸不存在"})
        loop = asyncio.get_running_loop()

        if kind == "video":
            path = self.store.item_abs(item, "video")
            if not path:
                return json_response({"success": False, "message": "这张壁纸没有视频文件"})
            size = path.stat().st_size
            limit = 32 * 1024 * 1024
            if size > limit:
                return json_response(
                    {
                        "success": False,
                        "message": f"视频有 {round(size / 1048576, 1)}MB，预览会卡，直接应用到面板看吧",
                    }
                )
            raw = await loop.run_in_executor(None, lambda: Path(path).read_bytes())
            return json_response(
                {
                    "success": True,
                    "kind": "video",
                    "size_mb": round(size / 1048576, 2),
                    "data_url": "data:video/mp4;base64," + base64.b64encode(raw).decode("ascii"),
                }
            )

        try:
            width = int(request.query.get("w", "1600") or 1600)
        except (TypeError, ValueError):
            width = 1600
        width = max(320, min(width, 3840))
        if item.get("kind") == "video":
            path = await loop.run_in_executor(None, self.store.make_thumb, item_id, width)
        else:
            path = self.store.item_abs(item, "still")
        if not path or not Path(path).exists():
            return json_response({"success": False, "message": "没有可用的预览图"})
        raw = await loop.run_in_executor(None, lambda: Path(path).read_bytes())
        return json_response(
            {
                "success": True,
                "kind": "still",
                "bytes": len(raw),
                "data_url": "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii"),
            }
        )

    async def handle_delete_item(self):
        payload = await request.json(default={}) or {}
        item_id = str(payload.get("id", "")).strip()
        ok = self.store.delete_item(item_id)
        if ok:
            self.plugin.apply_now()
        return json_response({"success": ok, "state": self._state()})

    async def handle_batch_items(self):
        """壁纸库里的批量操作：批量删除、批量加进轮换集。"""
        payload = await request.json(default={}) or {}
        action = str(payload.get("action") or "").strip().lower()
        raw_ids = payload.get("ids") or []
        if isinstance(raw_ids, str):
            raw_ids = [raw_ids]
        ids = [str(i).strip() for i in raw_ids if str(i).strip()]
        if not ids:
            return json_response({"success": False, "message": "还没选中壁纸"})

        if action in {"delete", "remove"}:
            loop = asyncio.get_running_loop()
            done: list[str] = []
            missing: list[str] = []
            for item_id in ids:
                ok = await loop.run_in_executor(None, self.store.delete_item, item_id)
                if ok:
                    done.append(item_id)
                else:
                    missing.append(item_id)
            if done:
                self.plugin.apply_now()
            message = f"删掉 {len(done)} 张" + (f"，{len(missing)} 张没找着" if missing else "")
            return json_response(
                {"success": True, "done": done, "missing": missing, "message": message, "state": self._state()}
            )

        if action in {"collect", "add"}:
            key = str(payload.get("key") or "").strip()
            coll = self.store.collection_by_key(key)
            if not coll:
                return json_response({"success": False, "message": "找不到这个轮换集"})
            have = list(coll.get("items") or [])
            added = 0
            for item_id in ids:
                if item_id not in have:
                    have.append(item_id)
                    added += 1
            coll["items"] = have
            self.store.save_lib()
            message = f"{added} 张加进「{coll.get('name')}」"
            return json_response({"success": True, "added": added, "message": message, "state": self._state()})

        return json_response({"success": False, "message": "不支持这个批量操作"})

    async def handle_ffmpeg(self):
        """检查 / 自动安装 ffmpeg。"""
        payload = await request.json(default={}) or {}
        action = str(payload.get("action") or "").strip().lower()
        store = self.store

        if action in {"install", "download"}:
            if store.refresh_ffmpeg():
                return json_response(
                    {"success": True, "ffmpeg": store.ffmpeg, "source": store.ffmpeg_source(), "message": "已经有 ffmpeg 了", "stats": store.stats()}
                )
            job_id = store.jobs.create("安装 ffmpeg", total=1)
            asyncio.create_task(self._run_ffmpeg_install(job_id))
            return json_response({"success": True, "job_id": job_id})

        found = store.refresh_ffmpeg()
        return json_response(
            {
                "success": True,
                "ffmpeg": found or "",
                "source": store.ffmpeg_source(),
                "message": "ffmpeg 就绪" if found else "还没找到 ffmpeg",
                "stats": store.stats(),
            }
        )

    async def _run_ffmpeg_install(self, job_id: str) -> None:
        store = self.store
        loop = asyncio.get_running_loop()

        def log(message: str) -> None:
            logger.info(f"[壁纸工坊] {message}")
            store.jobs.log(job_id, message)

        store.jobs.log(job_id, "开始检查 ffmpeg")
        try:
            got = await loop.run_in_executor(None, lambda: store.install_ffmpeg(log))
        except Exception as exc:
            logger.error(f"[壁纸工坊] 安装 ffmpeg 失败: {exc}")
            got = None
        store.jobs.update(job_id, done=1)
        store.jobs.finish(
            job_id,
            result={"ffmpeg": got or "", "state": self._state()},
            message="ffmpeg 装好了" if got else "没装上，手动把 ffmpeg.exe 放进插件目录的 bin 文件夹也行",
        )

    async def handle_update_item(self):
        payload = await request.json(default={}) or {}
        item = self.store.find_item(str(payload.get("id", "")))
        if not item:
            return json_response({"success": False, "message": "壁纸不存在"})
        renamed = payload.get("name")
        if isinstance(renamed, str) and renamed.strip():
            item["name"] = safe_name(renamed, item.get("id", "wallpaper"))
        if "sound" in payload:
            item["sound"] = bool(payload.get("sound"))
        if payload.get("note") is not None:
            item["note"] = str(payload.get("note"))
        self.store.save_lib()
        self.plugin.apply_now()
        return json_response({"success": True, "state": self._state()})

    async def handle_save_collection(self):
        payload = await request.json(default={}) or {}
        name = str(payload.get("name", "")).strip()
        key = str(payload.get("key", "")).strip()
        if not name:
            return json_response({"success": False, "message": "集合名字不能为空"})
        coll = self.store.collection_by_key(key)
        if coll:
            coll["name"] = safe_name(name)
        else:
            coll = self.store.add_collection(name)
        self.store.save_lib()
        return json_response({"success": True, "collection": coll, "state": self._state()})

    async def handle_delete_collection(self):
        payload = await request.json(default={}) or {}
        ok = self.store.delete_collection(str(payload.get("key", "")))
        return json_response({"success": ok, "state": self._state()})

    async def handle_collection_items(self):
        payload = await request.json(default={}) or {}
        key = str(payload.get("key", "")).strip()
        items = payload.get("items") or []
        if isinstance(items, str):
            items = [i for i in items.split(",") if i.strip()]
        coll = self.store.set_collection_items(key, [str(i) for i in items])
        if not coll:
            return json_response({"success": False, "message": "集合不存在"})
        self.plugin.apply_now()
        return json_response({"success": True, "collection": coll, "state": self._state()})

    # ------------------------------------------------------------ 配置

    def _split_patch(self, patch: dict) -> dict:
        """把页面传来的扁平配置映射回分组结构（只保留 schema 里存在的键）。"""
        grouped: dict[str, dict] = {}
        flat = flatten_config(self.plugin.read_config_raw())
        for key, value in (patch or {}).items():
            if key in TOP_LEVEL_KEYS:
                flat[key] = value
                continue
            for group, keys in CONFIG_GROUPS.items():
                if key in keys:
                    grouped.setdefault(group, {})[key] = value
                    flat[key] = value
                    break
        for group, keys in CONFIG_GROUPS.items():
            payload = grouped.get(group)
            if not payload:
                continue
            for key in keys:
                if key in flat and key not in payload:
                    payload[key] = flat[key]
        return grouped

    async def handle_config(self):
        if request.method == "GET":
            return json_response({"success": True, "config": getattr(self.plugin, "config_flat", None)})
        payload = await request.json(default={}) or {}
        patch = payload.get("config") if isinstance(payload.get("config"), dict) else payload
        patch = {k: v for k, v in (patch or {}).items() if v is not None}
        if not patch:
            return json_response({"success": False, "message": "没有要保存的配置"})
        grouped = self._split_patch(patch)
        if not grouped:
            return json_response({"success": False, "message": "没有可识别的配置项"})
        ok = self.plugin.write_config(grouped)
        if not ok:
            return error_response("写入插件配置失败")
        result = self.plugin.apply_now()
        return json_response({"success": True, "applied": result, "config": self.plugin.config_flat})

    async def handle_apply(self):
        payload = await request.json(default={}) or {}
        if isinstance(payload.get("config"), dict):
            grouped = self._split_patch(payload["config"])
            if grouped:
                self.plugin.write_config(grouped)
        result = self.plugin.apply_now()
        return json_response({"success": True, "applied": result, "state": self._state()})

    async def handle_live(self):
        """面板页里注入的脚本靠这个接口做实时换壁纸，不用刷新页面。"""
        snap = self.store.live_snapshot()
        return json_response({"success": True, **snap})
