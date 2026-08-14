"""Offline direct-link selection, protocol, and redaction checks."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from feichuan_downloader.downloader import DirectLinkResult, DownloadError, Downloader  # noqa: E402
from feichuan_downloader.coordinator import CoordinatorError, DownloadCoordinator  # noqa: E402
from feichuan_downloader.douyin_capture import DouyinCapture  # noqa: E402
from feichuan_downloader.models import SourceKind  # noqa: E402
from feichuan_downloader.protocol import decode_request, encode_message  # noqa: E402
from feichuan_downloader.windows_clipboard import ClipboardWriteError  # noqa: E402
from feichuan_downloader.worker import WorkerService  # noqa: E402


SECRET_COMBINED = "https://media.example.test/combined.mp4?temporary=secret-combined"
SECRET_AUDIO = "https://media.example.test/audio.m4a?temporary=secret-audio"
SECRET_VIDEO = "https://media.example.test/video.mp4?temporary=secret-video"


def request(request_id: str, message_type: str, payload: dict[str, object]) -> bytes:
    return encode_message(request_id, message_type, payload)


def check_sensitive_result_redaction() -> None:
    result = DirectLinkResult(SECRET_AUDIO, "audio")
    assert SECRET_AUDIO not in repr(result)
    assert SECRET_AUDIO not in str(result)
    try:
        result.__getstate__()
    except TypeError:
        pass
    else:
        raise AssertionError("sensitive direct-link result allowed serialization")
    try:
        result.__reduce_ex__(4)
    except TypeError:
        pass
    else:
        raise AssertionError("sensitive direct-link result allowed pickle reduction")
    result.clear_sensitive()
    assert result.url == ""
    assert result.cleared


def check_combined_is_preferred_over_split() -> None:
    result = Downloader._select_direct_link(
        {
            "requested_formats": [
                {"url": SECRET_VIDEO, "vcodec": "h264", "acodec": "none", "height": 2160},
                {"url": SECRET_AUDIO, "vcodec": "none", "acodec": "aac", "abr": 192},
            ],
            "formats": [
                {"url": SECRET_COMBINED, "vcodec": "h264", "acodec": "aac", "height": 720},
            ],
        }
    )
    try:
        assert result.media_kind == "combined"
        assert result.url == SECRET_COMBINED
    finally:
        result.clear_sensitive()


def check_split_copies_best_audio_only() -> None:
    lower_audio = "https://media.example.test/audio-low.m4a?temporary=low"
    result = Downloader._select_direct_link(
        {
            "requested_formats": [
                {"url": SECRET_VIDEO, "vcodec": "h264", "acodec": "none", "height": 2160},
                {"url": lower_audio, "vcodec": "none", "acodec": "aac", "abr": 96},
                {"url": SECRET_AUDIO, "vcodec": "none", "acodec": "aac", "abr": 192},
            ]
        }
    )
    try:
        assert result.media_kind == "audio"
        assert result.url == SECRET_AUDIO
    finally:
        result.clear_sensitive()


def check_note_requires_standalone_audio() -> None:
    result = Downloader._select_direct_link(
        {
            "requested_formats": [
                {"url": SECRET_COMBINED, "vcodec": "h264", "acodec": "aac", "height": 1080},
                {"url": SECRET_AUDIO, "vcodec": "none", "acodec": "aac", "abr": 192},
            ]
        },
        require_audio_only=True,
    )
    try:
        assert result.media_kind == "audio"
        assert result.url == SECRET_AUDIO
    finally:
        result.clear_sensitive()

    try:
        Downloader._select_direct_link(
            {"url": SECRET_COMBINED, "vcodec": "h264", "acodec": "aac"},
            require_audio_only=True,
        )
    except DownloadError as exc:
        assert "背景音频" in str(exc)
    else:
        raise AssertionError("combined media was accepted as a note audio direct link")


def check_unsafe_or_video_only_is_rejected() -> None:
    for payload in (
        {"url": "file:///private/video.mp4", "vcodec": "h264", "acodec": "aac"},
        {"url": SECRET_VIDEO, "vcodec": "h264", "acodec": "none"},
        {"url": SECRET_COMBINED, "vcodec": "h264", "acodec": "aac", "is_live": True},
    ):
        try:
            Downloader._select_direct_link(payload)
        except DownloadError:
            pass
        else:
            raise AssertionError("unsafe or silent-only direct link was accepted")


def check_coordinator_routes_supported_single_sources() -> None:
    coordinator = DownloadCoordinator()
    try:
        coordinator.resolve_direct_link("https://www.youtube.com/playlist?list=PL_OFFLINE")
    except CoordinatorError as exc:
        assert "只支持单视频" in str(exc)
    else:
        raise AssertionError("playlist reached direct-link resolution")

    class NoteScanner:
        @staticmethod
        def identify(_text: str) -> object:
            return SimpleNamespace(
                source=SourceKind.SINGLE_LINK,
                url="https://www.douyin.com/note/123456/",
            )

    class DirectDownloader:
        def __init__(self) -> None:
            self.require_audio_only: list[bool] = []

        def resolve_direct_link(self, _url: str, **kwargs: object) -> DirectLinkResult:
            self.require_audio_only.append(bool(kwargs.get("require_audio_only")))
            return DirectLinkResult(SECRET_AUDIO, "audio")

        def cancel(self) -> None:
            return None

    downloader = DirectDownloader()
    note = DownloadCoordinator(
        douyin_scanner_factory=NoteScanner,
        downloader_factory=lambda: downloader,
    )
    result = note.resolve_direct_link("https://www.douyin.com/note/123456/")
    try:
        assert result.media_kind == "audio"
        assert downloader.require_audio_only == [True]
    finally:
        result.clear_sensitive()


def check_browser_capture_requires_note_audio() -> None:
    combined = {
        "url": SECRET_COMBINED.replace(".mp4", ".m3u8"),
        "mime": "application/vnd.apple.mpegurl",
        "content_length": 500000,
    }
    audio = {
        "url": SECRET_AUDIO,
        "mime": "audio/mp4",
        "content_length": 200000,
    }
    selected, media_kind = DouyinCapture._select_direct_candidate(
        [combined, audio],
        require_audio_only=True,
    )
    assert selected == SECRET_AUDIO and media_kind == "audio"

    try:
        DouyinCapture._select_direct_candidate(
            [combined],
            require_audio_only=True,
        )
    except RuntimeError as exc:
        assert "背景音频" in str(exc)
    else:
        raise AssertionError("browser capture accepted a combined note candidate")


def check_douyin_mislabeled_audio_url_is_recognized() -> None:
    candidate = {
        "url": "https://media.example.test/stream/audio_track?temporary=redacted",
        "mime": "video/mp4",
        "content_length": 87062,
    }
    assert DouyinCapture._candidate_media_kind(candidate) == "audio"
    assert DouyinCapture._find_audio_candidate([candidate]) is candidate


def check_protocol_copies_without_returning_url() -> None:
    emitted: list[dict[str, object]] = []
    copied: list[str] = []
    completed = threading.Event()
    sensitive_result: DirectLinkResult | None = None

    class FakeCoordinator:
        def __init__(self, *, on_event=None, on_line=None) -> None:
            self.busy = False

        def resolve_direct_link(self, _text: str, **_kwargs: object) -> DirectLinkResult:
            nonlocal sensitive_result
            sensitive_result = DirectLinkResult(SECRET_AUDIO, "audio")
            return sensitive_result

        def cancel(self) -> None:
            return None

    def emit(message: bytes) -> None:
        decoded = json.loads(message.decode("utf-8"))
        emitted.append(decoded)
        if decoded["type"] in {"direct_link.copy.result", "error"}:
            completed.set()

    service = WorkerService(
        coordinator_factory=FakeCoordinator,
        emit=emit,
        clipboard_writer=copied.append,
    )
    decoded = decode_request(
        request("direct-1", "direct_link.copy", {"text": "https://example.test/watch/1"})
    )
    assert service.dispatch(decoded) is None
    assert completed.wait(5)
    serialized = json.dumps(emitted, ensure_ascii=False)
    assert SECRET_AUDIO not in serialized
    assert copied == [SECRET_AUDIO]
    assert sensitive_result is not None and sensitive_result.cleared
    result = next(item for item in emitted if item["type"] == "direct_link.copy.result")
    assert result["payload"] == {"copied": True, "media_kind": "audio"}


def check_clipboard_failure_is_safe() -> None:
    emitted: list[dict[str, object]] = []
    completed = threading.Event()

    class FakeCoordinator:
        def __init__(self, *, on_event=None, on_line=None) -> None:
            self.busy = False

        def resolve_direct_link(self, _text: str, **_kwargs: object) -> DirectLinkResult:
            return DirectLinkResult(SECRET_COMBINED, "combined")

        def cancel(self) -> None:
            return None

    def reject_clipboard(_text: str) -> None:
        raise ClipboardWriteError("系统剪贴板正被其它程序占用，请稍后重试。")

    def emit(message: bytes) -> None:
        decoded = json.loads(message.decode("utf-8"))
        emitted.append(decoded)
        completed.set()

    service = WorkerService(
        coordinator_factory=FakeCoordinator,
        emit=emit,
        clipboard_writer=reject_clipboard,
    )
    service.dispatch(
        decode_request(
            request("direct-fail", "direct_link.copy", {"text": "https://example.test/watch/2"})
        )
    )
    assert completed.wait(5)
    serialized = json.dumps(emitted, ensure_ascii=False)
    assert SECRET_COMBINED not in serialized
    assert emitted[0]["type"] == "error"
    assert emitted[0]["payload"]["code"] == "clipboard_unavailable"


def main() -> None:
    check_sensitive_result_redaction()
    check_combined_is_preferred_over_split()
    check_split_copies_best_audio_only()
    check_note_requires_standalone_audio()
    check_unsafe_or_video_only_is_rejected()
    check_coordinator_routes_supported_single_sources()
    check_browser_capture_requires_note_audio()
    check_douyin_mislabeled_audio_url_is_recognized()
    check_protocol_copies_without_returning_url()
    check_clipboard_failure_is_safe()
    print("direct link smoke passed")


if __name__ == "__main__":
    main()
