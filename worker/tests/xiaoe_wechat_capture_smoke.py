"""Offline checks for the WeChat Xiaoetong owned-course capture boundary."""

from __future__ import annotations

import pickle
from pathlib import Path
import subprocess
import sys
import tempfile
import threading


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from feichuan_downloader.xiaoe_wechat_capture import (  # noqa: E402
    XiaoeEpisodeCaptureResult,
    XiaoeStreamCandidate,
    XiaoeWechatCaptureSession,
    episode_output_path,
    extract_media_urls,
    inspect_manifest_duration,
    manifest_url_for_media,
)
from feichuan_downloader.protocol import Request, encode_message  # noqa: E402
from feichuan_downloader.worker import WorkerService  # noqa: E402


FFMPEG = ROOT / "tools" / "ffmpeg.exe"
FFPROBE = ROOT / "tools" / "ffprobe.exe"


def check_cache_url_and_manifest_rules() -> None:
    signed = (
        "https://encrypt-k-vod.xet.tech/public/course/segment_001.ts"
        "?expires=1&sign=redacted-test-value"
    )
    payload = ("noise\x00" + signed + "\x00end").encode()
    urls = extract_media_urls(payload)
    assert urls == (signed,)
    manifest_url = manifest_url_for_media(urls[0])
    assert manifest_url.startswith(
        "https://encrypt-k-vod.xet.tech/public/course/playlist_eof.m3u8?"
    )
    assert "redacted-test-value" in manifest_url
    assert extract_media_urls(b"https://example.invalid/course/one.ts?sign=secret") == ()

    manifest = "#EXTM3U\n#EXTINF:2.5,\none.ts\n#EXTINF:3.25,\ntwo.ts\n#EXT-X-ENDLIST\n"
    assert inspect_manifest_duration(manifest) == 5.75
    try:
        inspect_manifest_duration(
            "#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"key\"\n#EXTINF:3,\none.ts\n#EXT-X-ENDLIST"
        )
    except ValueError:
        pass
    else:
        raise AssertionError("encrypted manifests must be rejected")

    candidate = XiaoeStreamCandidate(signed, 5.75)
    assert "redacted-test-value" not in repr(candidate)
    try:
        pickle.dumps(candidate)
    except TypeError:
        pass
    else:
        raise AssertionError("signed candidate must not be pickleable")


def check_existing_video_is_validated_and_skipped() -> None:
    if not FFMPEG.is_file() or not FFPROBE.is_file():
        raise AssertionError("tools 中缺少 ffmpeg.exe 或 ffprobe.exe")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        target = episode_output_path(
            root,
            "万周迎 | 非视觉太极读经典 悟拳道 修身心（12讲）",
            "万周迎 | 可爱的老子： 一个说「我有点笨」的智慧老头",
            1,
        )
        assert target.parent.name == "万周迎_非视觉太极读经典悟拳道修身心_已购课程"
        assert target.name == "01_万周迎_可爱的老子_一个说我有点笨的智慧老头.mp4"
        target.parent.mkdir(parents=True)
        completed = subprocess.run(
            [
                str(FFMPEG),
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=size=64x64:duration=1",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=sample_rate=44100:channel_layout=stereo",
                "-shortest",
                "-c:v",
                "mpeg4",
                "-c:a",
                "aac",
                "-y",
                str(target),
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert completed.returncode == 0
        session = XiaoeWechatCaptureSession(
            media_url_reader=lambda: (),
            ffmpeg_path=FFMPEG,
            ffprobe_path=FFPROBE,
            download_directory=root,
        )
        result = session.capture_episode(
            course_title="万周迎 | 非视觉太极读经典 悟拳道 修身心（12讲）",
            episode_title="万周迎 | 可爱的老子： 一个说「我有点笨」的智慧老头",
            episode_index=1,
            episode_total=9,
            page_url="https://shop.h5.xet.pomoho.com/p/course/column/p_public",
            cancel_event=threading.Event(),
            wait_seconds=0.01,
        )
        assert result.skipped and result.path == target
        assert not list(root.rglob("*.part.mp4"))
        assert "signed_urls=<memory-only>" in repr(session)
        session.clear_sensitive()


def check_protocol_keeps_signed_state_in_memory() -> None:
    class FakeSession(XiaoeWechatCaptureSession):
        def __init__(self) -> None:
            self.secret = "https://media.xet.tech/replay.m3u8?sign=redacted-test-value"
            self.armed = 0
            self.cleared = False

        def arm(self) -> None:
            self.armed += 1

        def capture_episode(self, **_kwargs) -> XiaoeEpisodeCaptureResult:
            return XiaoeEpisodeCaptureResult(Path(r"D:\owned\one.mp4"), False, 60.0)

        def clear_sensitive(self) -> None:
            self.secret = ""
            self.cleared = True

    session = FakeSession()
    service = WorkerService(xiaoe_capture_factory=lambda: session)
    response_type, payload = service.handle(Request("prepare", "xiaoe.capture.prepare", {}))
    assert response_type == "xiaoe.capture.prepare.result"
    operation_id = str(payload["operation_id"])
    _type, armed = service.handle(
        Request("arm", "xiaoe.capture.arm", {"operation_id": operation_id})
    )
    assert armed == {"armed": True} and session.armed == 1
    result = service._xiaoe_capture_download(
        {
            "operation_id": operation_id,
            "course_title": "公开课程名",
            "episode_title": "第一讲",
            "episode_index": 1,
            "episode_total": 1,
            "page_url": "https://shop.h5.xet.pomoho.com/p/course/column/p_public",
        }
    )
    encoded = encode_message("result", "xiaoe.capture.download.result", result)
    assert b"redacted-test-value" not in encoded
    service.handle(Request("release", "operation.release", {"operation_id": operation_id}))
    assert session.cleared


def main() -> None:
    check_cache_url_and_manifest_rules()
    check_existing_video_is_validated_and_skipped()
    check_protocol_keeps_signed_state_in_memory()
    print("xiaoe wechat capture smoke passed")


if __name__ == "__main__":
    main()
