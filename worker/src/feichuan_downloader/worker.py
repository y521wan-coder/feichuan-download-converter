"""Headless stdin/stdout worker entry point for the unified WinForms shell."""

from __future__ import annotations

import sys
from pathlib import Path
import secrets
import threading
from typing import Any, Callable, Mapping

from .config import (
    QUALITY_MODE_ASK_EACH_TIME,
    QUALITY_MODE_BEST,
    get_download_dir,
    get_quality_mode,
    set_download_dir,
    set_quality_mode,
)
from .core_updater import CoreUpdater
from .coordinator import (
    BatchDownloadSummary,
    DownloadCoordinator,
    PreparedGenericDownload,
    PreparedScan,
)
from .douyin_session import DouyinSessionProvider
from .downloader import DownloadResult, Downloader
from .models import DownloadEvent
from .protocol import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    WORKER_VERSION,
    OperationRegistry,
    ProtocolError,
    Request,
    decode_request,
    encode_error,
    encode_message,
    sanitize_public_text,
)
from .quality import choices_from_media_descriptors
from .software_updater import check_software_update


class WorkerService:
    """Small command surface that delegates download work to the existing coordinator."""

    def __init__(
        self,
        coordinator_factory: Callable[..., DownloadCoordinator] = DownloadCoordinator,
        emit: Callable[[bytes], None] | None = None,
    ) -> None:
        self.operations = OperationRegistry()
        self._emit = emit or (lambda _message: None)
        self.coordinator = coordinator_factory(
            on_event=self._on_download_event,
            on_line=self._on_log_line,
        )
        self.shutdown_requested = False
        self._task_lock = threading.RLock()
        self._task_thread: threading.Thread | None = None
        self._active_request_id = ""

    def dispatch(self, request: Request) -> tuple[str, Mapping[str, Any]] | None:
        if request.message_type in {
            "task.start",
            "prepared.download",
            "generic.download",
            "quality.inspect",
            "core_update.check",
            "software_update.check",
        }:
            self._start_background(request)
            return None
        return self.handle(request)

    def handle(self, request: Request) -> tuple[str, Mapping[str, Any]]:
        handlers = {
            "hello": self._hello,
            "link.classify": self._classify,
            "download_directory.get": self._get_download_directory,
            "download_directory.set": self._set_download_directory,
            "quality_mode.get": self._get_quality_mode,
            "quality_mode.set": self._set_quality_mode,
            "quality.query": self._quality_query,
            "task.status": self._task_status,
            "douyin_login.status": self._douyin_login_status,
            "douyin_login.clear": self._douyin_login_clear,
            "operation.release": self._release_operation,
            "cancel": self._cancel,
            "shutdown": self._shutdown,
        }
        try:
            handler = handlers[request.message_type]
        except KeyError as exc:
            raise ProtocolError("unsupported_message", "当前工作进程不支持这条命令。") from exc
        return f"{request.message_type}.result", handler(request.payload)

    @staticmethod
    def _hello(_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "worker_version": WORKER_VERSION,
            "protocol_name": PROTOCOL_NAME,
            "protocol_version": PROTOCOL_VERSION,
            "capabilities": [
                "hello",
                "link.classify",
                "download_directory.get",
                "download_directory.set",
                "quality_mode.get",
                "quality_mode.set",
                "quality.inspect",
                "task.start",
                "quality.query",
                "task.status",
                "prepared.download",
                "generic.download",
                "operation.release",
                "douyin_login.status",
                "douyin_login.clear",
                "core_update.check",
                "software_update.check",
                "cancel",
                "shutdown",
            ],
        }

    def _classify(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        text = str(payload.get("text") or "").strip()
        if not text:
            raise ProtocolError("missing_text", "请输入下载链接或平台分享文本。")
        try:
            source, url = self.coordinator.classify(text)
        except Exception as exc:
            raise ProtocolError("invalid_link", "没有识别到支持的下载链接。") from exc
        return {"source": source.value, "url": url}

    @staticmethod
    def _get_download_directory(_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"path": str(get_download_dir())}

    @staticmethod
    def _set_download_directory(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        path = str(payload.get("path") or "").strip()
        if not path:
            raise ProtocolError("missing_path", "下载目录不能为空。")
        try:
            selected = set_download_dir(path)
        except (OSError, ValueError) as exc:
            raise ProtocolError("invalid_directory", "无法使用所选下载目录。") from exc
        return {"path": str(selected)}

    def _cancel(self, _payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.coordinator.cancel()
        return {"accepted": True, "busy": self.coordinator.busy}

    def _task_status(self, _payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"busy": self._background_busy() or self.coordinator.busy}

    @staticmethod
    def _get_quality_mode(_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"mode": get_quality_mode()}

    @staticmethod
    def _set_quality_mode(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        mode = str(payload.get("mode") or "").strip()
        if mode not in {QUALITY_MODE_BEST, QUALITY_MODE_ASK_EACH_TIME}:
            raise ProtocolError("invalid_quality_mode", "未知的品质选择模式。")
        set_quality_mode(mode)
        return {"mode": get_quality_mode()}

    @staticmethod
    def _douyin_login_status(_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        from .config import douyin_chromium_profile_dir

        return {"saved": douyin_chromium_profile_dir().exists()}

    @staticmethod
    def _douyin_login_clear(_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            removed = DouyinSessionProvider.clear_saved_login_profile()
        except Exception as exc:
            raise ProtocolError("douyin_login_clear_failed", sanitize_public_text(exc)) from exc
        return {"removed": bool(removed)}

    def _quality_query(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        operation_id = str(payload.get("operation_id") or "").strip()
        prepared = self.operations.get(operation_id)
        if isinstance(prepared, PreparedScan):
            descriptors = tuple(
                descriptor
                for items in prepared.media_by_work_id.values()
                for descriptor in items
            )
            choices = choices_from_media_descriptors(descriptors)
        else:
            choices = ()
        return {
            "operation_id": operation_id,
            "choices": [
                {"value": choice.preference.value, "label": choice.label}
                for choice in choices
            ],
        }

    def _release_operation(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        operation_id = str(payload.get("operation_id") or "").strip()
        value = self.operations.remove(operation_id)
        if value is not None:
            clear = getattr(value, "clear_sensitive", None)
            if callable(clear):
                clear()
        return {"released": value is not None}

    def _shutdown(self, _payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if self._background_busy() or self.coordinator.busy:
            raise ProtocolError("worker_busy", "任务仍在进行，不能直接关闭工作进程。")
        self.operations.clear()
        self.shutdown_requested = True
        return {"accepted": True}

    def _start_background(self, request: Request) -> None:
        with self._task_lock:
            if self._background_busy() or self.coordinator.busy:
                raise ProtocolError("worker_busy", "已有下载或扫描任务正在进行。")
            self._active_request_id = request.request_id
            self._task_thread = threading.Thread(
                target=self._run_background,
                args=(request,),
                daemon=True,
                name="feichuan-worker-task",
            )
            self._task_thread.start()

    def _background_busy(self) -> bool:
        thread = self._task_thread
        return thread is not None and thread.is_alive()

    def _run_background(self, request: Request) -> None:
        try:
            if request.message_type == "task.start":
                payload = self._task_start(request.payload)
            elif request.message_type == "prepared.download":
                payload = self._prepared_download(request.payload)
            elif request.message_type == "generic.download":
                payload = self._generic_download(request.payload)
            elif request.message_type == "quality.inspect":
                payload = self._quality_inspect(request.payload)
            elif request.message_type == "core_update.check":
                payload = self._core_update_check(request.payload)
            else:
                payload = self._software_update_check(request.payload)
            response = encode_message(request.request_id, f"{request.message_type}.result", payload)
        except ProtocolError as exc:
            response = encode_error(request.request_id, exc)
        except Exception as exc:
            response = encode_error(
                request.request_id,
                ProtocolError("task_failed", sanitize_public_text(exc)),
            )
        self._emit(response)
        with self._task_lock:
            self._active_request_id = ""
            self._task_thread = None

    def _task_start(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        text = str(payload.get("text") or "").strip()
        if not text:
            raise ProtocolError("missing_text", "请输入下载链接或平台分享文本。")
        quality = str(payload.get("quality_preference") or "best").strip()
        outcome = self.coordinator.scan_or_download(
            text,
            interactive_douyin_login=bool(payload.get("interactive_douyin_login", False)),
            quality_preference=quality,
        )
        if isinstance(outcome, DownloadResult):
            return self._download_result_payload(outcome)
        if isinstance(outcome, PreparedGenericDownload):
            operation_id = self.operations.add(outcome)
            inspection = outcome.inspection
            return {
                "kind": "generic_confirmation",
                "operation_id": operation_id,
                "inspection": None
                if inspection is None
                else {
                    "is_playlist": inspection.is_playlist,
                    "count": inspection.count,
                    "title": inspection.title,
                    "entries_preview": list(inspection.entries_preview),
                },
                "inspection_error": sanitize_public_text(outcome.inspection_error),
            }
        if isinstance(outcome, PreparedScan):
            operation_id = self.operations.add(outcome)
            previews = self.coordinator.preview_prepared(outcome)
            return {
                "kind": "batch_confirmation",
                "operation_id": operation_id,
                "scan": {
                    "source": outcome.result.source.value,
                    "author": outcome.result.author,
                    "source_title": outcome.result.source_title,
                    "reported_count": outcome.result.reported_count,
                    "unique_count": outcome.result.unique_count,
                    "content_counts": dict(outcome.result.content_counts),
                    "enumeration_complete": outcome.result.enumeration_complete,
                    "incomplete_reason": sanitize_public_text(outcome.result.incomplete_reason),
                    "items": [
                        {
                            "work_id": item.work_id,
                            "content_type": item.content_type.value,
                            "title": item.title,
                            "author": item.author,
                        }
                        for item in outcome.result.items
                    ],
                },
                "choices": [choice.value for choice in outcome.result.confirmation_choices],
                "previews": [
                    {
                        "choice": preview.choice.value,
                        "download_count": preview.download_count,
                        "skip_count": preview.skip_count,
                        "unsupported_count": preview.unsupported_count,
                    }
                    for preview in previews
                ],
            }
        raise ProtocolError("unexpected_result", "下载工作进程返回了未知结果。")

    def _prepared_download(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        operation_id = str(payload.get("operation_id") or "").strip()
        prepared = self.operations.get(operation_id)
        if not isinstance(prepared, PreparedScan):
            raise ProtocolError("invalid_operation", "当前操作不是批量扫描结果。")
        try:
            summary = self.coordinator.download_prepared(
                prepared,
                str(payload.get("choice") or ""),
                quality_preference=str(payload.get("quality_preference") or "best"),
            )
            return self._batch_result_payload(summary)
        finally:
            self.operations.remove(operation_id)
            prepared.clear_sensitive()

    def _generic_download(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        operation_id = str(payload.get("operation_id") or "").strip()
        prepared = self.operations.get(operation_id)
        if not isinstance(prepared, PreparedGenericDownload):
            raise ProtocolError("invalid_operation", "当前操作不是普通网站确认结果。")
        try:
            result = self.coordinator.download_generic(
                prepared,
                str(payload.get("playlist_mode") or ""),
                quality_preference=str(payload.get("quality_preference") or "best"),
            )
            return self._download_result_payload(result)
        finally:
            self.operations.remove(operation_id)

    def _quality_inspect(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        text = str(payload.get("text") or "").strip()
        if not text:
            raise ProtocolError("missing_text", "请输入下载链接或平台分享文本。")
        try:
            _source, url = self.coordinator.classify(text)
            choices = Downloader().inspect_quality_choices(
                url,
                playlist_mode=str(payload.get("playlist_mode") or "single"),
                cancel_event=self.coordinator.cancel_event,
                on_line=self._on_log_line,
            )
        except Exception as exc:
            raise ProtocolError("quality_inspection_failed", sanitize_public_text(exc)) from exc
        return {
            "choices": [
                {"value": choice.preference.value, "label": choice.label}
                for choice in choices
            ]
        }

    def _core_update_check(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        updater = CoreUpdater(line_callback=self._on_log_line)
        result = updater.check_and_update(
            manual=True,
            install=bool(payload.get("install", False)),
        )
        return {
            "ok": result.ok,
            "updated": result.updated,
            "available": result.available,
            "current_version": result.current_version,
            "latest_version": result.latest_version,
            "message": sanitize_public_text(result.message),
        }

    @staticmethod
    def _software_update_check(_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        result = check_software_update()
        return {
            "ok": result.ok,
            "available": result.available,
            "latest_version": result.latest_version,
            "download_url": result.download_url,
            "sha256": result.sha256,
            "file_size": result.file_size,
            "release_notes": sanitize_public_text(result.release_notes, limit=8000),
            "message": sanitize_public_text(result.message),
        }

    @staticmethod
    def _download_result_payload(result: DownloadResult) -> Mapping[str, Any]:
        paths = tuple(getattr(result, "paths", ()) or (result.path,))
        return {
            "kind": "download",
            "paths": [str(path) for path in paths],
            "title": result.title,
            "used_douyin_fallback": result.used_douyin_fallback,
        }

    @staticmethod
    def _batch_result_payload(summary: BatchDownloadSummary) -> Mapping[str, Any]:
        return {
            "kind": "download",
            "total": summary.total,
            "succeeded": summary.succeeded,
            "skipped": summary.skipped,
            "failed": summary.failed,
            "paths": [str(path) for path in summary.paths],
        }

    def _on_download_event(self, event: DownloadEvent) -> None:
        request_id = self._active_request_id
        if not request_id:
            return
        current_file = Path(event.current_file).name if event.current_file else ""
        self._emit_event(
            "task.progress",
            {
                "request_id": request_id,
                "stage": event.stage.value,
                "scanned_count": event.scanned_count,
                "current": event.current,
                "total": event.total,
                "succeeded": event.succeeded,
                "skipped": event.skipped,
                "failed": event.failed,
                "current_file": current_file,
                "overall_percent": event.overall_percent,
                "message": sanitize_public_text(event.message),
            },
        )

    def _on_log_line(self, line: str) -> None:
        request_id = self._active_request_id
        if request_id:
            self._emit_event(
                "task.log",
                {"request_id": request_id, "message": sanitize_public_text(line)},
            )

    def _emit_event(self, message_type: str, payload: Mapping[str, Any]) -> None:
        event_id = "event_" + secrets.token_urlsafe(12)
        try:
            self._emit(encode_message(event_id, message_type, payload))
        except ProtocolError:
            self._emit(
                encode_message(
                    event_id,
                    "task.log",
                    {"request_id": self._active_request_id, "message": "状态信息已隐藏。"},
                )
            )


def _write_stdout(payload: bytes) -> None:
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def _drain_oversized_line() -> None:
    while True:
        chunk = sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1)
        if not chunk or chunk.endswith(b"\n"):
            return


def run() -> int:
    service = WorkerService(emit=_write_stdout)
    while not service.shutdown_requested:
        raw_line = sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1)
        if not raw_line:
            break
        if len(raw_line) > MAX_MESSAGE_BYTES:
            if not raw_line.endswith(b"\n"):
                _drain_oversized_line()
            _write_stdout(
                encode_error(
                    "unknown",
                    ProtocolError("message_too_large", "协议消息超过大小限制。"),
                )
            )
            continue
        request_id = "unknown"
        try:
            request = decode_request(raw_line)
            request_id = request.request_id
            dispatched = service.dispatch(request)
            if dispatched is None:
                continue
            response_type, response_payload = dispatched
            response = encode_message(request_id, response_type, response_payload)
        except Exception as exc:
            response = encode_error(request_id, exc)
        _write_stdout(response)
    service.operations.clear()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
