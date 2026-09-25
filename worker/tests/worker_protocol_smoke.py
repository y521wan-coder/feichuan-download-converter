from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from feichuan_downloader.protocol import (  # noqa: E402
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    OperationRegistry,
    ProtocolError,
    decode_request,
    encode_message,
)
from feichuan_downloader.downloader import DownloadResult  # noqa: E402
from feichuan_downloader.models import DownloadEvent, DownloadStage  # noqa: E402
from feichuan_downloader.worker import WorkerService  # noqa: E402


def request(request_id: str, message_type: str, payload: dict[str, object]) -> bytes:
    return encode_message(request_id, message_type, payload)


def check_protocol_guards() -> None:
    decoded = decode_request(request("one", "hello", {}))
    assert decoded.request_id == "one"
    assert decoded.message_type == "hello"
    for payload in (
        {"secret_key": "must-not-cross"},
        {"nested": {"cookie": "must-not-cross"}},
        {"text": "https://media.example/video?q-signature=must-not-cross"},
        {"text": "https://media.xet.tech/replay.m3u8?sign=redacted-test-value"},
    ):
        try:
            encode_message("blocked", "test", payload)
        except ProtocolError as exc:
            assert exc.code in {"sensitive_field", "sensitive_value"}
        else:
            raise AssertionError("sensitive protocol value was accepted")


def check_operation_registry() -> None:
    sensitive = object()
    registry = OperationRegistry()
    operation_id = registry.add(sensitive)
    assert operation_id.startswith("op_")
    assert registry.get(operation_id) is sensitive
    assert registry.remove(operation_id) is sensitive
    try:
        registry.get(operation_id)
    except ProtocolError as exc:
        assert exc.code == "operation_not_found"
    else:
        raise AssertionError("expired operation ID remained available")


def check_subprocess_round_trip() -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(SRC)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["FEICHUAN_AUTO_UPDATE_CORE"] = "0"
    environment["FEICHUAN_SOFTWARE_UPDATE_ENDPOINT"] = ""
    with tempfile.TemporaryDirectory() as temporary:
        environment["FEICHUAN_SETTINGS_PATH"] = str(Path(temporary) / "settings.json")
        process = subprocess.Popen(
            [sys.executable, str(SRC / "worker_main.py")],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdin is not None
        assert process.stdout is not None

        def exchange(encoded: bytes) -> dict[str, object]:
            process.stdin.write(encoded)
            process.stdin.flush()
            line = process.stdout.readline()
            assert line, "worker closed stdout before responding"
            return json.loads(line.decode("utf-8"))

        hello = exchange(request("hello-1", "hello", {}))
        assert hello["protocol"] == PROTOCOL_NAME
        assert hello["version"] == PROTOCOL_VERSION
        assert hello["type"] == "hello.result"
        assert hello["payload"]["worker_version"] == "1.1"
        assert "douyin.note.images" in hello["payload"]["capabilities"]
        assert "xiaoe.capture.download" in hello["payload"]["capabilities"]

        classified = exchange(
            request(
                "classify-1",
                "link.classify",
                {"text": "复制打开 https://v.douyin.com/example/ 试试看"},
            )
        )
        assert classified["type"] == "link.classify.result"
        assert classified["payload"]["source"] == "single_link"
        assert classified["payload"]["url"] == "https://v.douyin.com/example/"

        shutdown = exchange(request("shutdown-1", "shutdown", {}))
        assert shutdown["type"] == "shutdown.result"
        assert process.wait(timeout=10) == 0
        stderr = process.stderr.read() if process.stderr is not None else b""
        assert not stderr


def check_background_download_and_progress() -> None:
    emitted: list[dict[str, object]] = []
    completed = threading.Event()

    def emit(message: bytes) -> None:
        decoded = json.loads(message.decode("utf-8"))
        emitted.append(decoded)
        if decoded["type"] == "task.start.result":
            completed.set()

    class FakeCoordinator:
        last_note_content = ""
        def __init__(self, *, on_event=None, on_line=None) -> None:
            self.on_event = on_event
            self.on_line = on_line
            self.busy = False

        def scan_or_download(self, _text, **_kwargs):
            type(self).last_note_content = str(
                _kwargs.get("douyin_note_content") or ""
            )
            self.busy = True
            try:
                if self.on_event:
                    self.on_event(
                        DownloadEvent(
                            stage=DownloadStage.DOWNLOADING,
                            current=1,
                            total=1,
                            overall_percent=50,
                            current_file=r"D:\private\sample.mp4",
                            message="正在下载。",
                        )
                    )
                return DownloadResult(Path(r"D:\output\sample.mp4"), title="sample")
            finally:
                self.busy = False

        def cancel(self):
            return None

    service = WorkerService(coordinator_factory=FakeCoordinator, emit=emit)
    decoded = decode_request(request("task-1", "task.start", {"text": "https://example.com"}))
    assert service.dispatch(decoded) is None
    assert completed.wait(5), "background worker did not return"
    progress = next(message for message in emitted if message["type"] == "task.progress")
    assert progress["id"] != "task-1"
    assert progress["payload"]["request_id"] == "task-1"
    assert progress["payload"]["current_file"] == "sample.mp4"
    result = next(message for message in emitted if message["type"] == "task.start.result")
    assert result["payload"]["kind"] == "download"
    assert result["payload"]["paths"] == [r"D:\output\sample.mp4"]
    assert FakeCoordinator.last_note_content == "audio_only"

    try:
        service._task_start(
            {
                "text": "https://example.com",
                "douyin_note_content": "unsupported",
            }
        )
    except ProtocolError as exc:
        assert exc.code == "invalid_douyin_note_content"
    else:
        raise AssertionError("invalid note content value was accepted")

    partial_payload = service._download_result_payload(
        DownloadResult(
            Path(r"D:\output\sample.m4a"),
            partial_success=True,
            warnings=("图片：离线失败",),
        )
    )
    assert partial_payload["partial_success"] is True
    assert partial_payload["warnings"] == ["图片：离线失败"]


def check_cancel_reaches_idle_status() -> None:
    started = threading.Event()
    released = threading.Event()

    class CancellableCoordinator:
        def __init__(self, *, on_event=None, on_line=None) -> None:
            self.busy = False

        def scan_or_download(self, _text, **_kwargs):
            self.busy = True
            started.set()
            released.wait(5)
            self.busy = False
            return DownloadResult(Path(r"D:\output\cancelled.mp4"), title="cancelled")

        def cancel(self):
            released.set()

    service = WorkerService(coordinator_factory=CancellableCoordinator)
    task = decode_request(request("task-cancel", "task.start", {"text": "https://example.com"}))
    assert service.dispatch(task) is None
    assert started.wait(2)
    _kind, status = service.handle(decode_request(request("status-1", "task.status", {})))
    assert status["busy"] is True
    service.handle(decode_request(request("cancel-1", "cancel", {})))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        _kind, status = service.handle(
            decode_request(request("status-2", "task.status", {}))
        )
        if not status["busy"]:
            break
        time.sleep(0.02)
    assert status["busy"] is False


def main() -> None:
    check_protocol_guards()
    check_operation_registry()
    check_subprocess_round_trip()
    check_background_download_and_progress()
    check_cancel_reaches_idle_status()
    print("worker protocol smoke passed")


if __name__ == "__main__":
    main()
