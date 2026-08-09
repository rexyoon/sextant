from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path

from .base import (
    Activity,
    ActivityType,
    Document,
    DocumentStatus,
    DocumentType,
    Store,
    utc_now_iso,
)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS activity (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project      TEXT NOT NULL,
    type         TEXT NOT NULL CHECK (type IN ('COMMIT', 'NOTE', 'ERROR')),
    ref          TEXT,
    raw_text     TEXT NOT NULL,
    occurred_at  TEXT NOT NULL,
    occurred_utc TEXT NOT NULL,
    local_date   TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

-- ref 가 있는 것만 유일. NOTE 는 ref 가 NULL 이라 몇 건이든 쌓인다.
CREATE UNIQUE INDEX IF NOT EXISTS activity_ref_unique
    ON activity (project, type, ref) WHERE ref IS NOT NULL;

CREATE INDEX IF NOT EXISTS activity_project_date
    ON activity (project, local_date);

CREATE TABLE IF NOT EXISTS document (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project      TEXT NOT NULL,
    type         TEXT NOT NULL CHECK (type IN ('DEVLOG', 'TROUBLESHOOT', 'RESUME')),
    title        TEXT NOT NULL,
    body_md      TEXT NOT NULL,
    period_start TEXT,
    period_end   TEXT,
    status       TEXT NOT NULL DEFAULT 'DRAFT'
                 CHECK (status IN ('DRAFT', 'APPROVED')),
    source_path  TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- 하루치 일지는 (project, DEVLOG, 그 날) 하나뿐이어야 한다.
-- 기간이 NULL 인 트러블슈팅은 SQLite 가 NULL 을 서로 다르게 보므로 여러 건 가능.
CREATE UNIQUE INDEX IF NOT EXISTS document_period_unique
    ON document (project, type, period_start, period_end);

CREATE TABLE IF NOT EXISTS document_source (
    document_id INTEGER NOT NULL REFERENCES document (id) ON DELETE CASCADE,
    activity_id INTEGER NOT NULL REFERENCES activity (id) ON DELETE CASCADE,
    PRIMARY KEY (document_id, activity_id)
);
"""


class SQLiteStore(Store):
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        if self._path.parent and str(self._path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        # 외래키는 SQLite 에서 기본이 꺼져 있다. 켜지 않으면 document_source 의
        # ON DELETE CASCADE 가 조용히 무시되고 고아 행이 남는다.
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def _migrate(self) -> None:
        """`PRAGMA user_version` 으로 버전을 관리한다.

        별도 마이그레이션 테이블을 두지 않는 이유: Phase 1 은 스키마가 하나고,
        Phase 2 에서 Postgres 로 갈 때 여기 이력은 어차피 안 따라간다.
        """
        current = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"DB 스키마 버전 {current} 은 이 코드(v{SCHEMA_VERSION})보다 최신입니다. "
                "sextant 를 업데이트하세요."
            )
        if current < SCHEMA_VERSION:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def add_activity(self, activity: Activity) -> Activity | None:
        saved = self.add_activities([activity])
        return saved[0] if saved else None

    def add_activities(self, activities: Iterable[Activity]) -> list[Activity]:
        """한 트랜잭션으로 저장한다.

        중복 검사를 INSERT OR IGNORE 로 하지 않는 이유:
        그건 CHECK 위반 같은 **진짜 버그까지 조용히 삼킨다.** 여기서는
        이미 있는 ref 를 미리 읽어 걸러내고, UNIQUE 인덱스는 마지막 방어선으로만
        남긴다. 그래서 예상 못 한 IntegrityError 는 그대로 터지게 둔다.
        """
        pending = list(activities)
        if not pending:
            return []

        seen: dict[tuple[str, str], set[str]] = {}
        saved: list[Activity] = []
        now = utc_now_iso()

        with self._conn:  # 예외가 나면 통째로 롤백된다
            for item in pending:
                if item.ref is not None:
                    key = (item.project, str(item.type))
                    if key not in seen:
                        seen[key] = self.known_refs(item.project, ActivityType(item.type))
                    if item.ref in seen[key]:
                        continue  # 이미 수집한 커밋
                    seen[key].add(item.ref)

                cursor = self._conn.execute(
                    """
                    INSERT INTO activity
                        (project, type, ref, raw_text, occurred_at, occurred_utc,
                         local_date, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.project,
                        str(item.type),
                        item.ref,
                        item.raw_text,
                        item.occurred_at,
                        item.occurred_utc,
                        item.local_date,
                        now,
                    ),
                )
                stored = Activity(
                    project=item.project,
                    type=ActivityType(item.type),
                    raw_text=item.raw_text,
                    occurred_at=item.occurred_at,
                    occurred_utc=item.occurred_utc,
                    local_date=item.local_date,
                    ref=item.ref,
                    id=cursor.lastrowid,
                    created_at=now,
                )
                saved.append(stored)

        return saved

    def known_refs(self, project: str, type: ActivityType) -> set[str]:
        rows = self._conn.execute(
            "SELECT ref FROM activity WHERE project = ? AND type = ? AND ref IS NOT NULL",
            (project, str(type)),
        ).fetchall()
        return {row["ref"] for row in rows}

    def activities_on(self, project: str, day: date) -> list[Activity]:
        return self.activities_between(project, day, day)

    def activities_between(
        self, project: str, start: date, end: date
    ) -> list[Activity]:
        rows = self._conn.execute(
            """
            SELECT * FROM activity
            WHERE project = ? AND local_date BETWEEN ? AND ?
            ORDER BY occurred_utc, id
            """,
            (project, start.isoformat(), end.isoformat()),
        ).fetchall()
        return [_row_to_activity(row) for row in rows]

    def save_draft(
        self, document: Document, source_activity_ids: Sequence[int] = ()
    ) -> Document:
        existing = self.get_document(
            document.project, document.type, document.period_start, document.period_end
        )
        if existing is not None and existing.status is DocumentStatus.APPROVED:
            raise ValueError(
                f"이미 승인된 문서입니다 (id={existing.id}). "
                "덮어쓰려면 먼저 상태를 DRAFT 로 되돌리세요."
            )

        now = utc_now_iso()
        with self._conn:
            if existing is None:
                cursor = self._conn.execute(
                    """
                    INSERT INTO document
                        (project, type, title, body_md, period_start, period_end,
                         status, source_path, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'DRAFT', ?, ?, ?)
                    """,
                    (
                        document.project,
                        str(document.type),
                        document.title,
                        document.body_md,
                        document.period_start,
                        document.period_end,
                        document.source_path,
                        now,
                        now,
                    ),
                )
                document_id = int(cursor.lastrowid)
            else:
                document_id = int(existing.id)
                self._conn.execute(
                    """
                    UPDATE document
                       SET title = ?, body_md = ?, source_path = ?,
                           status = 'DRAFT', updated_at = ?
                     WHERE id = ?
                    """,
                    (document.title, document.body_md, document.source_path, now, document_id),
                )
            self._conn.execute(
                "DELETE FROM document_source WHERE document_id = ?", (document_id,)
            )
            self._conn.executemany(
                "INSERT INTO document_source (document_id, activity_id) VALUES (?, ?)",
                [(document_id, int(activity_id)) for activity_id in source_activity_ids],
            )

        stored = self._document_by_id(document_id)
        assert stored is not None
        return stored

    def get_document(
        self,
        project: str,
        type: DocumentType,
        period_start: str | None = None,
        period_end: str | None = None,
    ) -> Document | None:
        row = self._conn.execute(
            """
            SELECT * FROM document
             WHERE project = ? AND type = ?
               AND period_start IS ? AND period_end IS ?
             ORDER BY updated_at DESC, id DESC
             LIMIT 1
            """,
            (project, str(type), period_start, period_end),
        ).fetchone()
        return _row_to_document(row) if row else None

    def apply_edit(
        self, document_id: int, body_md: str, status: DocumentStatus
    ) -> Document:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE document SET body_md = ?, status = ?, updated_at = ? WHERE id = ?",
                (body_md, str(status), utc_now_iso(), document_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"문서를 찾을 수 없습니다: id={document_id}")

        stored = self._document_by_id(document_id)
        assert stored is not None
        return stored

    def source_activity_ids(self, document_id: int) -> list[int]:
        rows = self._conn.execute(
            "SELECT activity_id FROM document_source WHERE document_id = ? ORDER BY activity_id",
            (document_id,),
        ).fetchall()
        return [int(row["activity_id"]) for row in rows]

    def _document_by_id(self, document_id: int) -> Document | None:
        row = self._conn.execute(
            "SELECT * FROM document WHERE id = ?", (document_id,)
        ).fetchone()
        return _row_to_document(row) if row else None


def _row_to_activity(row: sqlite3.Row) -> Activity:
    return Activity(
        project=row["project"],
        type=ActivityType(row["type"]),
        raw_text=row["raw_text"],
        occurred_at=row["occurred_at"],
        occurred_utc=row["occurred_utc"],
        local_date=row["local_date"],
        ref=row["ref"],
        id=row["id"],
        created_at=row["created_at"],
    )


def _row_to_document(row: sqlite3.Row) -> Document:
    return Document(
        project=row["project"],
        type=DocumentType(row["type"]),
        title=row["title"],
        body_md=row["body_md"],
        status=DocumentStatus(row["status"]),
        period_start=row["period_start"],
        period_end=row["period_end"],
        source_path=row["source_path"],
        id=row["id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )