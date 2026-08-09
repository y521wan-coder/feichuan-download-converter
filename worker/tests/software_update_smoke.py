"""验证正式软件更新协议、下载和 SHA-256 失败闭锁。"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from feichuan_downloader.software_updater import (  # noqa: E402
    check_software_update,
    download_update_installer,
    launch_update_installer,
)


PACKAGE = b"MZ" + b"feichuan-installer-smoke" * 128
PACKAGE_SHA = hashlib.sha256(PACKAGE).hexdigest()


class Handler(BaseHTTPRequestHandler):
    mode = "404"
    last_query: dict[str, list[str]] = {}
    last_package_request = ""

    def do_GET(self) -> None:  # noqa: N802 - stdlib API name
        parts = urlsplit(self.path)
        if parts.path == "/packages/server-provided.exe":
            Handler.last_package_request = self.path
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(PACKAGE)))
            self.end_headers()
            self.wfile.write(PACKAGE)
            return

        Handler.last_query = parse_qs(parts.query)
        if Handler.mode == "404":
            body = json.dumps({"code": 4041, "message": "product not found"}).encode()
            self.send_response(404)
        elif Handler.mode == "malformed":
            body = json.dumps(
                {
                    "code": 0,
                    "message": "ok",
                    "data": {
                        "update_available": True,
                        "latest_version": "0.3.1",
                        "sha256": PACKAGE_SHA,
                    },
                }
            ).encode()
            self.send_response(200)
        else:
            current = Handler.last_query.get("current_version", [""])[0]
            update_available = current != "0.3.1"
            data = {
                "update_available": update_available,
                "latest_version": "0.3.1",
                "release_notes": "installer smoke",
                "download_url": (
                    f"http://127.0.0.1:{self.server.server_port}"
                    "/packages/server-provided.exe?opaque=server-value"
                    if update_available
                    else None
                ),
                "file_size": len(PACKAGE),
                "sha256": PACKAGE_SHA if update_available else None,
                "published_at": "2026-07-18T00:00:00Z",
            }
            body = json.dumps(
                {
                    "code": 0,
                    "message": "ok",
                    "data": data,
                    # A top-level decoy proves the client consumes data.download_url
                    # verbatim instead of guessing or reading another field.
                    "download_url": "http://127.0.0.1/must-not-be-used.exe",
                }
            ).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/check"
    try:
        Handler.mode = "404"
        result = check_software_update(endpoint, current_version="0.3.0")
        assert result.ok and not result.available

        Handler.mode = "update"
        result = check_software_update(endpoint, current_version="0.3.0")
        assert result.ok and result.available
        assert result.download_url.endswith(
            "/packages/server-provided.exe?opaque=server-value"
        )
        assert result.sha256 == PACKAGE_SHA
        assert result.file_size == len(PACKAGE)
        assert Handler.last_query["product_key"] == ["feichuan_download_tool"]
        assert Handler.last_query["platform"] == ["windows"]
        assert Handler.last_query["channel"] == ["stable"]
        assert Handler.last_query["current_version"] == ["0.3.0"]

        with tempfile.TemporaryDirectory() as temporary:
            downloaded = download_update_installer(result, temporary)
            assert downloaded.path.name == "飞船下载工具-Setup-0.3.1.exe"
            assert downloaded.path.read_bytes() == PACKAGE
            assert downloaded.sha256 == PACKAGE_SHA
            assert Handler.last_package_request.endswith(
                "/packages/server-provided.exe?opaque=server-value"
            )

            with patch(
                "feichuan_downloader.software_updater.subprocess.Popen"
            ) as popen:
                launch_update_installer(
                    downloaded.path,
                    expected_sha256=downloaded.sha256,
                )
                popen.assert_called_once()

            downloaded.path.write_bytes(PACKAGE + b"tampered-after-download")
            with patch(
                "feichuan_downloader.software_updater.subprocess.Popen"
            ) as popen:
                try:
                    launch_update_installer(
                        downloaded.path,
                        expected_sha256=downloaded.sha256,
                    )
                except RuntimeError as exc:
                    assert "SHA-256" in str(exc)
                else:
                    raise AssertionError("tampered installer was allowed to launch")
                popen.assert_not_called()

            broken = replace(result, sha256="0" * 64)
            try:
                download_update_installer(broken, Path(temporary) / "broken")
            except RuntimeError as exc:
                assert "SHA-256" in str(exc)
            else:
                raise AssertionError("checksum mismatch was not rejected")
            assert not list((Path(temporary) / "broken").glob("*.part"))

        current = check_software_update(endpoint, current_version="0.3.1")
        assert current.ok and not current.available
        assert not current.download_url

        Handler.mode = "malformed"
        malformed = check_software_update(endpoint, current_version="0.3.0")
        assert not malformed.ok and not malformed.available
        assert "download_url" in malformed.message
    finally:
        server.shutdown()
        server.server_close()
    print("software update smoke passed")


if __name__ == "__main__":
    main()
