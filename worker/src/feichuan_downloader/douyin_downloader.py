"""抖音批量作品的媒体下载后端。

该模块只消费枚举器已经生成的稳定 ``WorkItem`` 和内存 ``MediaDescriptor``。
签名媒体 URL、Cookie 与其它临时请求字段不会写入文件、异常或日志。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

from .config import FFMPEG_PATH, FFPROBE_PATH, get_download_dir, safe_url_for_log
from .downloader import Downloader
from .models import ContentKind, DownloadMode, MediaDescriptor, Platform, WorkItem
from .naming import (
    douyin_image_filename,
    douyin_video_filename,
    publication_date,
)
from .quality import QualityPreference, coerce_quality_preference


ProgressCallback = Callable[[float | None], None]

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0 Safari/537.36"
)
_URL_RE = re.compile(r"https?://[^\s\]>)'\"]+", re.IGNORECASE)
_SECRET_RE = re.compile(
    r"(?i)\b(cookie|authorization|proxy-authorization|token|decode[_-]?key)"
    r"(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)


class DouyinDownloadError(RuntimeError):
    """可直接显示给用户、且不含临时媒体凭据的错误。"""


class _Cancelled(RuntimeError):
    pass


class _DirectDownloadFailure(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DouyinDownloadResult:
    item: WorkItem
    media_paths: tuple[Path, ...]
    description_path: Path | None = None
    used_fallback: bool = False
    skipped: bool = False

    @property
    def path(self) -> Path:
        if not self.media_paths:
            raise RuntimeError("下载结果没有媒体文件。")
        return self.media_paths[0]

    @property
    def files(self) -> tuple[Path, ...]:
        if self.description_path is None:
            return self.media_paths
        return (*self.media_paths, self.description_path)


def _check_cancel(cancel_event: Any) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise _Cancelled("下载已取消。")


def _emit_progress(callback: ProgressCallback | None, value: float | None) -> None:
    if callback is None:
        return
    if value is None:
        callback(None)
        return
    callback(max(0.0, min(100.0, float(value))))


def _redact_error(value: object, sensitive_values: Iterable[str] = ()) -> str:
    """错误中不保留任何 URL，并按字段名及已知值清理凭据。"""

    text = str(value or "").strip()
    text = _URL_RE.sub("<redacted-url>", text)
    text = _SECRET_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>",
        text,
    )
    for secret in sensitive_values:
        secret = str(secret or "")
        if secret:
            text = text.replace(secret, "<redacted>")
    return text[:1000] or "未知错误"


def _transport_error(exc: BaseException) -> str:
    if isinstance(exc, requests.Timeout):
        return "媒体请求超时"
    if isinstance(exc, requests.ConnectionError):
        return "媒体连接失败"
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code:
        return f"媒体服务器返回 HTTP {int(status_code)}"
    return "媒体请求失败"


class DouyinDownloader:
    """下载一个已枚举的抖音视频或图文作品。"""

    def __init__(
        self,
        *,
        download_dir: str | os.PathLike[str] | None = None,
        ffmpeg_path: str | os.PathLike[str] = FFMPEG_PATH,
        ffprobe_path: str | os.PathLike[str] = FFPROBE_PATH,
        fallback_downloader: Any = None,
        timeout: tuple[float, float] = (10.0, 60.0),
        quality_preference: QualityPreference | str | None = None,
    ) -> None:
        self.download_dir = (
            Path(download_dir) if download_dir is not None else get_download_dir()
        )
        self.ffmpeg_path = Path(ffmpeg_path)
        self.ffprobe_path = Path(ffprobe_path)
        self.fallback_downloader = fallback_downloader
        self.timeout = timeout
        self.quality_preference = coerce_quality_preference(quality_preference)

    def download(
        self,
        item: WorkItem,
        media: Iterable[MediaDescriptor],
        *,
        cookie_header: str = "",
        mode: DownloadMode | str = DownloadMode.INCREMENTAL,
        cancel_event: Any = None,
        on_progress: ProgressCallback | None = None,
        quality_preference: QualityPreference | str | None = None,
    ) -> DouyinDownloadResult:
        """下载一个作品；图文的 ``media`` 顺序就是最终图片序号。"""

        if not isinstance(item, WorkItem):
            raise TypeError("item 必须是 WorkItem。")
        if item.platform is not Platform.DOUYIN:
            raise DouyinDownloadError("DouyinDownloader 只处理抖音作品。")
        try:
            selected_mode = DownloadMode(mode)
        except (TypeError, ValueError) as exc:
            raise DouyinDownloadError(f"未知下载模式：{mode}") from exc
        descriptors = tuple(media)
        if any(not isinstance(descriptor, MediaDescriptor) for descriptor in descriptors):
            raise TypeError("media 必须只包含 MediaDescriptor。")
        selected_quality = (
            coerce_quality_preference(quality_preference)
            if quality_preference is not None
            else self.quality_preference
        )
        self.download_dir.mkdir(parents=True, exist_ok=True)
        try:
            _check_cancel(cancel_event)
            if item.content_type is ContentKind.VIDEO:
                return self._download_video(
                    item,
                    descriptors,
                    cookie_header,
                    selected_mode,
                    cancel_event,
                    on_progress,
                    selected_quality,
                )
            if item.content_type is ContentKind.IMAGE:
                return self._download_images(
                    item,
                    descriptors,
                    cookie_header,
                    selected_mode,
                    cancel_event,
                    on_progress,
                )
            if item.content_type is ContentKind.LIVE:
                raise DouyinDownloadError("抖音直播首版不下载。")
            raise DouyinDownloadError("不支持此抖音作品类型。")
        except _Cancelled as exc:
            raise DouyinDownloadError("下载已取消。") from exc

    @staticmethod
    def _quality_score(descriptor: MediaDescriptor) -> tuple[int, int, int, int]:
        codec = descriptor.codec.lower().replace(".", "").replace("-", "")
        h264 = int("h264" in codec or "avc" in codec)
        width = descriptor.width or 0
        height = descriptor.height or 0
        pixels = width * height
        quality_numbers = [int(value) for value in re.findall(r"\d+", descriptor.quality)]
        quality_value = max(quality_numbers, default=0)
        if "4k" in descriptor.quality.lower():
            quality_value = max(quality_value, 2160)
        return h264, pixels, height, quality_value

    @classmethod
    def select_video_media(
        cls,
        media: Iterable[MediaDescriptor],
        quality_preference: QualityPreference | str | None = None,
    ) -> MediaDescriptor | None:
        candidates = [descriptor for descriptor in media if descriptor.media_url]
        preference = coerce_quality_preference(quality_preference)
        filtered = cls._filter_candidates(candidates, preference)
        if filtered:
            candidates = filtered
        return max(candidates, key=cls._quality_score) if candidates else None

    @staticmethod
    def _filter_candidates(
        candidates: list[MediaDescriptor],
        preference: QualityPreference,
    ) -> list[MediaDescriptor]:
        if preference is QualityPreference.VIDEO_MP4:
            return [
                item
                for item in candidates
                if item.container.lower().lstrip(".") == "mp4"
            ]
        if preference is QualityPreference.VIDEO_WEBM:
            return [
                item
                for item in candidates
                if item.container.lower().lstrip(".") == "webm"
            ]
        if preference is QualityPreference.HEIGHT_1080:
            return [item for item in candidates if int(item.height or 0) <= 1080]
        if preference is QualityPreference.HEIGHT_720:
            return [item for item in candidates if int(item.height or 0) <= 720]
        return []

    @staticmethod
    def _descriptor_extension(descriptor: MediaDescriptor | None) -> str:
        container = (descriptor.container if descriptor else "").lower().lstrip(".")
        if container in {"mp4", "m4v", "mov", "webm", "mkv"}:
            return f".{container}"
        return ".mp4"

    def _download_video(
        self,
        item: WorkItem,
        descriptors: tuple[MediaDescriptor, ...],
        cookie_header: str,
        mode: DownloadMode,
        cancel_event: Any,
        on_progress: ProgressCallback | None,
        quality_preference: QualityPreference,
    ) -> DouyinDownloadResult:
        descriptor = self.select_video_media(descriptors, quality_preference)
        target = self.download_dir / douyin_video_filename(
            item,
            extension=self._descriptor_extension(descriptor),
        )
        partial = target.with_name(target.name + ".part")
        if mode is not DownloadMode.REDOWNLOAD_ALL and target.is_file():
            try:
                self._validate_video(target)
            except Exception:
                pass
            else:
                self._unlink(partial)
                _emit_progress(on_progress, 100.0)
                return DouyinDownloadResult(item, (target,), skipped=True)

        direct_reason = "没有可用的直接媒体地址"
        if descriptor is not None:
            effective_cookie = cookie_header or descriptor.cookie
            try:
                self._download_http(
                    descriptor,
                    partial,
                    effective_cookie,
                    cancel_event,
                    on_progress,
                )
                _check_cancel(cancel_event)
                self._validate_video(partial)
                os.replace(partial, target)
                _emit_progress(on_progress, 100.0)
                return DouyinDownloadResult(item, (target,))
            except _Cancelled:
                self._unlink(partial)
                raise
            except Exception as exc:
                if cancel_event is not None and cancel_event.is_set():
                    self._unlink(partial)
                    raise _Cancelled("下载已取消。") from exc
                header_values = tuple(descriptor.headers.values())
                direct_reason = _redact_error(
                    exc,
                    (
                        effective_cookie,
                        descriptor.cookie,
                        descriptor.token,
                        *header_values,
                    ),
                )
                self._unlink(partial)

        try:
            result = self._download_video_fallback(
                item,
                target,
                cancel_event,
                on_progress,
                quality_preference,
            )
            _emit_progress(on_progress, 100.0)
            return result
        except _Cancelled:
            raise
        except Exception as exc:
            if cancel_event is not None and cancel_event.is_set():
                raise _Cancelled("下载已取消。") from exc
            fallback_reason = _redact_error(exc, (cookie_header,))
            raise DouyinDownloadError(
                f"抖音视频下载失败：直接下载未完成（{direct_reason}）；"
                f"链接兜底未完成（{fallback_reason}）。"
            ) from None

    def _download_video_fallback(
        self,
        item: WorkItem,
        target: Path,
        cancel_event: Any,
        on_progress: ProgressCallback | None,
        quality_preference: QualityPreference,
    ) -> DouyinDownloadResult:
        if not item.canonical_url:
            raise RuntimeError("作品没有可用于链接兜底的规范链接")
        _check_cancel(cancel_event)
        backend = self.fallback_downloader
        if backend is None:
            backend = Downloader()
        # Cookie 只供上面的直接请求使用，不传给 yt-dlp，也不创建 cookie 文件。
        result = backend.download(
            item.canonical_url,
            on_progress=on_progress,
            cancel_event=cancel_event,
            quality_preference=quality_preference,
        )
        _check_cancel(cancel_event)
        source = Path(result.path)
        self._validate_video(source)
        if self._same_path(source, target):
            return DouyinDownloadResult(item, (target,), used_fallback=True)

        staging = target.with_name(target.name + ".fallback.part")
        self._unlink(staging)
        copied = False
        try:
            try:
                os.replace(source, staging)
            except OSError:
                shutil.copyfile(source, staging)
                copied = True
            self._validate_video(staging)
            _check_cancel(cancel_event)
            os.replace(staging, target)
            if copied:
                self._unlink(source)
            return DouyinDownloadResult(item, (target,), used_fallback=True)
        finally:
            self._unlink(staging)

    def _download_images(
        self,
        item: WorkItem,
        descriptors: tuple[MediaDescriptor, ...],
        cookie_header: str,
        mode: DownloadMode,
        cancel_event: Any,
        on_progress: ProgressCallback | None,
    ) -> DouyinDownloadResult:
        if not descriptors:
            raise DouyinDownloadError("抖音图文没有发现原图。")

        targets = [
            self.download_dir / douyin_image_filename(item, index)
            for index in range(1, len(descriptors) + 1)
        ]
        description_target = self._description_path(item)
        description_partial = description_target.with_name(description_target.name + ".part")
        staged: list[tuple[Path, Path]] = []
        temporary_paths: set[Path] = {description_partial}
        completed_targets: list[Path] = []
        current_index = 0
        self._unlink(description_partial)

        try:
            for current_index, (descriptor, target) in enumerate(
                zip(descriptors, targets, strict=True),
                start=1,
            ):
                _check_cancel(cancel_event)
                raw_partial = target.with_name(target.name + ".part")
                converted_partial = target.with_name(target.name + ".converted.part")
                temporary_paths.update((raw_partial, converted_partial))
                if mode is not DownloadMode.REDOWNLOAD_ALL and target.is_file():
                    try:
                        self._validate_webp(target)
                    except Exception:
                        pass
                    else:
                        self._unlink(raw_partial)
                        self._unlink(converted_partial)
                        completed_targets.append(target)
                        _emit_progress(
                            on_progress,
                            current_index * 95.0 / len(descriptors),
                        )
                        continue

                self._unlink(converted_partial)
                effective_cookie = cookie_header or descriptor.cookie

                self._download_http(
                    descriptor,
                    raw_partial,
                    effective_cookie,
                    cancel_event,
                    lambda value, index=current_index: _emit_progress(
                        on_progress,
                        ((index - 1) + ((value or 0.0) / 100.0))
                        * 95.0
                        / len(descriptors),
                    ),
                )
                _check_cancel(cancel_event)
                if self._is_webp(raw_partial):
                    self._validate_webp(raw_partial)
                    staged.append((target, raw_partial))
                else:
                    self._convert_to_webp(raw_partial, converted_partial, cancel_event)
                    self._validate_webp(converted_partial)
                    self._unlink(raw_partial)
                    staged.append((target, converted_partial))

            description_valid = False
            if mode is not DownloadMode.REDOWNLOAD_ALL and description_target.is_file():
                try:
                    description_target.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    pass
                else:
                    description_valid = True
            if not description_valid:
                description_partial.write_text(
                    self._description_text(item, len(descriptors)),
                    encoding="utf-8",
                    newline="\n",
                )

            _check_cancel(cancel_event)
            for target, stage in staged:
                os.replace(stage, target)
                completed_targets.append(target)
            if not description_valid:
                os.replace(description_partial, description_target)
            self._cleanup(temporary_paths)
            _emit_progress(on_progress, 100.0)
            ordered_paths = tuple(target for target in targets if target in completed_targets)
            return DouyinDownloadResult(
                item=item,
                media_paths=ordered_paths,
                description_path=description_target,
                skipped=not staged and description_valid,
            )
        except _Cancelled:
            self._cleanup(temporary_paths)
            raise
        except Exception as exc:
            if cancel_event is not None and cancel_event.is_set():
                self._cleanup(temporary_paths)
                raise _Cancelled("下载已取消。") from exc
            sensitive: list[str] = [cookie_header]
            for descriptor in descriptors:
                sensitive.extend((descriptor.cookie, descriptor.token))
                sensitive.extend(descriptor.headers.values())
            self._cleanup(temporary_paths)
            index_text = f"第 {current_index} 张" if current_index else "作品"
            reason = _redact_error(exc, sensitive)
            raise DouyinDownloadError(
                f"抖音图文下载失败（{index_text}），本作品未完整提交：{reason}。"
            ) from None

    def _download_http(
        self,
        descriptor: MediaDescriptor,
        partial: Path,
        cookie_header: str,
        cancel_event: Any,
        on_progress: ProgressCallback | None,
    ) -> None:
        if not descriptor.media_url:
            raise _DirectDownloadFailure("媒体信息已过期或已清理")
        _check_cancel(cancel_event)
        existing = partial.stat().st_size if partial.is_file() else 0
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }
        for key, value in descriptor.headers.items():
            normalized = key.strip()
            if normalized.lower() in {"host", "content-length", "range", "cookie"}:
                continue
            if "\r" in value or "\n" in value:
                continue
            headers[normalized] = value
        cookie_value = self._cookie_value(cookie_header)
        if cookie_value:
            headers["Cookie"] = cookie_value
        if existing:
            headers["Range"] = f"bytes={existing}-"

        session = requests.Session()
        session.trust_env = False
        try:
            try:
                response = session.get(
                    descriptor.media_url,
                    headers=headers,
                    stream=True,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                raise _DirectDownloadFailure(_transport_error(exc)) from None
            with response:
                if response.status_code == 416 and existing:
                    _emit_progress(on_progress, 100.0)
                    return
                if response.status_code not in {200, 206}:
                    raise _DirectDownloadFailure(
                        f"媒体服务器返回 HTTP {response.status_code}"
                    )
                append = bool(existing and response.status_code == 206)
                total = 0
                if response.status_code == 206:
                    content_range = response.headers.get("Content-Range", "")
                    match = _CONTENT_RANGE_RE.fullmatch(content_range.strip())
                    if not match or int(match.group(1)) != existing:
                        raise _DirectDownloadFailure("媒体服务器返回了无效的续传范围")
                    if match.group(3) != "*":
                        total = int(match.group(3))
                else:
                    existing = 0
                    try:
                        total = int(response.headers.get("Content-Length", "0") or 0)
                    except ValueError:
                        total = 0
                received = existing
                mode = "ab" if append else "wb"
                with partial.open(mode) as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        _check_cancel(cancel_event)
                        if not chunk:
                            continue
                        output.write(chunk)
                        received += len(chunk)
                        if total:
                            _emit_progress(on_progress, min(99.0, received * 100.0 / total))
                if not partial.is_file() or partial.stat().st_size == 0:
                    raise _DirectDownloadFailure("媒体响应为空")
        finally:
            session.close()

    @staticmethod
    def _cookie_value(value: str) -> str:
        cookie = str(value or "").replace("\r", "").replace("\n", "").strip()
        if cookie.lower().startswith("cookie:"):
            cookie = cookie.split(":", 1)[1].strip()
        return cookie

    def _validate_video(self, path: Path) -> None:
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("视频文件为空")
        if not self.ffprobe_path.is_file():
            raise RuntimeError("缺少 ffprobe，无法校验视频")
        try:
            completed = subprocess.run(
                [
                    str(self.ffprobe_path),
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "csv=p=0",
                    str(path),
                ],
                capture_output=True,
                timeout=45,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("ffprobe 视频校验失败") from exc
        stream_types = set(
            (completed.stdout or b"").decode("utf-8", errors="replace").split()
        )
        if completed.returncode or "video" not in stream_types:
            raise RuntimeError("文件没有有效视频轨道")
        if "audio" not in stream_types:
            raise RuntimeError("文件缺少音频轨道")

    @staticmethod
    def _is_webp(path: Path) -> bool:
        if not path.is_file():
            return False
        try:
            with path.open("rb") as source:
                head = source.read(16)
        except OSError:
            return False
        return head.startswith(b"RIFF") and head[8:12] == b"WEBP"

    def _validate_webp(self, path: Path) -> None:
        if not self._is_webp(path):
            raise RuntimeError("图片不是有效的 WebP 文件")
        if not self.ffprobe_path.is_file():
            raise RuntimeError("缺少 ffprobe，无法校验图片")
        try:
            completed = subprocess.run(
                [
                    str(self.ffprobe_path),
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=codec_name",
                    "-of",
                    "csv=p=0",
                    str(path),
                ],
                capture_output=True,
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("ffprobe 图片校验失败") from exc
        codec = (completed.stdout or b"").decode("utf-8", errors="replace").strip().lower()
        if completed.returncode or "webp" not in codec:
            raise RuntimeError("图片不是 ffprobe 可识别的 WebP")

    def _convert_to_webp(
        self,
        source: Path,
        destination: Path,
        cancel_event: Any,
    ) -> None:
        if not self.ffmpeg_path.is_file():
            raise RuntimeError("原图不是 WebP，且缺少 ffmpeg 转换工具")
        self._unlink(destination)
        command = [
            str(self.ffmpeg_path),
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-c:v",
            "libwebp",
            "-f",
            "webp",
            str(destination),
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + 90
        try:
            while process.poll() is None:
                _check_cancel(cancel_event)
                if time.monotonic() >= deadline:
                    raise RuntimeError("图片转换超时")
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode:
                raise RuntimeError("ffmpeg 图片转换失败")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()

    def _description_path(self, item: WorkItem) -> Path:
        image_name = douyin_image_filename(item, 1)
        suffix = "_001.webp"
        if not image_name.endswith(suffix):
            raise RuntimeError("无法生成图文说明文件名")
        return self.download_dir / (image_name[: -len(suffix)] + "_说明.txt")

    @staticmethod
    def _description_text(item: WorkItem, image_count: int) -> str:
        canonical = safe_url_for_log(item.canonical_url) if item.canonical_url else ""
        lines = [
            "平台：抖音图文",
            f"作者：{item.author or '未知作者'}",
            f"发布日期：{publication_date(item.published_at)}",
            f"作品ID：{item.work_id}",
            f"标题：{item.title or '无标题'}",
            f"图片数量：{image_count}",
        ]
        if canonical:
            lines.append(f"规范链接：{canonical}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _same_path(first: Path, second: Path) -> bool:
        try:
            return os.path.normcase(os.path.abspath(first)) == os.path.normcase(
                os.path.abspath(second)
            )
        except OSError:
            return first == second

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    @classmethod
    def _cleanup(cls, paths: Iterable[Path]) -> None:
        for path in paths:
            cls._unlink(path)


# 简短别名，便于协调器按“作品下载后端”命名注入。
DouyinWorkDownloader = DouyinDownloader


__all__ = [
    "DouyinDownloadError",
    "DouyinDownloadResult",
    "DouyinDownloader",
    "DouyinWorkDownloader",
]
