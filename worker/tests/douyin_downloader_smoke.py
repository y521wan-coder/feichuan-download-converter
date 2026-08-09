"""纯离线验证抖音作品下载、Range、图文转换和敏感错误脱敏。"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from feichuan_downloader.douyin_downloader import (
    DouyinDownloadError,
    DouyinDownloader,
)
from feichuan_downloader.models import (
    ContentKind,
    DownloadMode,
    MediaDescriptor,
    Platform,
    WorkItem,
)
from feichuan_downloader.naming import (
    douyin_image_filename,
    douyin_video_filename,
)


FFMPEG = ROOT / "tools" / "ffmpeg.exe"
FFPROBE = ROOT / "tools" / "ffprobe.exe"


class Handler(BaseHTTPRequestHandler):
    payloads: dict[str, bytes] = {}
    failing: set[str] = set()
    request_paths: list[str] = []
    range_headers: list[str] = []
    cookie_headers: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib API name
        path = urlsplit(self.path).path
        type(self).request_paths.append(path)
        type(self).range_headers.append(self.headers.get("Range", ""))
        type(self).cookie_headers.append(self.headers.get("Cookie", ""))
        if path in type(self).failing:
            self.send_response(503)
            self.end_headers()
            return
        payload = type(self).payloads.get(path)
        if payload is None:
            self.send_response(404)
            self.end_headers()
            return

        range_header = self.headers.get("Range", "")
        start = 0
        status = 200
        if range_header.startswith("bytes=") and range_header.endswith("-"):
            try:
                start = int(range_header[6:-1])
            except ValueError:
                start = 0
            if start >= len(payload):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(payload)}")
                self.end_headers()
                return
            status = 206
        body = payload[start:]
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        if status == 206:
            self.send_header(
                "Content-Range",
                f"bytes {start}-{len(payload) - 1}/{len(payload)}",
            )
        if path.endswith(".webp"):
            self.send_header("Content-Type", "image/webp")
        elif path.endswith(".png"):
            self.send_header("Content-Type", "image/png")
        else:
            self.send_header("Content-Type", "video/mp4")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return

    @classmethod
    def reset_requests(cls) -> None:
        cls.request_paths = []
        cls.range_headers = []
        cls.cookie_headers = []


class SuccessfulFallback:
    def __init__(self, source: Path, output_dir: Path) -> None:
        self.source = source
        self.output_dir = output_dir
        self.calls: list[str] = []

    def download(self, url: str, **_kwargs: object) -> SimpleNamespace:
        self.calls.append(url)
        output = self.output_dir / "yt-dlp临时标题.mp4"
        shutil.copyfile(self.source, output)
        return SimpleNamespace(path=output)


class FailingFallback:
    def download(self, _url: str, **_kwargs: object) -> SimpleNamespace:
        raise RuntimeError(
            "fallback https://fallback.example.test/video?token=FALLBACK_URL_SECRET "
            "Cookie=FALLBACK_COOKIE_SECRET DecodeKey=FALLBACK_KEY_SECRET"
        )


def create_fixtures(directory: Path) -> tuple[Path, Path, Path]:
    if not FFMPEG.is_file() or not FFPROBE.is_file():
        raise AssertionError("tools 中缺少 ffmpeg.exe 或 ffprobe.exe")
    video = directory / "fixture.mp4"
    png = directory / "fixture.png"
    webp = directory / "fixture.webp"
    commands = [
        [
            str(FFMPEG),
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=96x64:r=5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-t",
            "1",
            "-c:v",
            # The distributable FFmpeg payload is the reviewed LGPL build;
            # libx264 is GPL-only and intentionally absent.  OpenH264 keeps
            # this fixture H.264 without weakening the release boundary.
            "libopenh264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "32k",
            str(video),
        ],
        [
            str(FFMPEG),
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=32x24",
            "-frames:v",
            "1",
            str(png),
        ],
        [
            str(FFMPEG),
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=32x24",
            "-frames:v",
            "1",
            "-c:v",
            "libwebp",
            str(webp),
        ],
    ]
    for command in commands:
        completed = subprocess.run(command, capture_output=True, check=False)
        if completed.returncode:
            raise AssertionError(completed.stderr.decode("utf-8", errors="replace"))
    return video, png, webp


def is_webp(path: Path) -> bool:
    head = path.read_bytes()[:16]
    return head.startswith(b"RIFF") and head[8:12] == b"WEBP"


def descriptor(base: str, path: str, **kwargs: object) -> MediaDescriptor:
    return MediaDescriptor(
        quality=str(kwargs.pop("quality", "原图")),
        media_url=f"{base}{path}",
        **kwargs,
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="feichuan-douyin-backend-") as directory:
        temp_root = Path(directory)
        fixtures = temp_root / "fixtures"
        output = temp_root / "output"
        fixtures.mkdir()
        output.mkdir()
        video_fixture, png_fixture, webp_fixture = create_fixtures(fixtures)

        Handler.payloads = {
            "/video.mp4": video_fixture.read_bytes(),
            "/fallback-direct.mp4": video_fixture.read_bytes(),
            "/one.webp": webp_fixture.read_bytes(),
            "/two.png": png_fixture.read_bytes(),
        }
        Handler.failing = {"/fail-video.mp4", "/fail-image.png"}
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            backend = DouyinDownloader(
                download_dir=output,
                ffmpeg_path=FFMPEG,
                ffprobe_path=FFPROBE,
                fallback_downloader=FailingFallback(),
            )

            video_item = WorkItem(
                platform=Platform.DOUYIN,
                work_id="video-1001",
                content_type=ContentKind.VIDEO,
                title="蓝色视频",
                author="测试作者",
                published_at=date(2026, 7, 18),
                canonical_url=f"{base}/canonical/video-1001?token=CANONICAL_SECRET",
            )
            selected_video = descriptor(
                base,
                "/video.mp4?token=DIRECT_URL_SECRET",
                quality="1080P",
                width=1920,
                height=1080,
                codec="avc1.640028",
            )
            video_candidates = (
                descriptor(
                    base,
                    "/not-selected-vp9.mp4?token=VP9_SECRET",
                    quality="2160P",
                    width=3840,
                    height=2160,
                    codec="vp9",
                ),
                descriptor(
                    base,
                    "/not-selected-h264.mp4?token=LOW_SECRET",
                    quality="720P",
                    width=1280,
                    height=720,
                    codec="h264",
                ),
                selected_video,
            )
            target = output / douyin_video_filename(video_item)
            partial = target.with_name(target.name + ".part")
            fixture_bytes = video_fixture.read_bytes()
            resume_at = max(1, len(fixture_bytes) // 3)
            partial.write_bytes(fixture_bytes[:resume_at])
            progress: list[float] = []
            Handler.reset_requests()
            result = backend.download(
                video_item,
                video_candidates,
                cookie_header="session=COOKIE_HEADER_SECRET",
                on_progress=lambda value: progress.append(float(value or 0.0)),
            )
            assert result.path == target
            assert target.name == "抖音_测试作者_20260718_video-1001_蓝色视频.mp4"
            assert target.read_bytes() == fixture_bytes
            assert not partial.exists()
            assert Handler.request_paths == ["/video.mp4"]
            assert Handler.range_headers == [f"bytes={resume_at}-"]
            assert Handler.cookie_headers == ["session=COOKIE_HEADER_SECRET"]
            assert progress and progress[-1] == 100.0

            Handler.reset_requests()
            skipped = backend.download(video_item, video_candidates)
            assert skipped.skipped and not Handler.request_paths

            cancelled = threading.Event()
            cancelled.set()
            try:
                backend.download(
                    video_item,
                    video_candidates,
                    mode=DownloadMode.REDOWNLOAD_ALL,
                    cancel_event=cancelled,
                )
            except DouyinDownloadError as exc:
                assert "取消" in str(exc)
            else:
                raise AssertionError("pre-cancelled download unexpectedly succeeded")
            assert not Handler.request_paths

            fallback_item = WorkItem(
                platform=Platform.DOUYIN,
                work_id="video-fallback",
                content_type=ContentKind.VIDEO,
                title="兜底视频",
                author="测试作者",
                published_at=date(2026, 7, 18),
                canonical_url=f"{base}/canonical/fallback?token=FALLBACK_CANONICAL_SECRET",
            )
            successful_fallback = SuccessfulFallback(video_fixture, output)
            fallback_backend = DouyinDownloader(
                download_dir=output,
                ffmpeg_path=FFMPEG,
                ffprobe_path=FFPROBE,
                fallback_downloader=successful_fallback,
            )
            fallback_result = fallback_backend.download(
                fallback_item,
                (
                    descriptor(
                        base,
                        "/fail-video.mp4?token=EXPIRED_DIRECT_SECRET",
                        quality="1080P",
                        width=1920,
                        height=1080,
                        codec="h264",
                    ),
                ),
            )
            expected_fallback = output / douyin_video_filename(fallback_item)
            assert fallback_result.used_fallback
            assert fallback_result.path == expected_fallback
            assert expected_fallback.is_file()
            assert not (output / "yt-dlp临时标题.mp4").exists()
            assert successful_fallback.calls == [fallback_item.canonical_url]

            image_item = WorkItem(
                platform=Platform.DOUYIN,
                work_id="image-2002",
                content_type=ContentKind.IMAGE,
                title="两张原图",
                author="图文作者",
                published_at=date(2026, 7, 18),
                canonical_url=f"{base}/note/image-2002?token=DESCRIPTION_URL_SECRET",
            )
            Handler.reset_requests()
            image_result = backend.download(
                image_item,
                (
                    descriptor(base, "/one.webp?token=IMAGE_ONE_SECRET", quality="原图1"),
                    descriptor(base, "/two.png?token=IMAGE_TWO_SECRET", quality="原图2"),
                ),
                cookie_header="image_session=IMAGE_COOKIE_SECRET",
            )
            first_image = output / douyin_image_filename(image_item, 1)
            second_image = output / douyin_image_filename(image_item, 2)
            assert image_result.media_paths == (first_image, second_image)
            assert first_image.read_bytes() == webp_fixture.read_bytes()
            assert is_webp(first_image) and is_webp(second_image)
            assert second_image.read_bytes() != png_fixture.read_bytes()
            assert Handler.request_paths == ["/one.webp", "/two.png"]
            assert Handler.cookie_headers == [
                "image_session=IMAGE_COOKIE_SECRET",
                "image_session=IMAGE_COOKIE_SECRET",
            ]
            assert image_result.description_path and image_result.description_path.is_file()
            description = image_result.description_path.read_text(encoding="utf-8")
            assert "作品ID：image-2002" in description
            assert "图片数量：2" in description
            assert "DESCRIPTION_URL_SECRET" not in description
            assert not list(output.glob("*.part"))

            # REDOWNLOAD_ALL 在全部新图验证前不替换旧目标；第二张失败时旧文件仍完整。
            failing_item = WorkItem(
                platform=Platform.DOUYIN,
                work_id="image-failure",
                content_type=ContentKind.IMAGE,
                title="失败图文",
                author="图文作者",
                published_at=date(2026, 7, 18),
                canonical_url=f"{base}/note/image-failure?token=FAIL_CANONICAL_SECRET",
            )
            old_targets = [
                output / douyin_image_filename(failing_item, 1),
                output / douyin_image_filename(failing_item, 2),
            ]
            for old_target in old_targets:
                old_target.write_bytes(webp_fixture.read_bytes())
            old_bytes = [path.read_bytes() for path in old_targets]
            try:
                backend.download(
                    failing_item,
                    (
                        descriptor(base, "/one.webp?token=STAGED_IMAGE_SECRET"),
                        descriptor(base, "/fail-image.png?token=FAIL_IMAGE_SECRET"),
                    ),
                    cookie_header="failure_cookie=FAILURE_COOKIE_SECRET",
                    mode=DownloadMode.REDOWNLOAD_ALL,
                )
            except DouyinDownloadError as exc:
                message = str(exc)
            else:
                raise AssertionError("failing image work unexpectedly succeeded")
            assert [path.read_bytes() for path in old_targets] == old_bytes
            assert not list(output.glob("*image-failure*.part"))
            for secret in (
                "STAGED_IMAGE_SECRET",
                "FAIL_IMAGE_SECRET",
                "FAILURE_COOKIE_SECRET",
                "FAIL_CANONICAL_SECRET",
            ):
                assert secret not in message
            assert "http://" not in message and "https://" not in message

            # 视频的直接请求和兜底同时失败时，异常也不得包含 URL/Cookie/DecodeKey。
            protected_bytes = expected_fallback.read_bytes()
            try:
                backend.download(
                    fallback_item,
                    (
                        descriptor(
                            base,
                            "/fail-video.mp4?token=VIDEO_ERROR_SECRET",
                            quality="1080P",
                            codec="h264",
                        ),
                    ),
                    cookie_header="video_cookie=VIDEO_COOKIE_SECRET",
                    mode=DownloadMode.REDOWNLOAD_ALL,
                )
            except DouyinDownloadError as exc:
                error_message = str(exc)
            else:
                raise AssertionError("failing video work unexpectedly succeeded")
            assert expected_fallback.read_bytes() == protected_bytes
            for secret in (
                "VIDEO_ERROR_SECRET",
                "VIDEO_COOKIE_SECRET",
                "FALLBACK_URL_SECRET",
                "FALLBACK_COOKIE_SECRET",
                "FALLBACK_KEY_SECRET",
            ):
                assert secret not in error_message
            assert "http://" not in error_message and "https://" not in error_message

            all_bytes = b"".join(
                path.read_bytes() for path in output.iterdir() if path.is_file()
            )
            for secret in (
                b"COOKIE_HEADER_SECRET",
                b"IMAGE_COOKIE_SECRET",
                b"DIRECT_URL_SECRET",
                b"IMAGE_ONE_SECRET",
                b"IMAGE_TWO_SECRET",
            ):
                assert secret not in all_bytes
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    print("douyin downloader smoke passed")


if __name__ == "__main__":
    main()
