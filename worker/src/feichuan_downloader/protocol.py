"""Versioned JSON Lines protocol shared by the headless download worker.

Only public, non-sensitive metadata may cross this boundary. Browser state, cookies,
signed media URLs and tokens stay in objects retained by :class:`OperationRegistry`.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import secrets
import threading
from typing import Any, Mapping


PROTOCOL_NAME = "feichuan-worker"
PROTOCOL_VERSION = 1
WORKER_VERSION = "1.0"
MAX_MESSAGE_BYTES = 1024 * 1024

_FORBIDDEN_KEYS = frozenset(
    {
        "authorization",
        "browsercontext",
        "browsertoken",
        "cookie",
        "cookies",
        "mediaurl",
        "mediatoken",
        "qsignature",
        "secretid",
        "secretkey",
        "signature",
        "signedurl",
        "token",
    }
)
_SENSITIVE_VALUE = re.compile(
    r"(?i)(?:\bAKID[0-9A-Za-z]{8,}\b|"
    r"\b(?:authorization|cookie|q-signature|x-cos-security-token)\s*[:=]|"
    r"https?://\S+\?(?:\S*&)?(?:q-signature|x-cos-security-token)=)"
)
_SIGNED_URL = re.compile(
    r"(?i)https?://\S+\?(?:\S*&)?(?:q-signature|x-cos-security-token)=\S+"
)
_LABELED_SECRET = re.compile(
    r"(?i)\b(secretid|secretkey|authorization|cookie|q-signature|x-cos-security-token)"
    r"\s*[:=]\s*\S+"
)


class ProtocolError(ValueError):
    """A safe protocol failure whose message never includes the rejected payload."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Request:
    request_id: str
    message_type: str
    payload: Mapping[str, Any]


class OperationRegistry:
    """Keeps scan/browser objects in memory and exposes only opaque operation IDs."""

    def __init__(self) -> None:
        self._values: dict[str, Any] = {}
        self._lock = threading.RLock()

    def add(self, value: Any) -> str:
        operation_id = "op_" + secrets.token_urlsafe(18)
        with self._lock:
            self._values[operation_id] = value
        return operation_id

    def get(self, operation_id: str) -> Any:
        with self._lock:
            try:
                return self._values[operation_id]
            except KeyError as exc:
                raise ProtocolError("operation_not_found", "操作已过期，请重新扫描。") from exc

    def remove(self, operation_id: str) -> Any | None:
        with self._lock:
            return self._values.pop(operation_id, None)

    def clear(self) -> None:
        with self._lock:
            values = tuple(self._values.values())
            self._values.clear()
        for value in values:
            clear = getattr(value, "clear_sensitive", None)
            if callable(clear):
                try:
                    clear()
                except Exception:
                    pass


def _normalized_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def ensure_public_payload(value: Any, *, depth: int = 0) -> None:
    """Reject protocol values that could carry credentials or signed media state."""

    if depth > 32:
        raise ProtocolError("payload_too_deep", "协议消息嵌套层级过多。")
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _normalized_key(key) in _FORBIDDEN_KEYS:
                raise ProtocolError("sensitive_field", "协议消息包含禁止传输的敏感字段。")
            ensure_public_payload(child, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            ensure_public_payload(child, depth=depth + 1)
        return
    if isinstance(value, str) and _SENSITIVE_VALUE.search(value):
        raise ProtocolError("sensitive_value", "协议消息包含禁止传输的敏感内容。")


def sanitize_public_text(value: object, *, limit: int = 2048) -> str:
    """Remove signed URLs and labeled secrets before a status crosses stdout."""

    text = str(value or "")
    text = _SIGNED_URL.sub("[已隐藏签名地址]", text)
    text = _LABELED_SECRET.sub(r"\1=[已隐藏]", text)
    if _SENSITIVE_VALUE.search(text):
        return "状态信息包含敏感内容，已隐藏。"
    return text if len(text) <= limit else text[:limit] + "…"


def decode_request(raw_line: bytes | str) -> Request:
    if isinstance(raw_line, bytes):
        if len(raw_line) > MAX_MESSAGE_BYTES:
            raise ProtocolError("message_too_large", "协议消息超过大小限制。")
        try:
            text = raw_line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("invalid_encoding", "协议消息必须使用 UTF-8。") from exc
    else:
        text = raw_line
        if len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ProtocolError("message_too_large", "协议消息超过大小限制。")
    try:
        message = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ProtocolError("invalid_json", "无法解析协议消息。") from exc
    if not isinstance(message, dict):
        raise ProtocolError("invalid_message", "协议消息必须是 JSON 对象。")
    ensure_public_payload(message)
    if message.get("protocol") != PROTOCOL_NAME:
        raise ProtocolError("protocol_mismatch", "工作进程协议名称不匹配。")
    if message.get("version") != PROTOCOL_VERSION:
        raise ProtocolError("version_mismatch", "工作进程协议版本不兼容。")
    request_id = str(message.get("id") or "").strip()
    message_type = str(message.get("type") or "").strip()
    payload = message.get("payload", {})
    if not request_id or len(request_id) > 128:
        raise ProtocolError("invalid_id", "协议请求 ID 无效。")
    if not message_type or len(message_type) > 80:
        raise ProtocolError("invalid_type", "协议消息类型无效。")
    if not isinstance(payload, dict):
        raise ProtocolError("invalid_payload", "协议 payload 必须是 JSON 对象。")
    return Request(request_id, message_type, payload)


def encode_message(
    request_id: str,
    message_type: str,
    payload: Mapping[str, Any] | None = None,
) -> bytes:
    message = {
        "protocol": PROTOCOL_NAME,
        "version": PROTOCOL_VERSION,
        "id": str(request_id),
        "type": str(message_type),
        "payload": dict(payload or {}),
    }
    ensure_public_payload(message)
    try:
        encoded = json.dumps(
            message,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError("invalid_response", "工作进程生成了无效响应。") from exc
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message_too_large", "协议响应超过大小限制。")
    return encoded + b"\n"


def encode_error(request_id: str, error: ProtocolError | Exception) -> bytes:
    if isinstance(error, ProtocolError):
        code = error.code
        message = str(error)
    else:
        code = "worker_error"
        message = "工作进程执行失败，请重试；如果仍失败，请重新扫描。"
    return encode_message(
        request_id or "unknown",
        "error",
        {"code": code, "message": message, "retryable": code != "version_mismatch"},
    )
