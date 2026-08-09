"""跨界面、扫描器和下载后端共享的领域模型。

本模块只保存稳定元数据。带签名的媒体地址、Cookie、令牌和解密密钥只能放在
``MediaDescriptor`` 的内存字段中；该类型刻意禁止 pickle，并且字符串表示不会
暴露这些字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class SourceKind(str, Enum):
    """用户可以发起的任务来源。"""

    SINGLE_LINK = "single_link"
    DOUYIN_PROFILE = "douyin_profile"
    DOUYIN_COLLECTION = "douyin_collection"
    YOUTUBE_PLAYLIST = "youtube_playlist"
    YOUTUBE_CHANNEL = "youtube_channel"


class DownloadMode(str, Enum):
    """完整枚举后可选择的三种下载模式。"""

    INCREMENTAL = "incremental"
    REDOWNLOAD_ALL = "redownload_all"
    RETRY_FAILED = "retry_failed"


class IncompleteScanAction(str, Enum):
    """枚举不完整时唯一允许继续执行的确认动作。"""

    DISCOVERED_ONLY = "discovered_only"


class Platform(str, Enum):
    DOUYIN = "douyin"
    YOUTUBE = "youtube"
    GENERIC = "generic"


class ContentKind(str, Enum):
    VIDEO = "video"
    IMAGE = "image"
    LIVE = "live"
    UNKNOWN = "unknown"


class DownloadStage(str, Enum):
    IDLE = "idle"
    SCANNING = "scanning"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    DOWNLOADING = "downloading"
    VALIDATING = "validating"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ValidationStatus(str, Enum):
    PENDING = "pending"
    VALID = "valid"
    FAILED = "failed"
    INVALID = "invalid"
    MISSING = "missing"
    SKIPPED = "skipped"


def _enum_value(enum_type: type[Enum], value: Enum | str, field_name: str) -> Enum:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(str(item.value) for item in enum_type)
        raise ValueError(f"{field_name} 必须是以下值之一：{allowed}") from exc


@dataclass(frozen=True, slots=True)
class WorkItem:
    """一个具有稳定作品 ID 的待处理作品。"""

    platform: Platform
    work_id: str
    content_type: ContentKind
    title: str
    author: str
    published_at: date | datetime | str | None
    canonical_url: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "platform",
            _enum_value(Platform, self.platform, "platform"),
        )
        object.__setattr__(
            self,
            "content_type",
            _enum_value(ContentKind, self.content_type, "content_type"),
        )
        work_id = str(self.work_id or "").strip()
        if not work_id:
            raise ValueError("work_id 不能为空。")
        object.__setattr__(self, "work_id", work_id)
        object.__setattr__(self, "title", str(self.title or "").strip())
        object.__setattr__(self, "author", str(self.author or "").strip())
        object.__setattr__(self, "canonical_url", str(self.canonical_url or "").strip())

    @property
    def stable_id(self) -> str:
        """``work_id`` 的语义化别名，便于协议层表达稳定作品 ID。"""

        return self.work_id


@dataclass(frozen=True, slots=True)
class ScanResult:
    """一次主页、合集或捕获扫描的完整结果。"""

    source: SourceKind
    author: str
    reported_count: int | None
    unique_count: int
    content_counts: Mapping[ContentKind | str, int] = field(default_factory=dict)
    enumeration_complete: bool = False
    items: tuple[WorkItem, ...] = ()
    incomplete_reason: str = ""
    source_title: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _enum_value(SourceKind, self.source, "source"))
        object.__setattr__(self, "author", str(self.author or "").strip())
        if self.reported_count is not None and self.reported_count < 0:
            raise ValueError("reported_count 不能为负数。")
        if self.unique_count < 0:
            raise ValueError("unique_count 不能为负数。")
        normalized_counts: dict[str, int] = {}
        for raw_key, raw_count in self.content_counts.items():
            key = raw_key.value if isinstance(raw_key, ContentKind) else str(raw_key)
            count = int(raw_count)
            if count < 0:
                raise ValueError("content_counts 不能包含负数。")
            normalized_counts[key] = count
        object.__setattr__(self, "content_counts", MappingProxyType(normalized_counts))
        items = tuple(self.items)
        if items and len(items) != self.unique_count:
            raise ValueError("unique_count 必须与 items 中的唯一作品数一致。")
        object.__setattr__(self, "items", items)
        object.__setattr__(
            self,
            "incomplete_reason",
            str(self.incomplete_reason or "").strip(),
        )
        object.__setattr__(self, "source_title", str(self.source_title or "").strip())

    @property
    def allowed_download_modes(self) -> tuple[DownloadMode, ...]:
        """只有完整枚举才显示三种常规下载模式。"""

        if not self.enumeration_complete:
            return ()
        return tuple(DownloadMode)

    @property
    def incomplete_action(self) -> IncompleteScanAction | None:
        if self.enumeration_complete:
            return None
        return IncompleteScanAction.DISCOVERED_ONLY

    @property
    def confirmation_choices(
        self,
    ) -> tuple[DownloadMode | IncompleteScanAction, ...]:
        """直接供确认窗口使用，避免不完整枚举显示“已获取全部作品”。"""

        if self.enumeration_complete:
            return tuple(DownloadMode)
        return (IncompleteScanAction.DISCOVERED_ONLY,)


@dataclass(frozen=True, slots=True)
class DownloadEvent:
    """下载协调器向 GUI 发送的结构化进度快照。"""

    stage: DownloadStage
    scanned_count: int = 0
    current: int = 0
    total: int = 0
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    current_file: str = ""
    overall_percent: float | None = None
    message: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage", _enum_value(DownloadStage, self.stage, "stage"))
        for field_name in (
            "scanned_count",
            "current",
            "total",
            "succeeded",
            "skipped",
            "failed",
        ):
            value = int(getattr(self, field_name))
            if value < 0:
                raise ValueError(f"{field_name} 不能为负数。")
            object.__setattr__(self, field_name, value)
        if self.total and self.current > self.total:
            raise ValueError("current 不能大于 total。")
        if self.overall_percent is not None:
            percent = float(self.overall_percent)
            if not 0.0 <= percent <= 100.0:
                raise ValueError("overall_percent 必须位于 0 到 100 之间。")
            object.__setattr__(self, "overall_percent", percent)
        object.__setattr__(self, "current_file", str(self.current_file or ""))
        object.__setattr__(self, "message", str(self.message or ""))


class MediaDescriptor:
    """只驻留内存的临时媒体信息。

    ``media_url``、请求头、Cookie 和 token 都属于敏感字段。
    对象没有 ``__dict__``，不可 pickle；``repr``/``str`` 和
    :meth:`to_public_dict` 只暴露清晰度、编码等非敏感元数据。
    """

    __slots__ = (
        "quality",
        "width",
        "height",
        "codec",
        "container",
        "_media_url",
        "_headers",
        "_cookie",
        "_token",
        "_cleared",
    )

    def __init__(
        self,
        *,
        quality: str,
        media_url: str,
        width: int | None = None,
        height: int | None = None,
        codec: str = "",
        container: str = "",
        headers: Mapping[str, str] | None = None,
        cookie: str = "",
        token: str = "",
    ) -> None:
        quality = str(quality or "").strip()
        media_url = str(media_url or "").strip()
        if not quality:
            raise ValueError("quality 不能为空。")
        if not media_url:
            raise ValueError("media_url 不能为空。")
        if width is not None and int(width) < 0:
            raise ValueError("width 不能为负数。")
        if height is not None and int(height) < 0:
            raise ValueError("height 不能为负数。")
        self.quality = quality
        self.width = int(width) if width is not None else None
        self.height = int(height) if height is not None else None
        self.codec = str(codec or "").strip()
        self.container = str(container or "").strip()
        self._media_url = media_url
        self._headers = {str(key): str(value) for key, value in (headers or {}).items()}
        self._cookie = str(cookie or "")
        self._token = str(token or "")
        self._cleared = False

    @property
    def media_url(self) -> str:
        return self._media_url

    @property
    def headers(self) -> dict[str, str]:
        return dict(self._headers)

    @property
    def cookie(self) -> str:
        return self._cookie

    @property
    def token(self) -> str:
        return self._token

    @property
    def cleared(self) -> bool:
        return self._cleared

    def to_public_dict(self) -> dict[str, Any]:
        """返回可安全用于状态展示的非敏感字段。"""

        return {
            "quality": self.quality,
            "width": self.width,
            "height": self.height,
            "codec": self.codec,
            "container": self.container,
        }

    def clear_sensitive(self) -> None:
        """尽快释放敏感引用。"""

        self._headers.clear()
        self._media_url = ""
        self._cookie = ""
        self._token = ""
        self._cleared = True

    def __enter__(self) -> MediaDescriptor:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.clear_sensitive()

    def __repr__(self) -> str:
        return (
            "MediaDescriptor("
            f"quality={self.quality!r}, width={self.width!r}, height={self.height!r}, "
            f"codec={self.codec!r}, container={self.container!r}, "
            "sensitive=<in-memory-redacted>)"
        )

    __str__ = __repr__

    def __getstate__(self) -> object:
        raise TypeError("MediaDescriptor 含敏感临时字段，禁止序列化。")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("MediaDescriptor 含敏感临时字段，禁止序列化。")
