"""下载状态数据库。

作品元数据与其一个或多个输出文件分表保存。数据库没有媒体 URL、请求头、Cookie
或 token 字段，调用接口也不接受 ``MediaDescriptor``，从结构上避免把临时
敏感信息写入磁盘。
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

from .config import safe_url_for_log
from .models import (
    ContentKind,
    DownloadMode,
    Platform,
    SourceKind,
    ValidationStatus,
    WorkItem,
)


_SCHEMA_VERSION = 1
_URL_RE = re.compile(r"https?://[^\s\]>)'\"]+", re.IGNORECASE)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(cookie|authorization|proxy-authorization|token|decode[_-]?key)"
    r"(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_MAX_FAILURE_REASON = 4000


def default_state_path() -> Path:
    """返回当前用户的默认状态数据库路径。"""

    configured = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(configured) if configured else Path.home() / "AppData" / "Local"
    return base / "FeichuanDownloader" / "state.sqlite3"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _published_at_text(value: date | datetime | str | None) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "").strip()[:100]


def _safe_stable_url(value: str) -> str:
    """数据库只保存规范链接的 scheme/host/path，不保存任何 query。"""

    return safe_url_for_log(str(value or "").strip())


def _safe_failure_reason(value: str) -> str:
    """移除错误文本中的签名 query 和常见敏感字段赋值。"""

    text = str(value or "")
    text = _URL_RE.sub(lambda match: safe_url_for_log(match.group(0)), text)
    text = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>",
        text,
    )
    return text.strip()[:_MAX_FAILURE_REASON]


@dataclass(frozen=True, slots=True)
class StoredWork:
    database_id: int
    source_kind: SourceKind
    item: WorkItem
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    database_id: int
    work_database_id: int
    artifact_key: str
    file_path: Path
    validation_status: ValidationStatus
    failure_reason: str
    file_size: int | None
    created_at: str
    updated_at: str


class StateStore:
    """线程安全的 SQLite 状态存储。"""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else default_state_path()
        self.path = self.path.expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            self.path,
            timeout=10,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._initialize_schema()

    def _initialize_schema(self) -> None:
        statuses = ", ".join(f"'{status.value}'" for status in ValidationStatus)
        with self._connection:
            self._connection.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS works (
                    id INTEGER PRIMARY KEY,
                    platform TEXT NOT NULL,
                    work_id TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    author TEXT NOT NULL DEFAULT '',
                    published_at TEXT NOT NULL DEFAULT '',
                    canonical_url TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(platform, work_id)
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    id INTEGER PRIMARY KEY,
                    work_database_id INTEGER NOT NULL
                        REFERENCES works(id) ON DELETE CASCADE,
                    artifact_key TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    validation_status TEXT NOT NULL
                        CHECK(validation_status IN ({statuses})),
                    failure_reason TEXT NOT NULL DEFAULT '',
                    file_size INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(work_database_id, artifact_key)
                );

                CREATE INDEX IF NOT EXISTS idx_works_source
                    ON works(source_kind, platform);
                CREATE INDEX IF NOT EXISTS idx_artifacts_status
                    ON artifacts(validation_status);
                """
            )
            self._connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._connection.commit()
                self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                self._connection.close()
                self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("StateStore 已关闭。")

    @staticmethod
    def _platform(value: Platform | str) -> Platform:
        try:
            return Platform(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"未知平台：{value}") from exc

    @staticmethod
    def _source_kind(value: SourceKind | str) -> SourceKind:
        try:
            return SourceKind(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"未知来源：{value}") from exc

    @staticmethod
    def _status(value: ValidationStatus | str) -> ValidationStatus:
        try:
            return ValidationStatus(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"未知校验状态：{value}") from exc

    def upsert_work(self, item: WorkItem, source_kind: SourceKind | str) -> int:
        """新增或刷新作品元数据，返回内部数据库 ID。"""

        if not isinstance(item, WorkItem):
            raise TypeError("item 必须是 WorkItem。")
        source = self._source_kind(source_kind)
        now = _utc_now()
        with self._lock:
            self._ensure_open()
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO works (
                        platform, work_id, source_kind, content_type, title, author,
                        published_at, canonical_url, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(platform, work_id) DO UPDATE SET
                        source_kind = excluded.source_kind,
                        content_type = excluded.content_type,
                        title = excluded.title,
                        author = excluded.author,
                        published_at = excluded.published_at,
                        canonical_url = excluded.canonical_url,
                        updated_at = excluded.updated_at
                    """,
                    (
                        item.platform.value,
                        item.work_id,
                        source.value,
                        item.content_type.value,
                        item.title,
                        item.author,
                        _published_at_text(item.published_at),
                        _safe_stable_url(item.canonical_url),
                        now,
                        now,
                    ),
                )
                row = self._connection.execute(
                    "SELECT id FROM works WHERE platform = ? AND work_id = ?",
                    (item.platform.value, item.work_id),
                ).fetchone()
        if row is None:  # defensive: INSERT/SELECT above are in one transaction
            raise RuntimeError("作品状态写入失败。")
        return int(row["id"])

    def get_work(self, platform: Platform | str, work_id: str) -> StoredWork | None:
        platform_value = self._platform(platform)
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM works WHERE platform = ? AND work_id = ?",
                (platform_value.value, str(work_id)),
            ).fetchone()
        return self._stored_work(row) if row is not None else None

    def list_works(
        self,
        *,
        source_kind: SourceKind | str | None = None,
        platform: Platform | str | None = None,
    ) -> tuple[StoredWork, ...]:
        clauses: list[str] = []
        values: list[str] = []
        if source_kind is not None:
            clauses.append("source_kind = ?")
            values.append(self._source_kind(source_kind).value)
        if platform is not None:
            clauses.append("platform = ?")
            values.append(self._platform(platform).value)
        sql = "SELECT * FROM works"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(sql, values).fetchall()
        return tuple(self._stored_work(row) for row in rows)

    @staticmethod
    def _stored_work(row: sqlite3.Row) -> StoredWork:
        published_at = str(row["published_at"] or "") or None
        item = WorkItem(
            platform=Platform(row["platform"]),
            work_id=str(row["work_id"]),
            content_type=ContentKind(row["content_type"]),
            title=str(row["title"]),
            author=str(row["author"]),
            published_at=published_at,
            canonical_url=str(row["canonical_url"]),
        )
        return StoredWork(
            database_id=int(row["id"]),
            source_kind=SourceKind(row["source_kind"]),
            item=item,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def record_artifact(
        self,
        platform: Platform | str,
        work_id: str,
        artifact_key: str,
        file_path: str | os.PathLike[str],
        validation_status: ValidationStatus | str,
        *,
        failure_reason: str = "",
        file_size: int | None = None,
    ) -> int:
        """新增或更新作品的一个输出文件。"""

        platform_value = self._platform(platform)
        status = self._status(validation_status)
        artifact_key = str(artifact_key or "").strip()
        if not artifact_key:
            raise ValueError("artifact_key 不能为空。")
        path_text = os.fspath(file_path).strip()
        if not path_text:
            raise ValueError("file_path 不能为空。")
        if path_text.lower().startswith(("http://", "https://")):
            raise ValueError("file_path 必须是本地路径，不能是媒体 URL。")
        if file_size is not None and int(file_size) < 0:
            raise ValueError("file_size 不能为负数。")
        safe_reason = _safe_failure_reason(failure_reason)
        now = _utc_now()

        with self._lock:
            self._ensure_open()
            work_row = self._connection.execute(
                "SELECT id FROM works WHERE platform = ? AND work_id = ?",
                (platform_value.value, str(work_id)),
            ).fetchone()
            if work_row is None:
                raise KeyError(f"作品不存在：{platform_value.value}/{work_id}")
            work_database_id = int(work_row["id"])
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO artifacts (
                        work_database_id, artifact_key, file_path, validation_status,
                        failure_reason, file_size, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(work_database_id, artifact_key) DO UPDATE SET
                        file_path = excluded.file_path,
                        validation_status = excluded.validation_status,
                        failure_reason = excluded.failure_reason,
                        file_size = excluded.file_size,
                        updated_at = excluded.updated_at
                    """,
                    (
                        work_database_id,
                        artifact_key,
                        path_text,
                        status.value,
                        safe_reason,
                        int(file_size) if file_size is not None else None,
                        now,
                        now,
                    ),
                )
                row = self._connection.execute(
                    """
                    SELECT id FROM artifacts
                    WHERE work_database_id = ? AND artifact_key = ?
                    """,
                    (work_database_id, artifact_key),
                ).fetchone()
        if row is None:
            raise RuntimeError("文件状态写入失败。")
        return int(row["id"])

    def record_artifact_for(
        self,
        item: WorkItem,
        source_kind: SourceKind | str,
        artifact_key: str,
        file_path: str | os.PathLike[str],
        validation_status: ValidationStatus | str,
        *,
        failure_reason: str = "",
        file_size: int | None = None,
    ) -> int:
        """在一次事务式调用流程中确保作品存在并记录输出文件。"""

        self.upsert_work(item, source_kind)
        return self.record_artifact(
            item.platform,
            item.work_id,
            artifact_key,
            file_path,
            validation_status,
            failure_reason=failure_reason,
            file_size=file_size,
        )

    def get_artifact(
        self,
        platform: Platform | str,
        work_id: str,
        artifact_key: str,
    ) -> StoredArtifact | None:
        platform_value = self._platform(platform)
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                """
                SELECT artifacts.*
                FROM artifacts
                JOIN works ON works.id = artifacts.work_database_id
                WHERE works.platform = ? AND works.work_id = ?
                    AND artifacts.artifact_key = ?
                """,
                (platform_value.value, str(work_id), str(artifact_key)),
            ).fetchone()
        return self._stored_artifact(row) if row is not None else None

    def list_artifacts(
        self,
        platform: Platform | str,
        work_id: str,
    ) -> tuple[StoredArtifact, ...]:
        platform_value = self._platform(platform)
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT artifacts.*
                FROM artifacts
                JOIN works ON works.id = artifacts.work_database_id
                WHERE works.platform = ? AND works.work_id = ?
                ORDER BY artifacts.artifact_key
                """,
                (platform_value.value, str(work_id)),
            ).fetchall()
        return tuple(self._stored_artifact(row) for row in rows)

    @staticmethod
    def _stored_artifact(row: sqlite3.Row) -> StoredArtifact:
        return StoredArtifact(
            database_id=int(row["id"]),
            work_database_id=int(row["work_database_id"]),
            artifact_key=str(row["artifact_key"]),
            file_path=Path(str(row["file_path"])),
            validation_status=ValidationStatus(row["validation_status"]),
            failure_reason=str(row["failure_reason"]),
            file_size=int(row["file_size"]) if row["file_size"] is not None else None,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def should_download(
        self,
        platform: Platform | str,
        work_id: str,
        artifact_key: str,
        mode: DownloadMode | str,
        expected_path: str | os.PathLike[str] | None = None,
    ) -> bool:
        """根据三种重复运行模式判断一个预期文件是否应进入队列。"""

        try:
            selected_mode = DownloadMode(mode)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"未知下载模式：{mode}") from exc
        if selected_mode is DownloadMode.REDOWNLOAD_ALL:
            return True
        artifact = self.get_artifact(platform, work_id, artifact_key)
        if artifact is None:
            return selected_mode is DownloadMode.INCREMENTAL
        if expected_path is not None:
            stored = os.path.normcase(os.path.abspath(os.fspath(artifact.file_path)))
            expected = os.path.normcase(os.path.abspath(os.fspath(expected_path)))
            if stored != expected:
                return selected_mode is DownloadMode.INCREMENTAL
        if selected_mode is DownloadMode.RETRY_FAILED:
            return artifact.validation_status in {
                ValidationStatus.FAILED,
                ValidationStatus.INVALID,
            }
        if artifact.validation_status is ValidationStatus.SKIPPED:
            # The previous scan may have lacked media descriptors.  The caller
            # filters still-unsupported items before reaching this method, so an
            # incremental run should retry when the item becomes supported.
            return selected_mode is DownloadMode.INCREMENTAL
        if artifact.validation_status is ValidationStatus.VALID:
            return not artifact.file_path.is_file()
        return True

    def mark_missing_files(
        self,
        *,
        platform: Platform | str | None = None,
    ) -> int:
        """把数据库中标记有效但磁盘已缺失的文件更新为 ``missing``。"""

        values: list[str] = [ValidationStatus.VALID.value]
        sql = """
            SELECT artifacts.id, artifacts.file_path
            FROM artifacts
            JOIN works ON works.id = artifacts.work_database_id
            WHERE artifacts.validation_status = ?
        """
        if platform is not None:
            sql += " AND works.platform = ?"
            values.append(self._platform(platform).value)
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(sql, values).fetchall()
            missing_ids = [
                int(row["id"])
                for row in rows
                if not Path(str(row["file_path"])).is_file()
            ]
            if not missing_ids:
                return 0
            now = _utc_now()
            with self._connection:
                self._connection.executemany(
                    """
                    UPDATE artifacts
                    SET validation_status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        (ValidationStatus.MISSING.value, now, database_id)
                        for database_id in missing_ids
                    ),
                )
        return len(missing_ids)

    def delete_works_not_in(
        self,
        platform: Platform | str,
        work_ids: Iterable[str],
    ) -> int:
        """测试/维护辅助：删除某平台不在给定稳定 ID 集合中的作品及其文件记录。"""

        platform_value = self._platform(platform)
        keep = {str(value) for value in work_ids}
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT id, work_id FROM works WHERE platform = ?",
                (platform_value.value,),
            ).fetchall()
            delete_ids = [int(row["id"]) for row in rows if str(row["work_id"]) not in keep]
            if not delete_ids:
                return 0
            with self._connection:
                self._connection.executemany(
                    "DELETE FROM works WHERE id = ?",
                    ((database_id,) for database_id in delete_ids),
                )
        return len(delete_ids)
