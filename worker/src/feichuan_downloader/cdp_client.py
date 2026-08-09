"""Small, thread-safe Chrome DevTools Protocol client.

The old capture prototype consumed websocket messages inline and therefore could
not reliably match a command response to the command that produced it.  Batch
enumeration needs both command/response correlation and an independent stream
of network events, so this module owns a single receiver thread and exposes the
two channels separately.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from websocket import WebSocket
from websocket._exceptions import (
    WebSocketConnectionClosedException,
    WebSocketTimeoutException,
)


class CdpError(RuntimeError):
    """Base class for CDP transport and protocol errors."""


class CdpTimeoutError(CdpError):
    """A CDP command did not receive a response before its deadline."""


class CdpProtocolError(CdpError):
    """Chrome returned an ``error`` object for a command."""

    def __init__(self, method: str, error: dict[str, Any]) -> None:
        code = error.get("code", "?")
        message = str(error.get("message", "未知 CDP 错误"))
        super().__init__(f"{method} 失败（{code}）：{message}")
        self.method = method
        self.error = dict(error)


@dataclass
class _PendingCommand:
    event: threading.Event
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class _BoundedEventBuffer:
    """Bounded FIFO that protects critical events from ordinary queue noise."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._events: deque[tuple[dict[str, Any], bool]] = deque()
        self._closed = False
        self._condition = threading.Condition()

    def offer(self, message: dict[str, Any], *, critical: bool) -> tuple[bool, bool]:
        """Offer an event and report ``(event_dropped, critical_dropped)``."""

        dropped = False
        critical_dropped = False
        with self._condition:
            if len(self._events) >= self.capacity:
                ordinary_index = next(
                    (
                        index
                        for index, (_event, is_critical) in enumerate(self._events)
                        if not is_critical
                    ),
                    None,
                )
                if ordinary_index is not None:
                    del self._events[ordinary_index]
                    dropped = True
                elif not critical:
                    # The buffer contains only critical events.  An ordinary
                    # event must never evict one of them.
                    return True, False
                else:
                    # A bounded buffer cannot retain an unlimited stream of
                    # critical events.  Keep the newest event and make the loss
                    # observable to callers through CdpClient's counters.
                    self._events.popleft()
                    dropped = True
                    critical_dropped = True
            self._events.append((message, critical))
            self._condition.notify()
        return dropped, critical_dropped

    def get(self, timeout: float | None = None) -> dict[str, Any] | None:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while not self._events and not self._closed:
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                self._condition.wait(remaining)
            if self._events:
                message, _critical = self._events.popleft()
                return message
            return None

    def get_nowait(self) -> dict[str, Any] | None:
        with self._condition:
            if self._events:
                message, _critical = self._events.popleft()
                return message
            if self._closed:
                return None
            raise queue.Empty

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()


class CdpClient:
    """Correlate CDP commands while retaining an ordered event stream."""

    # These bookkeeping events are emitted for almost every network resource,
    # but neither the batch enumerator nor the single-item capture path consumes
    # them.  Filtering them before they reach the bounded queue prevents images,
    # scripts, and analytics traffic from hiding pagination responses.
    _IGNORED_EVENT_METHODS = frozenset(
        {
            "Network.dataReceived",
            "Network.requestWillBeSentExtraInfo",
            "Network.responseReceivedExtraInfo",
            "Network.resourceChangedPriority",
        }
    )
    _CRITICAL_EVENT_METHODS = frozenset(
        {
            "Network.responseReceived",
            "Network.loadingFinished",
            "Network.loadingFailed",
            "Page.frameNavigated",
        }
    )

    def __init__(self, websocket: WebSocket, *, event_capacity: int = 4096) -> None:
        if event_capacity < 1:
            raise ValueError("event_capacity must be positive")
        self.websocket = websocket
        self._event_queue = _BoundedEventBuffer(event_capacity)
        self._send_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._event_stats_lock = threading.Lock()
        self._pending: dict[int, _PendingCommand] = {}
        self._next_id = 0
        self._filtered_event_count = 0
        self._dropped_event_count = 0
        self._dropped_critical_event_count = 0
        self._closed = threading.Event()
        self._receiver = threading.Thread(
            target=self._receive_loop,
            name="feichuan-cdp-receiver",
            daemon=True,
        )
        self._receiver.start()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    @property
    def filtered_event_count(self) -> int:
        """Number of known high-frequency bookkeeping events discarded."""

        with self._event_stats_lock:
            return self._filtered_event_count

    @property
    def dropped_event_count(self) -> int:
        """Number of events discarded because the bounded buffer was full."""

        with self._event_stats_lock:
            return self._dropped_event_count

    @property
    def dropped_critical_event_count(self) -> int:
        """Number of critical events lost when the buffer held only critical events."""

        with self._event_stats_lock:
            return self._dropped_critical_event_count

    @property
    def event_overflowed(self) -> bool:
        return self.dropped_event_count > 0

    @property
    def critical_event_loss_detected(self) -> bool:
        return self.dropped_critical_event_count > 0

    def command(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """Send a command and return only its ``result`` object."""

        if not method:
            raise ValueError("CDP method must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if self.closed:
            raise CdpError("CDP 连接已经关闭。")

        pending = _PendingCommand(threading.Event())
        with self._send_lock:
            if self.closed:
                raise CdpError("CDP 连接已经关闭。")
            self._next_id += 1
            command_id = self._next_id
            with self._pending_lock:
                self._pending[command_id] = pending
            payload = {
                "id": command_id,
                "method": method,
                "params": params or {},
            }
            try:
                self.websocket.send(json.dumps(payload, ensure_ascii=False))
            except Exception as exc:
                with self._pending_lock:
                    self._pending.pop(command_id, None)
                raise CdpError(f"发送 CDP 命令失败：{type(exc).__name__}") from exc

        if not pending.event.wait(timeout):
            with self._pending_lock:
                self._pending.pop(command_id, None)
            raise CdpTimeoutError(f"等待 {method} 响应超时。")
        if pending.error:
            raise CdpProtocolError(method, pending.error)
        return dict(pending.result or {})

    def next_event(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Return the next CDP event, or ``None`` on timeout/closed transport."""

        try:
            value = self._event_queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if value is None:
            return None
        return dict(value)

    def drain_events(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        while True:
            try:
                value = self._event_queue.get_nowait()
            except queue.Empty:
                break
            if value is None:
                break
            events.append(dict(value))
        return events

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self.websocket.close()
        except Exception:
            pass
        self._fail_pending()
        self._offer_sentinel()
        if threading.current_thread() is not self._receiver:
            self._receiver.join(timeout=2)

    def _receive_loop(self) -> None:
        transport_error: Exception | None = None
        try:
            while not self._closed.is_set():
                try:
                    raw = self.websocket.recv()
                except WebSocketTimeoutException:
                    continue
                except WebSocketConnectionClosedException as exc:
                    transport_error = exc
                    break
                except Exception as exc:
                    transport_error = exc
                    break
                if not raw:
                    continue
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                try:
                    message = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                command_id = message.get("id")
                if isinstance(command_id, int):
                    with self._pending_lock:
                        pending = self._pending.pop(command_id, None)
                    if pending:
                        result = message.get("result")
                        error = message.get("error")
                        pending.result = result if isinstance(result, dict) else {}
                        pending.error = error if isinstance(error, dict) else None
                        pending.event.set()
                    continue
                if isinstance(message.get("method"), str):
                    method = str(message["method"])
                    if method in self._IGNORED_EVENT_METHODS:
                        with self._event_stats_lock:
                            self._filtered_event_count += 1
                        continue
                    self._offer_event(message, critical=method in self._CRITICAL_EVENT_METHODS)
        finally:
            self._closed.set()
            self._fail_pending(transport_error)
            self._offer_sentinel()

    def _offer_event(self, message: dict[str, Any], *, critical: bool) -> None:
        dropped, critical_dropped = self._event_queue.offer(
            message,
            critical=critical,
        )
        if not dropped:
            return
        with self._event_stats_lock:
            self._dropped_event_count += 1
            if critical_dropped:
                self._dropped_critical_event_count += 1

    def _offer_sentinel(self) -> None:
        self._event_queue.close()

    def _fail_pending(self, _cause: Exception | None = None) -> None:
        with self._pending_lock:
            pending_values = list(self._pending.values())
            self._pending.clear()
        for pending in pending_values:
            pending.error = {"code": -1, "message": "CDP 连接已经关闭"}
            pending.event.set()

    def __enter__(self) -> "CdpClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
