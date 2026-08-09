from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum


class ActivityType(StrEnum):
    COMMIT = "COMMIT"
    NOTE = "NOTE"
    ERROR = "ERROR"


class DocumentType(StrEnum):
    DEVLOG = "DEVLOG"
    TROUBLESHOOT = "TROUBLESHOOT"
    RESUME = "RESUME"


class DocumentStatus(StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Activity:
    """활동 원본 한 건.

    `type` 이 내장 함수를 가리지만, 기획서 스키마의 컬럼명이라 그대로 둔다.
    """

    project: str
    type: ActivityType
    raw_text: str
    occurred_at: str
    occurred_utc: str
    local_date: str
    ref: str | None = None
    id: int | None = None
    created_at: str | None = None

    @classmethod
    def create(
        cls,
        *,
        project: str,
        type: ActivityType,
        raw_text: str,
        occurred: datetime,
        ref: str | None = None,
    ) -> Activity:
        """`occurred` 로부터 occurred_at 과 local_date 를 함께 만든다.

        왜 local_date 를 별도 컬럼으로 두는가:
        일지는 "내 하루" 단위인데, UTC 기준으로만 조회하면 KST 오전 8시 커밋이
        전날 UTC 23시가 되어 **하루 전 일지에 들어간다.** 커밋이 찍힌 타임존
        기준 날짜를 저장 시점에 확정해두면 조회가 정확하고 인덱스도 탄다.

        naive datetime 을 막는 이유: 어느 타임존인지 모르는 시각으로 local_date
        를 계산하면 위 문제를 조용히 되살린다. 호출부에서 tz 를 붙이게 강제한다.
        """
        if occurred.tzinfo is None:
            raise ValueError("occurred 에는 타임존이 있어야 한다 (naive datetime 금지)")
        return cls(
            project=project,
            type=ActivityType(type),
            raw_text=raw_text,
            occurred_at=occurred.isoformat(timespec="seconds"),
            occurred_utc=occurred.astimezone(timezone.utc).isoformat(timespec="seconds"),
            local_date=occurred.date().isoformat(),
            ref=ref,
        )


@dataclass
class Document:
    """파생 문서(일지·트러블슈팅) 한 건.

    `source_path` 는 렌더된 마크다운 경로다. DB 가 저장소, 파일이 편집면이고
    `approve` 가 파일을 되읽어 DB 에 반영한다 — 그 왕복의 반대편 주소다.
    """

    project: str
    type: DocumentType
    title: str
    body_md: str
    status: DocumentStatus = DocumentStatus.DRAFT
    period_start: str | None = None
    period_end: str | None = None
    source_path: str | None = None
    id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ActivityStore(ABC):
    @abstractmethod
    def add_activity(self, activity: Activity) -> Activity | None:
        """저장하고 id 가 채워진 사본을 돌려준다. ref 가 이미 있으면 None."""

    @abstractmethod
    def add_activities(self, activities: Iterable[Activity]) -> list[Activity]:
        """여러 건을 한 트랜잭션으로 저장한다. 중복은 건너뛰고 저장된 것만 돌려준다."""

    @abstractmethod
    def known_refs(self, project: str, type: ActivityType) -> set[str]:
        """이미 저장된 ref 집합. `sync` 가 재수집을 건너뛰는 데 쓴다."""

    @abstractmethod
    def activities_on(self, project: str, day: date) -> list[Activity]:
        """그 날(로컬 기준) 활동을 시간순으로."""

    @abstractmethod
    def activities_between(self, project: str, start: date, end: date) -> list[Activity]:
        """[start, end] 양끝 포함 구간의 활동을 시간순으로."""


class DocumentStore(ABC):
    @abstractmethod
    def save_draft(
        self, document: Document, source_activity_ids: Sequence[int] = ()
    ) -> Document:
        """DRAFT 를 저장한다. 같은 (project, type, 기간) 초안이 있으면 덮어쓴다.

        이미 APPROVED 면 덮어쓰지 않고 ValueError 를 낸다.
        사람이 승인한 문장을 LLM 재실행이 지우는 사고를 저장 계층에서 막는다.
        """

    @abstractmethod
    def get_document(
        self,
        project: str,
        type: DocumentType,
        period_start: str | None = None,
        period_end: str | None = None,
    ) -> Document | None:
        """기간이 일치하는 문서. 기간이 NULL 인 문서가 여럿이면 가장 최근 것."""

    @abstractmethod
    def apply_edit(
        self, document_id: int, body_md: str, status: DocumentStatus
    ) -> Document:
        """파일에서 되읽은 본문을 반영하고 상태를 바꾼다 (`approve` 경로)."""

    @abstractmethod
    def source_activity_ids(self, document_id: int) -> list[int]:
        """이 문서의 근거가 된 activity id 들. 추적 가능성의 실체."""


class Store(ActivityStore, DocumentStore, ABC):
    """두 저장소를 한 연결 위에서 제공하는 묶음."""

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.close()
        return False