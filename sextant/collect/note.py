from __future__ import annotations

from datetime import datetime

from ..store import Activity, ActivityType


def build_note(project: str, text: str, occurred: datetime | None = None) -> Activity:
    """자유 메모를 activity 로 만든다.

    `astimezone()` 없이 `datetime.now()` 를 쓰면 tzinfo 가 없어 `Activity.create`
    에서 거부된다. 일부러 그렇게 해뒀다 — 메모의 날짜는 "내 하루"를 가르는
    기준이라 오프셋이 반드시 있어야 한다.
    """
    return Activity.create(
        project=project,
        type=ActivityType.NOTE,
        raw_text=text,
        occurred=occurred or datetime.now().astimezone(),
    )
