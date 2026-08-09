"""Offline command-correlation and event-queue checks for CdpClient."""

from __future__ import annotations

import json
import queue
import sys
import threading
from pathlib import Path

from websocket._exceptions import WebSocketConnectionClosedException, WebSocketTimeoutException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from feichuan_downloader.cdp_client import CdpClient, CdpProtocolError


class FakeWebSocket:
    def __init__(self) -> None:
        self.incoming: queue.Queue[str | object] = queue.Queue()
        self.sent: list[dict[str, object]] = []
        self.closed = False

    def send(self, raw: str) -> None:
        message = json.loads(raw)
        self.sent.append(message)
        command_id = int(message["id"])
        method = str(message["method"])
        if method == "Fail.test":
            response = {"id": command_id, "error": {"code": 42, "message": "expected"}}
        else:
            response = {"id": command_id, "result": {"echo": method}}
        self.incoming.put(json.dumps(response))

    def recv(self) -> str:
        if self.closed:
            raise WebSocketConnectionClosedException("closed")
        try:
            value = self.incoming.get(timeout=0.1)
        except queue.Empty as exc:
            raise WebSocketTimeoutException("timeout") from exc
        return str(value)

    def close(self) -> None:
        self.closed = True


def main() -> None:
    check_command_correlation()
    check_filtered_noise_does_not_hide_critical_events()
    check_ordinary_overflow_preserves_critical_events()
    check_critical_overflow_is_detectable()
    print("cdp client smoke passed")


def check_command_correlation() -> None:
    websocket = FakeWebSocket()
    client = CdpClient(websocket)  # type: ignore[arg-type]
    try:
        results: dict[str, str] = {}

        def invoke(name: str) -> None:
            results[name] = str(client.command(name)["echo"])

        threads = [threading.Thread(target=invoke, args=(f"Test.{index}",)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        assert results == {f"Test.{index}": f"Test.{index}" for index in range(8)}

        websocket.incoming.put(json.dumps({"method": "Network.responseReceived", "params": {"x": 1}}))
        event = client.next_event(timeout=1)
        assert event and event["method"] == "Network.responseReceived"
        try:
            client.command("Fail.test")
        except CdpProtocolError as exc:
            assert "42" in str(exc)
        else:
            raise AssertionError("protocol error was not raised")
    finally:
        client.close()


def check_filtered_noise_does_not_hide_critical_events() -> None:
    websocket = FakeWebSocket()
    client = CdpClient(websocket)  # type: ignore[arg-type]
    noise_methods = (
        "Network.dataReceived",
        "Network.requestWillBeSentExtraInfo",
        "Network.responseReceivedExtraInfo",
        "Network.resourceChangedPriority",
    )
    critical_events = (
        {"method": "Network.responseReceived", "params": {"requestId": "page-1"}},
        {"method": "Network.loadingFinished", "params": {"requestId": "page-1"}},
        {"method": "Network.loadingFailed", "params": {"requestId": "page-2"}},
        {"method": "Page.frameNavigated", "params": {"frame": {"id": "main"}}},
    )
    noise_count = 5000
    try:
        for index in range(noise_count):
            if index in {500, 1500, 2500, 3500}:
                websocket.incoming.put(json.dumps(critical_events[index // 1000]))
            websocket.incoming.put(
                json.dumps(
                    {
                        "method": noise_methods[index % len(noise_methods)],
                        "params": {"requestId": f"noise-{index}"},
                    }
                )
            )

        # A command response is a FIFO barrier proving that the receiver has
        # processed every event queued above it.
        assert client.command("Barrier.filtered", timeout=5)["echo"] == "Barrier.filtered"
        events = client.drain_events()
        assert [event["method"] for event in events] == [
            event["method"] for event in critical_events
        ]
        assert client.filtered_event_count == noise_count
        assert client.dropped_event_count == 0
        assert not client.critical_event_loss_detected
    finally:
        client.close()


def check_ordinary_overflow_preserves_critical_events() -> None:
    websocket = FakeWebSocket()
    client = CdpClient(websocket, event_capacity=8)  # type: ignore[arg-type]
    critical_methods = (
        "Network.responseReceived",
        "Network.loadingFinished",
        "Network.loadingFailed",
        "Page.frameNavigated",
    )
    try:
        for index in range(80):
            websocket.incoming.put(
                json.dumps(
                    {
                        "method": "Runtime.consoleAPICalled",
                        "params": {"index": index},
                    }
                )
            )
            if index < len(critical_methods):
                websocket.incoming.put(
                    json.dumps(
                        {
                            "method": critical_methods[index],
                            "params": {"index": index},
                        }
                    )
                )

        client.command("Barrier.ordinary", timeout=5)
        methods = [event["method"] for event in client.drain_events()]
        assert all(method in methods for method in critical_methods)
        assert client.event_overflowed
        assert client.dropped_event_count > 0
        assert client.dropped_critical_event_count == 0
    finally:
        client.close()


def check_critical_overflow_is_detectable() -> None:
    websocket = FakeWebSocket()
    client = CdpClient(websocket, event_capacity=2)  # type: ignore[arg-type]
    try:
        for index in range(3):
            websocket.incoming.put(
                json.dumps(
                    {
                        "method": "Network.responseReceived",
                        "params": {"requestId": f"critical-{index}"},
                    }
                )
            )
        client.command("Barrier.critical", timeout=5)
        assert client.dropped_event_count == 1
        assert client.dropped_critical_event_count == 1
        assert client.critical_event_loss_detected
        assert [
            event["params"]["requestId"] for event in client.drain_events()
        ] == ["critical-1", "critical-2"]
    finally:
        client.close()


if __name__ == "__main__":
    main()
