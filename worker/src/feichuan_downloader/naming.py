"""按平台生成稳定、可排序且适用于 Windows 的输出文件名。"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path

from .config import get_download_dir
from .models import ContentKind, Platform, WorkItem


MAX_FILENAME_UNITS = 240
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_VALID_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,10}\Z")
_DATE_PATTERN = re.compile(r"(?<!\d)(\d{4})[-/.]?(\d{2})[-/.]?(\d{2})(?!\d)")
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def windows_name_units(value: str) -> int:
    """返回 NTFS 文件名限制使用的 UTF-16 code unit 数。"""

    return len(value.encode("utf-16-le")) // 2


def _truncate_units(value: str, maximum: int) -> str:
    if maximum <= 0:
        return ""
    used = 0
    output: list[str] = []
    for character in value:
        units = windows_name_units(character)
        if used + units > maximum:
            break
        output.append(character)
        used += units
    return "".join(output)


def sanitize_component(value: object, fallback: str) -> str:
    """清理单个文件名组成部分，同时处理 Windows 保留名。"""

    text = unicodedata.normalize("NFC", str(value or "")).strip()
    text = _INVALID_CHARS.sub("_", text)
    text = "".join(character for character in text if character.isprintable())
    text = text.rstrip(". ").strip()
    if not text:
        text = fallback
    if text.split(".", 1)[0].upper() in _RESERVED_NAMES:
        text = f"_{text}"
    return text


def publication_date(value: date | datetime | str | None) -> str:
    """将发布时间规范为可排序的 ``YYYYMMDD``。"""

    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    match = _DATE_PATTERN.search(str(value or ""))
    if not match:
        return "未知日期"
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day).strftime("%Y%m%d")
    except ValueError:
        return "未知日期"


def _extension(value: str, fallback: str) -> str:
    extension = str(value or "").strip()
    if extension and not extension.startswith("."):
        extension = "." + extension
    if not _VALID_EXTENSION.fullmatch(extension):
        extension = fallback
    return extension.lower()


def _shorten_with_hash(value: str, maximum: int) -> str:
    if windows_name_units(value) <= maximum:
        return value
    if maximum <= 0:
        return ""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    suffix = f"~{digest}"
    suffix_units = windows_name_units(suffix)
    if maximum <= suffix_units + 1:
        return _truncate_units(value, maximum)
    prefix = _truncate_units(value, maximum - suffix_units).rstrip(". ")
    return (prefix + suffix) if prefix else _truncate_units(suffix, maximum)


def _compose(
    parts: list[str],
    extension: str,
    *,
    shrink_order: tuple[int, ...],
    maximum: int = MAX_FILENAME_UNITS,
) -> str:
    """优先缩短标题和作者，保留平台、日期、作品 ID 及变体标识。"""

    def build() -> str:
        return "_".join(parts) + extension

    filename = build()
    if windows_name_units(filename) <= maximum:
        return filename

    for index in shrink_order:
        excess = windows_name_units(filename) - maximum
        if excess <= 0:
            break
        current_units = windows_name_units(parts[index])
        minimum = 1
        target = max(minimum, current_units - excess)
        parts[index] = _shorten_with_hash(parts[index], target)
        filename = build()

    if windows_name_units(filename) > maximum:
        # 极端的超长作品 ID 仍需生成有效名称；保留其前缀和稳定短哈希。
        for index in range(len(parts) - 1, -1, -1):
            if index in shrink_order or index in {0, 2}:
                continue
            excess = windows_name_units(filename) - maximum
            if excess <= 0:
                break
            current_units = windows_name_units(parts[index])
            parts[index] = _shorten_with_hash(parts[index], max(1, current_units - excess))
            filename = build()

    if windows_name_units(filename) > maximum:
        raise ValueError("平台、日期和作品 ID 过长，无法生成有效的 Windows 文件名。")
    return filename


def _require_platform(item: WorkItem, expected: Platform) -> None:
    if item.platform is not expected:
        raise ValueError(f"作品平台必须是 {expected.value}。")


def douyin_video_filename(item: WorkItem, extension: str = ".mp4") -> str:
    """生成 ``抖音_作者_日期_作品ID_标题.mp4``。"""

    _require_platform(item, Platform.DOUYIN)
    parts = [
        "抖音",
        sanitize_component(item.author, "未知作者"),
        publication_date(item.published_at),
        sanitize_component(item.work_id, "未知作品"),
        sanitize_component(item.title, "无标题"),
    ]
    return _compose(parts, _extension(extension, ".mp4"), shrink_order=(4, 1, 3))


def douyin_image_filename(
    item: WorkItem,
    sequence: int,
    extension: str = ".webp",
) -> str:
    """生成 ``抖音图文_作者_日期_作品ID_序号.webp``。"""

    _require_platform(item, Platform.DOUYIN)
    sequence = int(sequence)
    if sequence < 1:
        raise ValueError("图文序号必须从 1 开始。")
    parts = [
        "抖音图文",
        sanitize_component(item.author, "未知作者"),
        publication_date(item.published_at),
        sanitize_component(item.work_id, "未知作品"),
        f"{sequence:03d}",
    ]
    return _compose(parts, _extension(extension, ".webp"), shrink_order=(1, 3))


def filename_for(
    item: WorkItem,
    *,
    sequence: int | None = None,
    extension: str | None = None,
) -> str:
    """按作品平台和类型选择规范命名函数。"""

    if item.platform is Platform.DOUYIN:
        if item.content_type is ContentKind.IMAGE:
            if sequence is None:
                raise ValueError("抖音图文命名需要 sequence。")
            return douyin_image_filename(item, sequence, extension or ".webp")
        return douyin_video_filename(item, extension or ".mp4")
    raise ValueError(f"暂不支持平台 {item.platform.value} 的规范文件名。")


def output_path(filename: str, directory: str | Path | None = None) -> Path:
    """将文件名放到固定下载根目录；测试可显式传入其它目录。"""

    name = Path(str(filename)).name
    if not name or name in {".", ".."}:
        raise ValueError("filename 不能为空。")
    return Path(directory) / name if directory is not None else get_download_dir() / name
