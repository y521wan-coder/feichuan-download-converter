"""无 GUI、无下载副作用的快速冒烟检查。"""

from __future__ import annotations

import hashlib
import inspect
import os
import sys
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from feichuan_downloader import __version__
from feichuan_downloader.config import (
    VERSION,
    get_download_dir,
    get_quality_mode,
    is_audio_url,
    QUALITY_MODE_ASK_EACH_TIME,
    QUALITY_MODE_BEST,
    safe_url_for_log,
    sanitize_filename,
    set_download_dir,
    set_quality_mode,
)
from feichuan_downloader.core_updater import CoreUpdater
from feichuan_downloader.douyin_capture import DouyinCapture, is_douyin_url
from feichuan_downloader.downloader import DownloadError, Downloader
from feichuan_downloader.software_updater import check_software_update


def main() -> None:
    assert VERSION == __version__ == "1.0"
    assert sanitize_filename('a<>:"/b*') == "a_____b_"
    assert safe_url_for_log("https://example.test/media.mp4?token=secret") == (
        "https://example.test/media.mp4"
    )
    assert is_audio_url("https://example.test/song.m4a?signature=hidden")
    assert is_douyin_url("https://v.douyin.com/example/")
    assert not is_douyin_url("https://www.example.com/video")
    note_candidates = [
        {
            "url": "https://media.example.test/preview.mp4",
            "mime": "video/mp4",
            "content_length": 900000,
        },
        {
            "url": "https://sf6-cdn-tos.douyinstatic.com/obj/ies-music/song",
            "mime": "audio/mpeg",
            "content_length": 6000000,
        },
    ]
    ordered_note = DouyinCapture._ordered_candidates(
        note_candidates,
        "https://www.douyin.com/note/7665882913611394033",
    )
    assert ordered_note == [note_candidates[1]]
    assert DouyinCapture._candidate_suffix(ordered_note[0]) == ".mp3"
    assert (
        DouyinCapture._ordered_candidates(
            [note_candidates[0]],
            "https://www.douyin.com/note/7665882913611394033",
        )
        == []
    )
    assert (
        DouyinCapture._ordered_candidates(
            note_candidates,
            "https://www.douyin.com/video/7665882913611394033",
        )[0]
        is note_candidates[0]
    )
    # Placeholder video regression: short MP4 should be rejected, real video accepted.
    import tempfile as _tl
    with _tl.TemporaryDirectory() as _td:
        _tmp = Path(_td)
        _short = _tmp / "short.mp4"
        _short.write_bytes(b"\x00\x00\x00\x1cftypmp42" + b"\x00" * 200)
        _long = _tmp / "long.mp4"
        _long.write_bytes(b"\x00\x00\x00\x1cftypmp42" + b"\x00" * 200)
        _ffprobe = str(Path(r"D:\飞船下载工具开发\tools\ffprobe.exe"))
        if Path(_ffprobe).exists():
            _orig_run = subprocess.run
            def _fake_short(*a, **kw):
                _args = a[0] if a else kw.get("args", [])
                if "format=duration" in " ".join(str(x) for x in _args):
                    _out = b"2.0\n"
                    return type(_orig_run(b"", returncode=0, stdout=_out, stderr=b""))(*a, **kw) if False else _orig_run.__class__(
                        type("R", (), {"returncode": 0, "stdout": _out, "stderr": b""})()
                    )
                return _orig_run(*a, **kw)
            class _FakeResult:
                def __init__(self, stdout=b"", stderr=b"", returncode=0):
                    self.stdout = stdout
                    self.stderr = stderr
                    self.returncode = returncode
            def _fake_run(*a, **kw):
                _args_str = " ".join(str(x) for x in (a[0] if a else kw.get("args", [])))
                if "format=duration" in _args_str:
                    _target = str(a[0][-1]) if a else ""
                    if "short" in _target:
                        return _FakeResult(stdout=b"2.0\n")
                    return _FakeResult(stdout=b"120.0\n")
                return _orig_run(*a, **kw)
            subprocess.run = _fake_run
            try:
                assert DouyinCapture._is_placeholder_video(_short) is True
                assert DouyinCapture._is_placeholder_video(_long) is False
            finally:
                subprocess.run = _orig_run
    # --- 回归：抓流兜底必须产出音画齐全的文件 ---
    tools_dir = ROOT / "tools"
    _ffmpeg = tools_dir / "ffmpeg.exe"
    _sorted = DouyinCapture._sort_candidates(
        [
            {"url": "https://x.example.test/video.mp4", "mime": "video/mp4", "content_length": 5000000},
            {"url": "https://x.example.test/small.mp4", "mime": "video/mp4", "content_length": 2000000},
            {"url": "https://x.example.test/audio.m4a", "mime": "audio/mp4", "content_length": 9000000},
            {"url": "https://x.example.test/master.m3u8", "mime": "application/vnd.apple.mpegurl", "content_length": 0},
        ]
    )
    assert [c["url"] for c in _sorted] == [
        "https://x.example.test/master.m3u8",
        "https://x.example.test/video.mp4",
        "https://x.example.test/small.mp4",
        "https://x.example.test/audio.m4a",
    ]
    _best = DouyinCapture._find_audio_candidate(
        [
            {"url": "https://x.example.test/video.mp4", "mime": "video/mp4", "content_length": 5000000},
            {"url": "https://x.example.test/audio.m4a", "mime": "audio/mp4", "content_length": 9000000},
            {"url": "https://x.example.test/audio.mp3", "mime": "audio/mpeg", "content_length": 1000000},
        ]
    )
    assert _best is not None and _best["url"].endswith("audio.mp3")
    assert (
        DouyinCapture._find_audio_candidate(
            [{"url": "https://x.example.test/v.mp4", "mime": "video/mp4", "content_length": 1}]
        )
        is None
    )
    if _ffmpeg.exists():
        with tempfile.TemporaryDirectory() as _fd:
            _m = Path(_fd)
            _vid = _m / "video_only.mp4"
            _aud = _m / "audio_only.m4a"
            _merged = _m / "merged.mp4"
            subprocess.run(
                [str(_ffmpeg), "-y", "-loglevel", "error", "-f", "lavfi",
                 "-i", "testsrc=duration=1:size=64x64:rate=10",
                 "-c:v", "libopenh264", "-pix_fmt", "yuv420p", str(_vid)],
                capture_output=True, check=False,
            )
            subprocess.run(
                [str(_ffmpeg), "-y", "-loglevel", "error", "-f", "lavfi",
                 "-i", "sine=frequency=440:duration=1", "-c:a", "aac", str(_aud)],
                capture_output=True, check=False,
            )
            assert _vid.exists() and _aud.exists(), "视频/音频夹具生成失败"
            if _vid.exists() and _aud.exists():
                assert DouyinCapture._probe_stream_types(_vid) == {"video"}
                assert DouyinCapture._probe_stream_types(_aud) == {"audio"}
                assert DouyinCapture._merge_audio_video(_vid, _aud, _merged) is True
                _merged_types = DouyinCapture._probe_stream_types(_merged)
                assert {"video", "audio"}.issubset(_merged_types), _merged_types
                _orig_dl = inspect.getattr_static(DouyinCapture, "_download_http")
                _orig_val = inspect.getattr_static(DouyinCapture, "_validate_media_file")
                _orig_ph = inspect.getattr_static(DouyinCapture, "_is_placeholder_video")
                _old_download_dir = os.environ.get("FEICHUAN_DOWNLOAD_DIR")

                def _fake_download_http(url, partial, headers, on_progress=None, cancel_event=None):
                    partial.write_bytes(_vid.read_bytes())

                def _fake_validate(path):
                    return None

                def _fake_placeholder(path):
                    return False

                DouyinCapture._download_http = staticmethod(_fake_download_http)
                DouyinCapture._validate_media_file = staticmethod(_fake_validate)
                DouyinCapture._is_placeholder_video = staticmethod(_fake_placeholder)
                try:
                    with tempfile.TemporaryDirectory() as _dd:
                        os.environ["FEICHUAN_DOWNLOAD_DIR"] = _dd
                        cap = DouyinCapture()
                        _raised = False
                        try:
                            cap._download_candidates(
                                [{"url": "https://x.example.test/v.mp4", "mime": "video/mp4", "content_length": 100}],
                                title="无声测试",
                                source_url="https://www.douyin.com/video/123",
                                on_line=None,
                                on_progress=None,
                            )
                        except RuntimeError as exc:
                            _raised = True
                            assert "音频" in str(exc), str(exc)
                        assert _raised
                        assert not any(Path(_dd).iterdir())
                finally:
                    setattr(DouyinCapture, "_download_http", _orig_dl)
                    setattr(DouyinCapture, "_validate_media_file", _orig_val)
                    setattr(DouyinCapture, "_is_placeholder_video", _orig_ph)
                    if _old_download_dir is None:
                        os.environ.pop("FEICHUAN_DOWNLOAD_DIR", None)
                    else:
                        os.environ["FEICHUAN_DOWNLOAD_DIR"] = _old_download_dir
    # 5) 真实场景：占位片段 + 纯视频轨 + 独立音频轨（Content-Type 误标 video/mp4）
    #    应自动跳过占位、配对合并为音画齐全文件。
    if _ffmpeg.exists():
        _old_dl2 = inspect.getattr_static(DouyinCapture, "_download_http")
        _old_download_dir2 = os.environ.get("FEICHUAN_DOWNLOAD_DIR")
        try:
            with tempfile.TemporaryDirectory() as _fd2:
                _m2 = Path(_fd2)
                _short_fix = _m2 / "short_placeholder.mp4"
                _vid2 = _m2 / "video_track.mp4"
                _aud2 = _m2 / "audio_track.m4a"
                subprocess.run(
                    [str(_ffmpeg), "-y", "-loglevel", "error", "-f", "lavfi",
                     "-i", "testsrc=duration=1:size=32x32:rate=5",
                     "-c:v", "libopenh264", "-pix_fmt", "yuv420p", str(_short_fix)],
                    capture_output=True, check=False,
                )
                subprocess.run(
                    [str(_ffmpeg), "-y", "-loglevel", "error", "-f", "lavfi",
                     "-i", "testsrc=duration=6:size=64x64:rate=10",
                     "-c:v", "libopenh264", "-pix_fmt", "yuv420p", str(_vid2)],
                    capture_output=True, check=False,
                )
                subprocess.run(
                    [str(_ffmpeg), "-y", "-loglevel", "error", "-f", "lavfi",
                     "-i", "sine=frequency=440:duration=6", "-c:a", "aac", str(_aud2)],
                    capture_output=True, check=False,
                )
                assert _short_fix.exists() and _vid2.exists() and _aud2.exists()
                _payload = {
                    "https://x.example.test/uuu.mp4": _short_fix,
                    "https://x.example.test/media-video.mp4": _vid2,
                    "https://x.example.test/media-audio.mp4": _aud2,
                }

                def _fake_download_http2(url, partial, headers, on_progress=None, cancel_event=None):
                    source = _payload.get(url)
                    if source is None:
                        raise RuntimeError("unexpected media url")
                    partial.write_bytes(source.read_bytes())

                DouyinCapture._download_http = staticmethod(_fake_download_http2)
                try:
                    with tempfile.TemporaryDirectory() as _dd2:
                        os.environ["FEICHUAN_DOWNLOAD_DIR"] = _dd2
                        cap = DouyinCapture()
                        result = cap._download_candidates(
                            [
                                {"url": "https://x.example.test/uuu.mp4", "mime": "video/mp4", "content_length": 100},
                                {"url": "https://x.example.test/media-video.mp4", "mime": "video/mp4", "content_length": 200},
                                {"url": "https://x.example.test/media-audio.mp4", "mime": "video/mp4", "content_length": 50},
                            ],
                            title="配对合并测试",
                            source_url="https://www.douyin.com/video/123",
                            on_line=None,
                            on_progress=None,
                        )
                        merged_types = DouyinCapture._probe_stream_types(result.path)
                        assert {"video", "audio"}.issubset(merged_types), merged_types
                        assert result.path.exists() and result.path.stat().st_size > 0
                finally:
                    setattr(DouyinCapture, "_download_http", _old_dl2)
                    if _old_download_dir2 is None:
                        os.environ.pop("FEICHUAN_DOWNLOAD_DIR", None)
                    else:
                        os.environ["FEICHUAN_DOWNLOAD_DIR"] = _old_download_dir2
        finally:
            setattr(DouyinCapture, "_download_http", _old_dl2)
            if _old_download_dir2 is None:
                os.environ.pop("FEICHUAN_DOWNLOAD_DIR", None)
            else:
                os.environ["FEICHUAN_DOWNLOAD_DIR"] = _old_download_dir2
    with tempfile.TemporaryDirectory() as temporary:
        old_settings = os.environ.get("FEICHUAN_SETTINGS_PATH")
        try:
            settings_file = Path(temporary) / "settings.json"
            selected = Path(temporary) / "accessible-downloads"
            os.environ["FEICHUAN_SETTINGS_PATH"] = str(settings_file)
            assert get_download_dir().name == "Downloads"
            assert get_quality_mode() == QUALITY_MODE_BEST
            assert set_quality_mode(QUALITY_MODE_ASK_EACH_TIME) == QUALITY_MODE_ASK_EACH_TIME
            assert set_download_dir(selected) == selected.resolve()
            assert get_download_dir() == selected.resolve()
            payload = settings_file.read_text(encoding="utf-8")
            assert "accessible-downloads" in payload
            assert QUALITY_MODE_ASK_EACH_TIME in payload
        finally:
            if old_settings is None:
                os.environ.pop("FEICHUAN_SETTINGS_PATH", None)
            else:
                os.environ["FEICHUAN_SETTINGS_PATH"] = old_settings
    no_endpoint = check_software_update("")
    assert no_endpoint.ok and not no_endpoint.available

    ytdlp = ROOT / "tools" / "yt-dlp.exe"
    checksum_file = ROOT / "tools" / "SHA2-256SUMS"
    assert ytdlp.exists() and checksum_file.exists()
    digest = hashlib.sha256(ytdlp.read_bytes()).hexdigest()
    listed = CoreUpdater._checksum_for(checksum_file.read_text(encoding="utf-8"), "yt-dlp.exe")
    assert listed and listed.lower() == digest.lower()
    assert CoreUpdater().current_version() != "未安装"
    try:
        Downloader().download("not-a-url")
    except DownloadError as exc:
        assert "有效" in str(exc)
    else:
        raise AssertionError("invalid URL did not fail safely")
    print("smoke checks passed")


if __name__ == "__main__":
    main()
