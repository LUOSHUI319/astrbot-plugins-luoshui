"""洛水壁纸工坊 - 核心逻辑。

这里尽量只依赖标准库，方便单独测试：
- 壁纸库（library.json）的读写
- 扫描壁纸文件夹（兼容 Wallpaper Engine 创意工坊目录）
- 调用 ffmpeg 转码成网页能放的 mp4、抽静态帧
- 把选中的壁纸发布到 AstrBot 的 data/dist 下
- 生成并写入注入面板的 HTML 代码块
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterable

PLUGIN_NAME = "astrbot_plugin_wallpaper_luoshui"
DISPLAY_NAME = "洛水壁纸工坊"

PLUGIN_DIR = Path(__file__).resolve().parent
BUNDLED_FFMPEG_DIR = PLUGIN_DIR / "bin"

# 插件包里没带 ffmpeg 时（比如用 git 装的）、机器上也没有时，按顺序试这几个源自动装一份
FFMPEG_DOWNLOAD_URLS = (
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
)

MARK_START = "<!-- astrbot-wallpaper:start -->"
MARK_END = "<!-- astrbot-wallpaper:end -->"
PUB_PREFIX = "astrwp_"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".avif"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v", ".mkv"}
SKIP_NAME_RE = re.compile(r"^(preview|thumbnail|thumb|icon|\.)", re.IGNORECASE)
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "text_color": "",
    "force_text_color": False,
    "veil_opacity": 0.10,
    "blur": 0,
    "background_color": "#080a0f",
    "text_shadow_off": True,
    "ui_text_color": "",
    "ui_sub_color": "",
    "fade_ms": 900,
    "mode": "random",
    "active_collection": "",
    "fixed_item": "",
    "switch_interval": 0,
    "enable_video": True,
    "video_sound": False,
    "video_width": 1920,
    "video_crf": 27,
    "keep_audio": False,
    "folders": [],
    "auto_scan": False,
    "recursive": True,
    "apply_on_start": True,
    "watch_config": True,
    "debug_log": False,
    "ffmpeg_auto_install": True,
}

COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


# --------------------------------------------------------------------------- 配置

def flatten_config(raw: dict | None) -> dict[str, Any]:
    """把分组配置拍平成一层的键值，顺带补默认值。"""
    flat: dict[str, Any] = {}
    stack: list[dict] = [raw or {}]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            if isinstance(value, dict) and not isinstance(value, list):
                stack.append(value)
            else:
                flat[key] = value
    for key, value in DEFAULTS.items():
        if flat.get(key) is None:
            flat[key] = value
    return flat


def load_config_file(path: Path) -> dict[str, Any]:
    try:
        return flatten_config(json.loads(Path(path).read_text(encoding="utf-8")))
    except Exception:
        return flatten_config(None)


def safe_name(text: str, fallback: str = "wallpaper") -> str:
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", str(text or "")).strip()
    name = re.sub(r"\s+", " ", name)
    return name[:60] or fallback


def _run(cmd: list[str], timeout: int = 1800) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        return proc.returncode, (proc.stderr or "") + (proc.stdout or "")
    except FileNotFoundError:
        return 127, "ffmpeg 未找到"
    except subprocess.TimeoutExpired:
        return 124, "处理超时"
    except Exception as exc:  # pragma: no cover
        return 1, str(exc)


# --------------------------------------------------------------------------- 任务

class JobManager:
    """导入/转码这类耗时操作丢进后台任务，页面轮询进度。"""

    def __init__(self) -> None:
        self._jobs: dict[str, dict] = {}
        self._lock = threading.RLock()

    def create(self, title: str, total: int = 1) -> str:
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._jobs[job_id] = {
                "id": job_id,
                "title": title,
                "state": "running",
                "total": total,
                "done": 0,
                "current": "",
                "message": "",
                "logs": [],
                "result": None,
                "created_at": time.time(),
            }
            self._prune()
        return job_id

    def update(self, job_id: str, **kwargs: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.update(kwargs)

    def log(self, job_id: str, message: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job["logs"].append(f"{time.strftime('%H:%M:%S')} {message}")
            del job["logs"][:-120]

    def finish(self, job_id: str, result: Any = None, state: str = "done", message: str = "") -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job["state"] = state
            job["result"] = result
            job["message"] = message
            job["finished_at"] = time.time()

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return json.loads(json.dumps(job)) if job else None

    def _prune(self) -> None:
        if len(self._jobs) < 40:
            return
        for job_id, job in sorted(self._jobs.items(), key=lambda kv: kv[1]["created_at"])[:10]:
            if job.get("state") != "running":
                self._jobs.pop(job_id, None)


# --------------------------------------------------------------------------- 主类

def download_ffmpeg(target_dir: Path | None = None, log: Callable[[str], None] | None = None) -> Path | None:
    """本机没有 ffmpeg 时，下一份静态编译版丢到插件包的 bin 目录里。

    只在 Windows 上做（插件面向的 AstrBot 面板基本都在 Windows）。返回 ffmpeg.exe 的路径，
    失败就返回 None，让调用方去提示用户手动放。
    """

    def say(message: str) -> None:
        if not log:
            return
        try:
            log(message)
        except Exception:
            pass

    if os.name != "nt":
        say("不是 Windows 系统，跳过自动安装 ffmpeg")
        return None

    target = Path(target_dir) if target_dir else BUNDLED_FFMPEG_DIR
    exe = target / "ffmpeg.exe"
    try:
        if exe.exists() and exe.stat().st_size > 1024 * 1024:
            return exe
    except OSError:
        pass

    try:
        target.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        say(f"建不了 {target}：{exc}")
        return None

    reason = ""
    with tempfile.TemporaryDirectory(prefix="astrwp_ffmpeg_") as tmp:
        tmp_dir = Path(tmp)
        for url in FFMPEG_DOWNLOAD_URLS:
            try:
                say(f"开始下载 ffmpeg：{url}")
                zip_path = tmp_dir / "ffmpeg.zip"
                req = urllib.request.Request(url, headers={"User-Agent": "AstrBot-WallpaperStudio"})
                with urllib.request.urlopen(req, timeout=90) as resp, open(zip_path, "wb") as fh:
                    shutil.copyfileobj(resp, fh, 1024 * 256)
                if zip_path.stat().st_size < 1024 * 1024:
                    reason = "下载到的文件不完整"
                    continue
                say("下载完成，正在解压")
                with zipfile.ZipFile(zip_path) as zf:
                    member = next(
                        (
                            name
                            for name in zf.namelist()
                            if name.replace("\\", "/").lower().endswith("bin/ffmpeg.exe")
                        ),
                        "",
                    )
                    if not member:
                        reason = "压缩包里没有 ffmpeg.exe"
                        continue
                    tmp_exe = tmp_dir / "ffmpeg.exe"
                    with zf.open(member) as src, open(tmp_exe, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 256)
                if tmp_exe.stat().st_size < 1024 * 1024:
                    reason = "解出来的 ffmpeg 不完整"
                    continue
                if exe.exists():
                    exe.unlink()
                tmp_exe.replace(exe)
                say(f"ffmpeg 已就位：{exe}")
                return exe
            except Exception as exc:
                reason = str(exc)
                say(f"这一路失败了：{exc}")
                continue
    say(f"自动装 ffmpeg 没成功（{reason or '原因不明'}），手动把 ffmpeg.exe 放到 {target} 也行")
    return None


class WallpaperStore:
    def __init__(self, base_dir: Path, astrbot_root: Path, jobs: JobManager | None = None) -> None:
        self.base = Path(base_dir)
        self.root = Path(astrbot_root)
        self.videos_dir = self.base / "media" / "videos"
        self.stills_dir = self.base / "media" / "stills"
        self.uploads_dir = self.base / "media" / "uploads"
        self.lib_path = self.base / "library.json"

        self.dist = self.root / "data" / "dist"
        self.index_html = self.dist / "index.html"
        self.pub_still_dir = self.dist / "bg"
        self.pub_video_dir = self.dist / "bg" / "video"

        self.ffmpeg = self._find_ffmpeg()
        self.jobs = jobs or JobManager()
        self._lock = threading.RLock()

        for folder in (self.videos_dir, self.stills_dir, self.uploads_dir, self.pub_still_dir, self.pub_video_dir):
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

        self.lib = self._load_lib()
        self._last_live: dict = {}

    # ---------------------------------------------------------------- 基础

    def _find_ffmpeg(self) -> str | None:
        exe_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        candidates = [
            BUNDLED_FFMPEG_DIR / exe_name,
            BUNDLED_FFMPEG_DIR / "ffmpeg.exe",
            self.base / "bin" / exe_name,
            self.root / "ffmpeg.exe",
            self.root / "bin" / exe_name,
            self.root / "data" / "ffmpeg.exe",
        ]
        for cand in candidates:
            try:
                if cand.exists() and cand.stat().st_size > 1024:
                    return str(cand)
            except OSError:
                continue
        found = shutil.which("ffmpeg")
        return found or None

    def ffmpeg_source(self) -> str:
        """ffmpeg 是从哪来的，面板上要显示给用户看。"""
        if not self.ffmpeg:
            return ""
        path = str(self.ffmpeg).lower()
        for prefix, label in (
            (str(BUNDLED_FFMPEG_DIR).lower(), "插件内置"),
            (str(self.base).lower(), "插件数据目录"),
            (str(self.root).lower(), "AstrBot 根目录"),
        ):
            if path.startswith(prefix):
                return label
        return "系统 PATH"

    def refresh_ffmpeg(self) -> str | None:
        self.ffmpeg = self._find_ffmpeg()
        return self.ffmpeg

    def install_ffmpeg(self, log: Callable[[str], None] | None = None) -> str | None:
        """首次装插件时调一次：有就直接用，没有就自动装一份到插件包的 bin 目录。"""
        if self.refresh_ffmpeg():
            if log:
                log(f"已经有 ffmpeg 了：{self.ffmpeg}")
            return self.ffmpeg
        got = download_ffmpeg(BUNDLED_FFMPEG_DIR, log=log)
        if got:
            self.ffmpeg = str(got)
        return self.ffmpeg

    def _load_lib(self) -> dict:
        try:
            data = json.loads(self.lib_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        lib = {
            "items": data.get("items") if isinstance(data.get("items"), list) else [],
            "collections": data.get("collections") if isinstance(data.get("collections"), list) else [],
        }
        have = {c.get("id") for c in lib["collections"] if isinstance(c, dict)}
        if "default" not in have:
            lib["collections"].insert(0, {"id": "default", "name": "默认", "items": [], "created_at": time.time()})
        return lib

    def save_lib(self) -> None:
        with self._lock:
            tmp = self.lib_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.lib, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.lib_path)

    # ---------------------------------------------------------------- 缩略图 / 预览

    def make_thumb(self, item_id: str, width: int = 480) -> Path | None:
        """给一张壁纸生成（或复用）缩略图，返回本地路径。"""
        item = self.find_item(item_id)
        if not item:
            return None
        width = max(120, min(int(width or 480), 2560))
        cache_dir = self.base / "thumbs"
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None
        dst = cache_dir / f"{item.get('id')}_w{width}.jpg"
        if dst.exists() and dst.stat().st_size > 512:
            return dst
        src = self.item_abs(item, "still") or self.item_abs(item, "video")
        if not src:
            return None
        if self.ffmpeg:
            cmd = [
                self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(src), "-frames:v", "1",
                "-vf", f"scale={width}:-2:flags=lanczos",
                "-q:v", "5", str(dst),
            ]
            code, _ = _run(cmd, timeout=180)
            if code == 0 and dst.exists() and dst.stat().st_size > 512:
                return dst
        # 没有 ffmpeg 就直接用原图顶着
        if item.get("kind") != "video":
            return self.item_abs(item, "still")
        return None

    def make_thumb_for_file(self, raw: str, width: int = 480) -> Path | None:
        """给来源文件夹里的图片/视频直接出缩略图（不入库），扫描结果也能预览。"""
        try:
            src = Path(str(raw)).expanduser()
        except Exception:
            return None
        if not src.is_file():
            return None
        ext = src.suffix.lower()
        if ext not in IMAGE_EXTS and ext not in VIDEO_EXTS:
            return None
        width = max(120, min(int(width or 480), 2560))
        cache_dir = self.base / "thumbs"
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None
        try:
            key = uuid.uuid5(uuid.NAMESPACE_URL, str(src.resolve()).lower()).hex[:16]
        except Exception:
            key = uuid.uuid5(uuid.NAMESPACE_URL, str(src).lower()).hex[:16]
        dst = cache_dir / f"src_{key}_w{width}.jpg"
        if dst.exists() and dst.stat().st_size > 512:
            return dst
        if self.ffmpeg:
            cmd = [
                self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(src), "-frames:v", "1",
                "-vf", f"scale={width}:-2:flags=lanczos",
                "-q:v", "5", str(dst),
            ]
            code, _ = _run(cmd, timeout=180)
            if code == 0 and dst.exists() and dst.stat().st_size > 512:
                return dst
        if ext in IMAGE_EXTS:
            return src
        return None

    def clear_thumbs(self, item_id: str | None = None) -> None:
        cache_dir = self.base / "thumbs"
        try:
            patterns = f"{item_id}_w*.jpg" if item_id else "*_w*.jpg"
            for path in cache_dir.glob(patterns):
                path.unlink(missing_ok=True)
        except Exception:
            pass

    # ---------------------------------------------------------------- 库

    def items(self) -> list[dict]:
        return list(self.lib.get("items") or [])

    def collections(self) -> list[dict]:
        return list(self.lib.get("collections") or [])

    def find_item(self, key: str) -> dict | None:
        if not key:
            return None
        key = str(key).strip()
        for item in self.items():
            if key in {item.get("id"), item.get("name")}:
                return item
        lowered = key.lower()
        for item in self.items():
            if str(item.get("name", "")).lower() == lowered:
                return item
        return None

    def item_abs(self, item: dict, kind: str) -> Path | None:
        rel = item.get(kind)
        if not rel:
            return None
        path = (self.base / rel).resolve()
        return path if path.exists() else None

    # ---------------------------------------------------------------- 探测 / 转码

    def probe(self, path: Path) -> dict:
        if not self.ffmpeg:
            return {}
        code, out = _run([self.ffmpeg, "-hide_banner", "-i", str(path)], timeout=60)
        info: dict[str, Any] = {}
        match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", out)
        if match:
            info["duration"] = round(
                int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3)), 2
            )
        match = re.search(r"Video: (\w+).*?(\d{2,5})x(\d{2,5})", out)
        if match:
            info["codec"] = match.group(1)
            info["width"] = int(match.group(2))
            info["height"] = int(match.group(3))
        if "Audio:" in out:
            info["has_audio"] = True
        return info

    def transcode(self, src: Path, dst: Path, width: int = 1920, crf: int = 27, keep_audio: bool = False) -> tuple[bool, str]:
        if not self.ffmpeg:
            return False, "没找到 ffmpeg，请把 ffmpeg.exe 放到插件目录的 bin 文件夹或 AstrBot 根目录"
        width = max(480, min(int(width or 1920), 3840))
        crf = max(14, min(int(crf or 27), 40))
        dst.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src),
        ]
        if keep_audio:
            cmd += ["-c:a", "aac", "-b:a", "128k"]
        else:
            cmd += ["-an"]
        cmd += [
            "-vf", f"scale={width}:-2:flags=lanczos",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
            "-movflags", "+faststart",
            str(dst),
        ]
        code, out = _run(cmd)
        if code != 0 or not dst.exists() or dst.stat().st_size < 1024:
            return False, (out or "转码失败")[-400:]
        return True, ""

    def make_still(self, src: Path, dst: Path, width: int = 1920, seek: float = 1.0) -> bool:
        if not self.ffmpeg:
            return False
        dst.parent.mkdir(parents=True, exist_ok=True)
        for at in (seek, 0.0):
            cmd = [
                self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", str(at), "-i", str(src), "-frames:v", "1",
                "-vf", f"scale={max(480, int(width))}:-2:flags=lanczos",
                "-q:v", "3", str(dst),
            ]
            code, _ = _run(cmd, timeout=300)
            if code == 0 and dst.exists() and dst.stat().st_size > 1024:
                return True
        return False

    def normalize_image(self, src: Path, dst: Path, width: int = 2560) -> bool:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if self.ffmpeg:
            cmd = [
                self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(src),
                "-vf", f"scale='min({max(480, int(width))},iw)':-2:flags=lanczos",
                "-q:v", "3", str(dst),
            ]
            code, _ = _run(cmd, timeout=300)
            if code == 0 and dst.exists() and dst.stat().st_size > 1024:
                return True
        try:
            shutil.copyfile(src, dst)
            return dst.exists()
        except Exception:
            return False

    # ---------------------------------------------------------------- 导入

    def import_file(
        self,
        src: Path,
        *,
        name: str | None = None,
        sound: bool = False,
        width: int = 1920,
        crf: int = 27,
        origin: str = "scan",
        move: bool = False,
        job_id: str | None = None,
        still_width: int = 1920,
    ) -> dict:
        src = Path(src)
        item_id = "w_" + uuid.uuid4().hex[:10]
        title = safe_name(name or src.stem, item_id)
        ext = src.suffix.lower()
        item: dict[str, Any] = {
            "id": item_id,
            "name": title,
            "kind": "video" if ext in VIDEO_EXTS else "image",
            "origin": origin,
            "source": str(src),
            "sound": bool(sound),
            "added_at": time.time(),
        }
        if job_id:
            self.jobs.log(job_id, f"开始处理 {src.name}")

        if item["kind"] == "video":
            info = self.probe(src)
            item.update({k: info.get(k) for k in ("width", "height", "duration", "codec") if info.get(k)})
            item["has_audio"] = bool(info.get("has_audio"))
            video_rel = Path("media") / "videos" / f"{item_id}.mp4"
            video_abs = self.base / video_rel
            ok, err = self.transcode(src, video_abs, width=width, crf=crf, keep_audio=bool(sound))
            if not ok:
                raise RuntimeError(f"转码失败：{err}")
            item["video"] = str(video_rel).replace("\\", "/")
            still_rel = Path("media") / "stills" / f"{item_id}.jpg"
            still_abs = self.base / still_rel
            if self.make_still(video_abs, still_abs, width=still_width) or self.make_still(src, still_abs, width=still_width):
                item["still"] = str(still_rel).replace("\\", "/")
            item["size"] = round(video_abs.stat().st_size / 1048576, 2)
        else:
            still_rel = Path("media") / "stills" / f"{item_id}.jpg"
            still_abs = self.base / still_rel
            if not self.normalize_image(src, still_abs):
                raise RuntimeError("静态图处理失败")
            item["still"] = str(still_rel).replace("\\", "/")
            item["kind"] = "image"
            item["size"] = round(still_abs.stat().st_size / 1048576, 2)
            info = self.probe(still_abs)
            item.update({k: info.get(k) for k in ("width", "height") if info.get(k)})

        with self._lock:
            self.lib["items"].append(item)
            default = next((c for c in self.lib["collections"] if c.get("id") == "default"), None)
            if default is not None and item_id not in default["items"]:
                default["items"].append(item_id)
        self.save_lib()

        if move:
            try:
                src.unlink()
            except Exception:
                pass
        if job_id:
            self.jobs.log(job_id, f"完成 {title}")
        return item

    # ---------------------------------------------------------------- 扫描

    def scan(self, folders: Iterable[str], recursive: bool = True, limit: int = 400) -> list[dict]:
        results: list[dict] = []
        seen: set[str] = set()
        for raw in folders or []:
            folder = Path(str(raw).strip().strip('"'))
            if not folder.exists() or not folder.is_dir():
                continue
            walker = folder.rglob("*") if recursive else folder.glob("*")
            for path in walker:
                try:
                    if not path.is_file():
                        continue
                except OSError:
                    continue
                ext = path.suffix.lower()
                if ext not in IMAGE_EXTS and ext not in VIDEO_EXTS:
                    continue
                if SKIP_NAME_RE.match(path.stem) or path.stem.lower() in {"preview", "thumbnail"}:
                    continue
                key = str(path).lower()
                if key in seen:
                    continue
                seen.add(key)
                title = self._title_of(path)
                try:
                    stat = path.stat()
                except OSError:
                    continue
                results.append(
                    {
                        "path": str(path),
                        "name": title,
                        "file": path.name,
                        "folder": str(path.parent),
                        "kind": "video" if ext in VIDEO_EXTS else "image",
                        "size": round(stat.st_size / 1048576, 2),
                        "mtime": int(stat.st_mtime),
                        "imported": any(str(i.get("source", "")).lower() == key for i in self.items()),
                    }
                )
                if len(results) >= limit:
                    return results
        results.sort(key=lambda r: (r["kind"] != "video", r["name"].lower()))
        return results

    @staticmethod
    def _title_of(path: Path) -> str:
        """Wallpaper Engine 的项目信息里带标题，顺手读一下。"""
        project = path.parent / "project.json"
        try:
            if project.exists():
                data = json.loads(project.read_text(encoding="utf-8", errors="ignore"))
                title = str(data.get("title") or "").strip()
                if title:
                    return safe_name(title, path.stem)
        except Exception:
            pass
        return safe_name(path.stem, "壁纸")

    # ---------------------------------------------------------------- 集合

    def collection_by_key(self, key: str) -> dict | None:
        if not key:
            return None
        key = str(key).strip()
        for coll in self.collections():
            if key in {coll.get("id"), coll.get("name")}:
                return coll
        return None

    def add_collection(self, name: str) -> dict:
        name = safe_name(name, "新集合")
        exist = self.collection_by_key(name)
        if exist:
            return exist
        coll = {"id": "c_" + uuid.uuid4().hex[:8], "name": name, "items": [], "created_at": time.time()}
        self.lib["collections"].append(coll)
        self.save_lib()
        return coll

    def delete_collection(self, key: str) -> bool:
        coll = self.collection_by_key(key)
        if not coll or coll.get("id") == "default":
            return False
        self.lib["collections"] = [c for c in self.collections() if c.get("id") != coll.get("id")]
        self.save_lib()
        return True

    def set_collection_items(self, key: str, item_ids: list[str]) -> dict | None:
        coll = self.collection_by_key(key)
        if not coll:
            return None
        valid = {item.get("id") for item in self.items()}
        coll["items"] = [str(i) for i in item_ids if str(i) in valid]
        self.save_lib()
        return coll

    # ---------------------------------------------------------------- 删除

    def delete_item(self, item_id: str) -> bool:
        item = self.find_item(item_id)
        if not item:
            return False
        with self._lock:
            self.lib["items"] = [i for i in self.items() if i.get("id") != item.get("id")]
            for coll in self.collections():
                coll["items"] = [i for i in coll.get("items", []) if i != item.get("id")]
        for kind in ("video", "still"):
            rel = item.get(kind)
            if rel:
                try:
                    (self.base / rel).unlink(missing_ok=True)
                except Exception:
                    pass
        self.clear_thumbs(item.get("id"))
        self.save_lib()
        return True

    # ---------------------------------------------------------------- 应用

    def pool_for(self, cfg: dict) -> list[dict]:
        items = self.items()
        collection = self.collection_by_key(str(cfg.get("active_collection") or ""))
        if collection is not None:
            ids = [str(i) for i in collection.get("items", [])]
            items = [i for i in items if i.get("id") in ids]
        mode = str(cfg.get("mode") or "random").lower()
        if mode == "fixed":
            fixed = self.find_item(str(cfg.get("fixed_item") or ""))
            if fixed:
                items = [fixed]
        pool = []
        for item in items:
            if item.get("kind") == "video" and not cfg.get("enable_video", True):
                if self.item_abs(item, "still"):
                    pool.append(item)
                continue
            pool.append(item)
        return pool

    def publish(self, pool: list[dict]) -> tuple[list[dict], list[str]]:
        """把壁纸库里的文件复制到 data/dist 下，返回 (可用的壁纸列表, 需要清理的旧文件名)。"""
        published: list[dict] = []
        keep: set[str] = set()
        for item in pool:
            item_id = item.get("id")
            still_src = self.item_abs(item, "still")
            video_src = self.item_abs(item, "video")
            if not still_src and not video_src:
                continue
            still_name = f"{PUB_PREFIX}{item_id}.jpg"
            video_name = f"{PUB_PREFIX}{item_id}.mp4"
            if still_src:
                dst = self.pub_still_dir / still_name
                self._copy_if_needed(still_src, dst)
                keep.add(still_name)
            if video_src:
                dst = self.pub_video_dir / video_name
                self._copy_if_needed(video_src, dst)
                keep.add(video_name)
            published.append(
                {
                    "id": item_id,
                    "name": item.get("name") or item_id,
                    "still": f"/bg/{still_name}" if still_src else "",
                    "video": f"/bg/video/{video_name}" if video_src else "",
                    "sound": bool(item.get("sound")),
                }
            )
        stale: list[str] = []
        for folder in (self.pub_still_dir, self.pub_video_dir):
            try:
                for path in folder.glob(f"{PUB_PREFIX}*"):
                    if path.name not in keep:
                        stale.append(path.name)
            except Exception:
                pass
        return published, stale

    @staticmethod
    def _copy_if_needed(src: Path, dst: Path) -> None:
        try:
            if dst.exists() and dst.stat().st_size == src.stat().st_size and int(dst.stat().st_mtime) >= int(src.stat().st_mtime):
                return
            shutil.copy2(src, dst)
        except Exception:
            pass

    def cleanup_published(self) -> list[str]:
        removed = []
        for folder in (self.pub_still_dir, self.pub_video_dir):
            try:
                for path in folder.glob(f"{PUB_PREFIX}*"):
                    try:
                        path.unlink()
                        removed.append(path.name)
                    except Exception:
                        pass
            except Exception:
                pass
        return removed

    # ---------------------------------------------------------------- 注入

    def build_block(self, cfg: dict, published: list[dict]) -> str:
        veil = max(0.0, min(float(cfg.get("veil_opacity", 0.1) or 0.0), 0.95))
        blur = max(0, min(int(cfg.get("blur", 0) or 0), 60))
        base_color = str(cfg.get("background_color") or "#080a0f")
        if not COLOR_RE.match(base_color):
            base_color = "#080a0f"
        text_color = str(cfg.get("text_color") or "").strip()
        if not COLOR_RE.match(text_color):
            text_color = ""
        interval = max(0, int(cfg.get("switch_interval", 0) or 0))
        mode = str(cfg.get("mode") or "random").lower()
        if mode not in {"random", "sequence", "fixed"}:
            mode = "random"
        sound = bool(cfg.get("video_sound")) and any(p.get("sound") for p in published)
        scale = 1.0 + min(blur / 200.0, 0.18)
        try:
            fade_ms = int(cfg.get("fade_ms", 900))
        except Exception:
            fade_ms = 900
        fade_ms = max(0, min(fade_ms, 5000))

        text_rules = ""
        if text_color:
            if cfg.get("force_text_color"):
                text_rules += f"\n  .v-application, .v-application * {{ color: var(--astr-wp-text, unset) !important; }}"
            else:
                text_rules += (
                    "\n  .v-application :where(h1,h2,h3,h4,h5,h6,p,span,a,label,small,strong,li,td,th,"
                    ".v-label,.v-messages__message,.v-list-item-title,.v-list-item-subtitle,.v-card-title,"
                    ".v-card-subtitle,.v-card-text,.text-body-1,.text-body-2,.text-caption,.text-h5,.text-h6)"
                    f" {{ color: var(--astr-wp-text, unset) !important; }}"
                )
        if cfg.get("text_shadow_off", True):
            text_rules += "\n  .v-application, .v-application * { text-shadow: none !important; }"

        live_items = [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "still": p.get("still"),
                "video": p.get("video"),
                "sound": bool(p.get("sound")),
            }
            for p in published
        ]
        live_core = {
            "mode": mode,
            "interval": interval,
            "sound": sound,
            "fade": fade_ms,
            "veil": round(veil, 3),
            "blur": blur,
            "base": base_color,
            "text_color": text_color,
            "force": bool(cfg.get("force_text_color")),
            "items": live_items,
        }
        live_hash = hashlib.md5(
            json.dumps(live_core, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:12]
        self._last_live = {
            "hash": live_hash,
            "enabled": True,
            "count": len(live_items),
            **live_core,
        }

        payload = {
            "live": True,
            "hash": live_hash,
            "items": published,
            "mode": mode,
            "interval": interval,
            "sound": sound,
            "fade": fade_ms,
            "title": DISPLAY_NAME,
        }
        payload_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

        return f"""{MARK_START}
<style id="astrbot-wallpaper-style">
  :root {{ --astr-wp: none; --astr-wp-veil: rgba(255, 255, 255, {veil:.3f}); --astr-wp-blur: {blur}px; --astr-wp-base: {base_color}; --astr-wp-text: {text_color or 'unset'}; --astr-wp-fade: {fade_ms}ms; }}
  html {{ background-color: var(--astr-wp-base) !important; }}
  body {{ background-color: transparent !important; background-image: none !important; }}
  #astr-wp-stack {{
    position: fixed; inset: 0; z-index: -4; pointer-events: none; overflow: hidden;
    filter: blur(var(--astr-wp-blur)); transform: scale({scale:.4f});
  }}
  #astr-wp-stack > .astr-wp-still {{
    position: absolute; inset: 0;
    background-image: var(--astr-wp);
    background-size: cover; background-position: center center; background-repeat: no-repeat;
    opacity: 0; transition: opacity var(--astr-wp-fade) ease-in-out; will-change: opacity;
  }}
  #astr-wp-stack > .astr-wp-still.astr-wp-visible {{ opacity: 1; }}
  #astr-wp-vstack {{
    position: fixed; inset: 0; z-index: -3; pointer-events: none; overflow: hidden;
    filter: blur(var(--astr-wp-blur)); transform: scale({scale:.4f});
  }}
  #astr-wp-vstack > video {{
    position: absolute; inset: 0; width: 100%; height: 100%;
    object-fit: cover; display: block; pointer-events: none;
    opacity: 0; transition: opacity var(--astr-wp-fade) ease-in-out; will-change: opacity;
  }}
  #astr-wp-vstack > video.astr-wp-on {{ opacity: 1; }}
  #astr-wp-veil {{
    position: fixed; inset: 0; z-index: -2; pointer-events: none;
    background-color: var(--astr-wp-veil);
  }}
  #astr-wp-sound {{
    position: fixed; right: 16px; bottom: 16px; z-index: 9999;
    display: none; align-items: center; gap: 6px;
    padding: 6px 12px; border-radius: 999px; cursor: pointer;
    font-size: 12px; color: #fff; background: rgba(0, 0, 0, 0.45);
    border: 1px solid rgba(255, 255, 255, 0.28); backdrop-filter: blur(6px);
  }}
  #astr-wp-sound.astr-wp-show {{ display: inline-flex; }}
  #app, .v-application, .v-application__wrap, .v-layout, .v-main, .v-main__wrap, .v-container, .v-row, .v-col,
  .v-sheet, .v-card, .v-list, .v-table, .v-window, .v-window__container, .v-expansion-panel, .v-expansion-panels,
  .v-tabs, .v-tab, .v-item-group, .v-footer, .v-toolbar, .v-app-bar, .v-navigation-drawer, .v-toolbar__content,
  .v-toolbar__extension, [class$="-page-container"], [class*="-card"], [class*="-panel"] {{
    background-color: transparent !important;
    background-image: none !important;
    backdrop-filter: none !important;
    -webkit-backdrop-filter: none !important;
  }}
  .v-overlay .v-card, .v-dialog .v-card, .v-dialog .v-sheet, .v-menu .v-list, .v-select__content .v-list,
  .v-snackbar__wrapper, .v-navigation-drawer--temporary, .v-navigation-drawer__content {{
    background-color: rgba(var(--v-theme-surface), 0.80) !important;
  }}
  .v-field, .v-input__slot {{ background-color: rgba(var(--v-theme-surface), 0.06) !important; }}
  .v-btn {{ background-color: rgba(var(--v-theme-surface), 0.10) !important; }}
  .v-chip {{ background-color: rgba(var(--v-theme-surface), 0.08) !important; }}{text_rules}
</style>
<script id="astrbot-wallpaper-script">
(function () {{
  var CFG = {payload_json};
  var items = CFG.items || [];
  window.ASTRBOT_WALLPAPER = CFG;
  if (!Array.isArray(items)) items = [];
  var KEY = 'astrbot-wallpaper-plugin-index';
  var SEQ = 'astrbot-wallpaper-plugin-seq';
  var current = -1;

  function pickIndex(step) {{
    if (CFG.mode === 'sequence') {{
      var n = parseInt(sessionStorage.getItem(SEQ) || '0', 10);
      if (isNaN(n)) n = 0;
      if (step) {{ n = n + 1; sessionStorage.setItem(SEQ, String(n)); }}
      return ((n % items.length) + items.length) % items.length;
    }}
    if (CFG.mode === 'fixed') return 0;
    if (step) {{
      var last = sessionStorage.getItem(KEY);
      var idx = Math.floor(Math.random() * items.length);
      if (items.length > 1 && String(idx) === last) idx = (idx + 1) % items.length;
      sessionStorage.setItem(KEY, String(idx));
      return idx;
    }}
    var stored = sessionStorage.getItem(KEY);
    if (stored === null || isNaN(parseInt(stored, 10))) {{
      stored = String(Math.floor(Math.random() * items.length));
      sessionStorage.setItem(KEY, stored);
    }}
    return parseInt(stored, 10) % items.length;
  }}

  function clearSurfaces() {{
    try {{
      var root = document.querySelector('.v-application');
      if (!root) return;
      var surf = getComputedStyle(root).getPropertyValue('--v-theme-surface').trim();
      var m = surf.match(/(\\d+)\\D+(\\d+)\\D+(\\d+)/);
      if (!m) return;
      var sr = +m[1], sg = +m[2], sb = +m[3];
      var skip = '#astr-wp-stack, #astr-wp-vstack, #astr-wp-stage, #astr-wp-video, #astr-wp-veil, #astr-wp-sound, .astr-wp-still, .v-overlay, .v-dialog, .v-menu, .v-tooltip, .v-snackbar, .v-btn, .v-field, .v-icon, input, textarea, select, .v-progress-linear, .v-slider, .v-switch, canvas, svg, img, video';
      var all = root.querySelectorAll('*');
      for (var i = 0; i < all.length; i++) {{
        var el = all[i];
        if (el.closest(skip)) continue;
        var bg = getComputedStyle(el).backgroundColor;
        var mm = bg.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)(?:,\\s*([\\d.]+))?\\)/);
        if (!mm) continue;
        var alpha = mm[4] === undefined ? 1 : parseFloat(mm[4]);
        if (alpha < 0.5) continue;
        if (Math.abs(+mm[1] - sr) <= 32 && Math.abs(+mm[2] - sg) <= 32 && Math.abs(+mm[3] - sb) <= 32) {{
          el.style.setProperty('background-color', 'transparent', 'important');
          el.style.setProperty('background-image', 'none', 'important');
        }}
      }}
    }} catch (e) {{}}
  }}

  var FADE = (typeof CFG.fade === 'number' && CFG.fade >= 0) ? CFG.fade : 900;

  function ensureStack() {{
    var el = document.getElementById('astr-wp-stack');
    if (!el) {{
      el = document.createElement('div');
      el.id = 'astr-wp-stack';
      (document.body || document.documentElement).appendChild(el);
    }}
    return el;
  }}

  function ensureVStack() {{
    var el = document.getElementById('astr-wp-vstack');
    if (!el) {{
      el = document.createElement('div');
      el.id = 'astr-wp-vstack';
      (document.body || document.documentElement).appendChild(el);
    }}
    return el;
  }}

  // 静态底图：双层交叉淡化，新图加载完再淡入，旧图同步淡出后移除
  function setStill(url, instant) {{
    var stack = ensureStack();
    var cur = document.getElementById('astr-wp-stage');
    url = url || '';
    if (cur && cur.getAttribute('data-url') === url) return cur;
    var layer = document.createElement('div');
    layer.className = 'astr-wp-still';
    layer.setAttribute('data-url', url);
    if (url) layer.style.backgroundImage = 'url("' + url + '")';
    stack.appendChild(layer);
    if (cur) cur.removeAttribute('id');
    layer.id = 'astr-wp-stage';
    var done = false;
    var promote = function () {{
      if (done || !layer.parentNode) return;
      done = true;
      var kids = Array.prototype.slice.call(stack.children);
      for (var i = 0; i < kids.length; i++) {{
        var k = kids[i];
        if (k !== layer && k !== cur) stack.removeChild(k);
      }}
      void layer.offsetWidth;
      layer.classList.add('astr-wp-visible');
      if (cur) {{
        cur.classList.remove('astr-wp-visible');
        setTimeout(function () {{ if (cur.parentNode) cur.parentNode.removeChild(cur); }}, FADE + 200);
      }}
    }};
    if (instant || !url) {{
      layer.style.transition = 'none';
      promote();
      setTimeout(function () {{ layer.style.transition = ''; }}, 80);
      return layer;
    }}
    var im = new Image();
    im.onload = promote;
    im.onerror = promote;
    im.src = url;
    setTimeout(promote, 1200);
    return layer;
  }}

  function makeVideo() {{
    var v = document.createElement('video');
    v.setAttribute('playsinline', '');
    v.setAttribute('webkit-playsinline', '');
    v.loop = true;
    v.preload = 'auto';
    if (!CFG.sound) {{ v.muted = true; v.defaultMuted = true; v.setAttribute('muted', ''); }}
    return v;
  }}

  function dropVideo(el) {{
    if (!el) return false;
    if (el.id === 'astr-wp-video') el.removeAttribute('id');
    try {{ if (CFG.sound) el.muted = true; }} catch (e) {{}}
    el.classList.remove('astr-wp-on');
    setTimeout(function () {{
      try {{ el.pause(); }} catch (e) {{}}
      if (el.parentNode) el.parentNode.removeChild(el);
    }}, FADE + 200);
    if (window.__astrWpVideo === el) window.__astrWpVideo = null;
    return true;
  }}

  // 动态壁纸：新视频能播了再淡入，旧视频同步淡出，避免中间空一帧
  function setVideo(url, sound) {{
    var stack = ensureVStack();
    var cur = document.getElementById('astr-wp-video');
    if (!url) {{ dropVideo(cur); return null; }}
    if (cur && cur.getAttribute('data-src') === url) {{
      window.__astrWpVideo = cur;
      if (cur.paused) tryPlay(cur);
      return cur;
    }}
    var v = makeVideo();
    stack.appendChild(v);
    v.setAttribute('data-src', url);
    v.src = url;
    try {{ v.load(); }} catch (e) {{}}
    var done = false;
    var promote = function () {{
      if (done || !v.parentNode) return;
      done = true;
      var kids = Array.prototype.slice.call(stack.children);
      for (var i = 0; i < kids.length; i++) {{
        var k = kids[i];
        if (k !== v && k !== cur) {{ try {{ k.pause(); }} catch (e) {{}} stack.removeChild(k); }}
      }}
      if (cur) cur.removeAttribute('id');
      v.id = 'astr-wp-video';
      window.__astrWpVideo = v;
      void v.offsetWidth;
      v.classList.add('astr-wp-on');
      if (cur) {{
        try {{ cur.muted = true; }} catch (e) {{}}
        cur.classList.remove('astr-wp-on');
        setTimeout(function () {{
          try {{ cur.pause(); }} catch (e) {{}}
          if (cur.parentNode) cur.parentNode.removeChild(cur);
        }}, FADE + 200);
      }}
      if (CFG.sound) setTimeout(function () {{ if (v.muted && v.parentNode) soundBadge(v); }}, 1200);
    }};
    v.addEventListener('canplay', promote);
    v.addEventListener('playing', promote);
    v.addEventListener('error', function () {{
      done = true;
      if (v.parentNode && v !== cur) v.parentNode.removeChild(v);
    }});
    tryPlay(v);
    return v;
  }}

  function soundBadge(video) {{
    var badge = document.getElementById('astr-wp-sound');
    if (!badge) {{
      badge = document.createElement('div');
      badge.id = 'astr-wp-sound';
      badge.addEventListener('click', function () {{
        if (!window.__astrWpVideo) return;
        window.__astrWpVideo.muted = !window.__astrWpVideo.muted;
        if (!window.__astrWpVideo.muted) {{
          var p = window.__astrWpVideo.play();
          if (p && p.catch) p.catch(function () {{}});
          badge.textContent = '🔊 点击静音';
        }} else {{
          badge.textContent = '🔈 点击出声';
        }}
      }});
      document.body.appendChild(badge);
    }}
    badge.textContent = video.muted ? '🔈 点击出声' : '🔊 点击静音';
    badge.classList.add('astr-wp-show');
  }}

  function tryPlay(video) {{
    if (!video) return;
    var p = video.play();
    if (p && p.catch) {{
      p.catch(function () {{
        if (CFG.sound) soundBadge(video);
        var kick = function () {{
          var again = video.play();
          if (again && again.catch) again.catch(function () {{}});
          document.removeEventListener('click', kick);
          document.removeEventListener('keydown', kick);
        }};
        document.addEventListener('click', kick);
        document.addEventListener('keydown', kick);
      }});
    }}
  }}

  function show(index, instant) {{
    var item = items[index];
    if (!item) return;
    if (index === current && !instant) return;
    current = index;
    var still = item.still || '';
    document.documentElement.style.setProperty('--astr-wp', still ? 'url("' + still + '")' : 'none');
    setStill(still, !!instant);
    if (item.video) {{
      setVideo(item.video, CFG.sound);
      return;
    }}
    var vids = document.querySelectorAll('#astr-wp-vstack > video');
    for (var i = 0; i < vids.length; i++) dropVideo(vids[i]);
  }}

  function restartTimer() {{
    if (window.__astrWpTimer) {{ clearInterval(window.__astrWpTimer); window.__astrWpTimer = null; }}
    if (CFG.interval > 0 && items.length > 1) {{
      window.__astrWpTimer = setInterval(function () {{ show(pickIndex(true)); }}, CFG.interval * 1000);
    }}
  }}

  function dropStage() {{
    ['astr-wp-stack', 'astr-wp-vstack'].forEach(function (id) {{
      var s = document.getElementById(id);
      if (!s) return;
      var kids = Array.prototype.slice.call(s.children);
      for (var i = 0; i < kids.length; i++) {{
        var k = kids[i];
        if (k.tagName === 'VIDEO') {{ try {{ k.pause(); }} catch (e) {{}} }}
        s.removeChild(k);
      }}
    }});
    ['astr-wp-veil', 'astr-wp-sound'].forEach(function (id) {{
      var el = document.getElementById(id);
      if (el) el.remove();
    }});
    window.__astrWpVideo = null;
    current = -1;
    document.documentElement.style.setProperty('--astr-wp', 'none');
    if (window.__astrWpTimer) {{ clearInterval(window.__astrWpTimer); window.__astrWpTimer = null; }}
  }}

  function applyLive(res) {{
    CFG.hash = res.hash || CFG.hash;
    if (Array.isArray(res.items)) {{ items = res.items; CFG.items = items; }}
    if (res.mode) CFG.mode = res.mode;
    if (typeof res.interval === 'number') CFG.interval = res.interval;
    if (typeof res.sound === 'boolean') CFG.sound = res.sound;
    window.ASTRBOT_WALLPAPER = CFG;
    var root = document.documentElement;
    if (typeof res.fade === 'number' && res.fade >= 0) {{
      CFG.fade = res.fade;
      FADE = res.fade;
      root.style.setProperty('--astr-wp-fade', res.fade + 'ms');
    }}
    if (typeof res.veil === 'number') root.style.setProperty('--astr-wp-veil', 'rgba(255, 255, 255, ' + res.veil.toFixed(3) + ')');
    if (typeof res.blur === 'number') root.style.setProperty('--astr-wp-blur', res.blur + 'px');
    if (res.base) root.style.setProperty('--astr-wp-base', res.base);
    if (typeof res.text_color === 'string') root.style.setProperty('--astr-wp-text', res.text_color || 'unset');
    if (res.enabled === false || !items.length) {{ dropStage(); return; }}
    if (!document.getElementById('astr-wp-stack') || !document.getElementById('astr-wp-veil')) {{
      boot();
      return;
    }}
    current = -1;
    show(pickIndex(false));
    restartTimer();
    if (!window.__astrWpVideo || window.__astrWpVideo.paused) clearSurfaces();
  }}

  function startLive() {{
    if (!CFG.live) return;
    var ticking = false;
    var tick = function () {{
      if (ticking) return;
      ticking = true;
      fetch('/api/plug/{PLUGIN_NAME}/live', {{ credentials: 'same-origin', cache: 'no-store' }})
        .then(function (r) {{ return r.ok ? r.json() : null; }})
        .then(function (res) {{
          ticking = false;
          if (!res || !res.success || res.pending) return;
          if (res.hash && res.hash === CFG.hash) return;
          applyLive(res);
        }})
        .catch(function () {{ ticking = false; }});
    }};
    setTimeout(tick, 1500);
    setInterval(tick, 2000);
    try {{
      document.addEventListener('visibilitychange', function () {{ if (!document.hidden) tick(); }});
      window.addEventListener('storage', function (e) {{
        if (e.key !== 'astrwp-live' || !e.newValue) return;
        try {{
          var res = JSON.parse(e.newValue);
          if (res && res.success !== false && !res.pending && res.hash && res.hash !== CFG.hash) applyLive(res);
        }} catch (err) {{}}
      }});
    }} catch (e) {{}}
  }}

  function boot() {{
    ensureStack();
    ensureVStack();
    if (!document.getElementById('astr-wp-veil')) {{
      var veil = document.createElement('div');
      veil.id = 'astr-wp-veil';
      document.body.appendChild(veil);
    }}
    current = -1;
    show(pickIndex(false), true);
    restartTimer();
    clearSurfaces();
    var t = null;
    try {{
      var mo = new MutationObserver(function () {{
        if (t) clearTimeout(t);
        t = setTimeout(clearSurfaces, 250);
      }});
      mo.observe(document.body, {{ childList: true, subtree: true }});
    }} catch (e) {{}}
    setTimeout(clearSurfaces, 500);
    setTimeout(clearSurfaces, 1500);
    setTimeout(clearSurfaces, 3000);
    window.addEventListener('hashchange', function () {{ setTimeout(clearSurfaces, 350); }});
    window.__astrWpNext = function () {{ show(pickIndex(true)); }};
    startLive();
  }}

  if (document.body) {{ boot(); }} else {{ document.addEventListener('DOMContentLoaded', boot); }}
}})();
</script>
{MARK_END}"""

    def write_block(self, block: str) -> bool:
        if not self.index_html.exists():
            return False
        html = self.index_html.read_text(encoding="utf-8", errors="ignore")
        if MARK_START in html and MARK_END in html:
            start = html.index(MARK_START)
            end = html.index(MARK_END) + len(MARK_END)
            if html[start:end] == block:
                return True
            new_html = html[:start] + block + html[end:]
        else:
            if "</head>" not in html:
                return False
            backup = self.index_html.with_name("index.html.bak_before_wallpaper_plugin")
            if not backup.exists():
                try:
                    backup.write_text(html, encoding="utf-8")
                except Exception:
                    pass
            index = html.index("</head>")
            new_html = html[:index] + block + "\n" + html[index:]
        try:
            self.index_html.write_text(new_html, encoding="utf-8", newline="")
            return True
        except Exception:
            return False

    def remove_block(self) -> bool:
        if not self.index_html.exists():
            return False
        html = self.index_html.read_text(encoding="utf-8", errors="ignore")
        if MARK_START in html and MARK_END in html:
            start = html.index(MARK_START)
            end = html.index(MARK_END) + len(MARK_END)
            try:
                self.index_html.write_text(html[:start] + html[end:], encoding="utf-8", newline="")
            except Exception:
                return False
        return True

    def apply(self, cfg: dict) -> dict:
        """按配置把壁纸发布到 dist 并写入注入块。"""
        if not cfg.get("enabled", True):
            self.remove_block()
            removed = self.cleanup_published()
            self._clear_live("插件已关闭")
            return {"applied": False, "reason": "插件已关闭", "count": 0, "items": [], "removed": removed}

        pool = self.pool_for(cfg)
        published, stale = self.publish(pool)
        removed = []
        for name in stale:
            for folder in (self.pub_still_dir, self.pub_video_dir):
                target = folder / name
                try:
                    if target.exists():
                        target.unlink()
                        removed.append(name)
                except Exception:
                    pass
        if not published:
            self.remove_block()
            self._clear_live("壁纸库是空的")
            return {
                "applied": False,
                "reason": "壁纸库是空的，先去导入或上传几张",
                "count": 0,
                "items": [],
                "removed": removed,
            }
        block = self.build_block(cfg, published)
        ok = self.write_block(block)
        return {
            "applied": bool(ok),
            "reason": "" if ok else "写入 data/dist/index.html 失败，检查文件权限",
            "count": len(published),
            "items": published,
            "removed": removed,
            "veil": cfg.get("veil_opacity"),
            "blur": cfg.get("blur"),
            "text_color": cfg.get("text_color"),
        }

    # ---------------------------------------------------------------- 实时同步

    def _clear_live(self, reason: str = "") -> None:
        self._last_live = {
            "hash": "off",
            "enabled": False,
            "count": 0,
            "items": [],
            "reason": reason,
        }

    def live_snapshot(self) -> dict:
        """面板页面里注入的脚本会轮询这份快照，用来不刷新就换壁纸。"""
        snap = self._last_live or {}
        if not snap:
            return {"enabled": True, "pending": True, "hash": "", "count": 0, "items": []}
        return dict(snap)

    # ---------------------------------------------------------------- 状态

    def stats(self) -> dict:
        items = self.items()
        return {
            "total": len(items),
            "videos": sum(1 for i in items if i.get("kind") == "video"),
            "images": sum(1 for i in items if i.get("kind") != "video"),
            "collections": len(self.collections()),
            "ffmpeg": self.ffmpeg or "",
            "ffmpeg_source": self.ffmpeg_source(),
            "ffmpeg_ok": bool(self.ffmpeg),
            "ffmpeg_hint": ""
            if self.ffmpeg
            else f"没找到 ffmpeg，把 ffmpeg.exe 放到 {BUNDLED_FFMPEG_DIR} 或 AstrBot 根目录都行",
            "dist": str(self.dist),
            "index_html": str(self.index_html),
            "index_exists": self.index_html.exists(),
        }
