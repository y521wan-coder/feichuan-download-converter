"""User-facing quality and format choices shared by GUI and download backends."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping


class QualityPreference(str, Enum):
    BEST = "best"
    VIDEO_MP4 = "video_mp4"
    VIDEO_WEBM = "video_webm"
    AUDIO_M4A = "audio_m4a"
    AUDIO_MP3 = "audio_mp3"
    HEIGHT_1080 = "height_1080"
    HEIGHT_720 = "height_720"


QUALITY_LABELS: dict[QualityPreference, str] = {
    QualityPreference.BEST: "最高品质",
    QualityPreference.VIDEO_MP4: "MP4 视频",
    QualityPreference.VIDEO_WEBM: "WebM 视频",
    QualityPreference.AUDIO_M4A: "M4A 音频",
    QualityPreference.AUDIO_MP3: "MP3 音频",
    QualityPreference.HEIGHT_1080: "1080P 或以下最高",
    QualityPreference.HEIGHT_720: "720P 或以下最高",
}

NO_ALTERNATIVE_MESSAGE = (
    "本次下载的资源只有默认的这一种画质或者音频格式或者质量，不提供其他选择。"
)


@dataclass(frozen=True, slots=True)
class QualityChoice:
    preference: QualityPreference
    label: str


def coerce_quality_preference(value: QualityPreference | str | None) -> QualityPreference:
    if isinstance(value, QualityPreference):
        return value
    if value is None or str(value or "").strip() == "":
        return QualityPreference.BEST
    try:
        return QualityPreference(str(value).strip())
    except ValueError:
        return QualityPreference.BEST


def preference_label(value: QualityPreference | str | None) -> str:
    preference = coerce_quality_preference(value)
    return QUALITY_LABELS.get(preference, QUALITY_LABELS[QualityPreference.BEST])


def ytdlp_format_selector(value: QualityPreference | str | None, *, audio_only: bool = False) -> str:
    """Return a yt-dlp format selector without forcing conversion."""

    preference = coerce_quality_preference(value)
    if audio_only and preference is QualityPreference.BEST:
        return "bestaudio/best"
    if preference is QualityPreference.VIDEO_MP4:
        return "bv*[ext=mp4]+ba/b[ext=mp4]/best[ext=mp4]/bv*+ba/b"
    if preference is QualityPreference.VIDEO_WEBM:
        return "bv*[ext=webm]+ba[ext=webm]/b[ext=webm]/best[ext=webm]/bv*+ba/b"
    if preference is QualityPreference.AUDIO_M4A:
        return "ba[ext=m4a]/best[ext=m4a]/ba/best"
    if preference is QualityPreference.AUDIO_MP3:
        return "ba[ext=mp3]/best[ext=mp3]/ba/best"
    if preference is QualityPreference.HEIGHT_1080:
        return "bv*[height<=1080]+ba/b[height<=1080]/best[height<=1080]/bv*+ba/b"
    if preference is QualityPreference.HEIGHT_720:
        return "bv*[height<=720]+ba/b[height<=720]/best[height<=720]/bv*+ba/b"
    return "bestaudio/best" if audio_only else "bv*+ba/b"


def _clean_ext(value: Any) -> str:
    return str(value or "").strip().lower().lstrip(".")


def _has_video(format_info: Mapping[str, Any]) -> bool:
    vcodec = str(format_info.get("vcodec") or "").strip().lower()
    return bool(vcodec and vcodec != "none")


def _has_audio(format_info: Mapping[str, Any]) -> bool:
    acodec = str(format_info.get("acodec") or "").strip().lower()
    return bool(acodec and acodec != "none")


def _height(format_info: Mapping[str, Any]) -> int:
    value = format_info.get("height")
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def choices_from_ytdlp_formats(formats: Iterable[Mapping[str, Any]]) -> tuple[QualityChoice, ...]:
    entries = [item for item in formats if isinstance(item, Mapping)]
    if not entries:
        return (QualityChoice(QualityPreference.BEST, QUALITY_LABELS[QualityPreference.BEST]),)
    has_video = any(_has_video(item) for item in entries)
    max_height = max((_height(item) for item in entries if _has_video(item)), default=0)
    video_exts = {_clean_ext(item.get("ext")) for item in entries if _has_video(item)}
    audio_exts = {_clean_ext(item.get("ext")) for item in entries if _has_audio(item)}

    preferences: list[QualityPreference] = [QualityPreference.BEST]
    if has_video and "mp4" in video_exts:
        preferences.append(QualityPreference.VIDEO_MP4)
    if has_video and "webm" in video_exts:
        preferences.append(QualityPreference.VIDEO_WEBM)
    if "m4a" in audio_exts:
        preferences.append(QualityPreference.AUDIO_M4A)
    if "mp3" in audio_exts:
        preferences.append(QualityPreference.AUDIO_MP3)
    if max_height >= 1080:
        preferences.append(QualityPreference.HEIGHT_1080)
    if max_height >= 720:
        preferences.append(QualityPreference.HEIGHT_720)

    deduped = tuple(dict.fromkeys(preferences))
    return tuple(QualityChoice(item, QUALITY_LABELS[item]) for item in deduped)


def choices_from_media_descriptors(descriptors: Iterable[Any]) -> tuple[QualityChoice, ...]:
    entries = tuple(descriptors)
    if not entries:
        return (QualityChoice(QualityPreference.BEST, QUALITY_LABELS[QualityPreference.BEST]),)
    max_height = max(
        (int(getattr(item, "height", 0) or 0) for item in entries),
        default=0,
    )
    containers = {
        _clean_ext(getattr(item, "container", ""))
        for item in entries
        if str(getattr(item, "container", "") or "").strip()
    }
    preferences: list[QualityPreference] = [QualityPreference.BEST]
    if "mp4" in containers:
        preferences.append(QualityPreference.VIDEO_MP4)
    if "webm" in containers:
        preferences.append(QualityPreference.VIDEO_WEBM)
    if max_height >= 1080:
        preferences.append(QualityPreference.HEIGHT_1080)
    if max_height >= 720:
        preferences.append(QualityPreference.HEIGHT_720)
    deduped = tuple(dict.fromkeys(preferences))
    return tuple(QualityChoice(item, QUALITY_LABELS[item]) for item in deduped)
