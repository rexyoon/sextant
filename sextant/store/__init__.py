"""저장 계층.

`base` 가 인터페이스, `sqlite` 가 Phase 1 구현이다.
Phase 2 에서 `postgres.py` 를 추가해 갈아끼우는 것이 이 추상화의 목적이므로,
바깥 코드는 반드시 `Store` 타입에만 의존한다 (SQLiteStore 를 직접 타이핑하지 말 것).
"""

from .base import (
    Activity,
    ActivityStore,
    ActivityType,
    Document,
    DocumentStatus,
    DocumentStore,
    DocumentType,
    Store,
    utc_now_iso,
)
from .sqlite import SQLiteStore

__all__ = [
    "Activity",
    "ActivityStore",
    "ActivityType",
    "Document",
    "DocumentStatus",
    "DocumentStore",
    "DocumentType",
    "SQLiteStore",
    "Store",
    "utc_now_iso",
]