
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from sextant.store import (
    Activity,
    ActivityType,
    Document,
    DocumentStatus,
    DocumentType,
    SQLiteStore,
)

KST = timezone(timedelta(hours=9))
UTC = timezone.utc


@pytest.fixture
def store(tmp_path):
    with SQLiteStore(tmp_path / "sextant.db") as opened:
        yield opened


def _commit(ref: str, when: datetime, project: str = "koala-back") -> Activity:
    return Activity.create(
        project=project,
        type=ActivityType.COMMIT,
        raw_text=f"commit {ref}",
        occurred=when,
        ref=ref,
    )


def test_create_rejects_naive_datetime():
    with pytest.raises(ValueError, match="타임존"):
        Activity.create(
            project="p",
            type=ActivityType.COMMIT,
            raw_text="x",
            occurred=datetime(2026, 8, 9, 8, 30),
            ref="abc",
        )


def test_local_date_follows_commit_timezone_not_utc():
    """KST 오전 8시 커밋은 UTC 로는 전날 23시다. 일지는 KST 기준 날짜에 들어가야 한다."""
    activity = _commit("abc", datetime(2026, 8, 9, 8, 30, tzinfo=KST))
    assert activity.local_date == "2026-08-09"
    assert activity.occurred_at.startswith("2026-08-09T08:30:00+09:00")


def test_duplicate_commit_is_skipped(store):
    when = datetime(2026, 8, 9, 10, 0, tzinfo=KST)

    assert store.add_activity(_commit("abc", when)) is not None
    assert store.add_activity(_commit("abc", when)) is None

    assert len(store.activities_on("koala-back", date(2026, 8, 9))) == 1


def test_same_ref_in_another_project_is_not_a_duplicate(store):
    when = datetime(2026, 8, 9, 10, 0, tzinfo=KST)

    assert store.add_activity(_commit("abc", when, project="a")) is not None
    assert store.add_activity(_commit("abc", when, project="b")) is not None


def test_notes_have_no_ref_and_stack_up(store):
    when = datetime(2026, 8, 9, 10, 0, tzinfo=KST)
    for text in ("첫 메모", "둘째 메모"):
        note = Activity.create(
            project="koala-back",
            type=ActivityType.NOTE,
            raw_text=text,
            occurred=when,
        )
        assert store.add_activity(note) is not None

    assert len(store.activities_on("koala-back", date(2026, 8, 9))) == 2


def test_add_activities_reports_only_inserted(store):
    when = datetime(2026, 8, 9, 10, 0, tzinfo=KST)
    store.add_activity(_commit("dup", when))

    saved = store.add_activities([_commit("dup", when), _commit("new", when)])

    assert [item.ref for item in saved] == ["new"]
    assert all(item.id is not None for item in saved)


def test_known_refs(store):
    when = datetime(2026, 8, 9, 10, 0, tzinfo=KST)
    store.add_activities([_commit("a", when), _commit("b", when)])

    assert store.known_refs("koala-back", ActivityType.COMMIT) == {"a", "b"}
    assert store.known_refs("koala-back", ActivityType.NOTE) == set()


def test_activities_between_is_inclusive_and_ordered(store):
    store.add_activities(
        [
            _commit("c", datetime(2026, 8, 11, 9, 0, tzinfo=KST)),
            _commit("a", datetime(2026, 8, 9, 9, 0, tzinfo=KST)),
            _commit("b", datetime(2026, 8, 10, 9, 0, tzinfo=KST)),
        ]
    )

    found = store.activities_between("koala-back", date(2026, 8, 9), date(2026, 8, 11))
    assert [item.ref for item in found] == ["a", "b", "c"]


def test_ordering_is_chronological_across_mixed_offsets(store):
    """오프셋이 섞여도 시간순이어야 한다.

    KST 08:30 커밋은 UTC 로 전날 23:30 이라 UTC 02:00 커밋보다 **먼저**다.
    occurred_at 문자열을 사전순으로 정렬하면 이 순서가 뒤집힌다.
    팀 레포에서는 기여자마다 오프셋이 달라 실제로 섞여 들어온다.
    """
    earlier = _commit("earlier", datetime(2026, 8, 9, 8, 30, tzinfo=KST))
    later = _commit("later", datetime(2026, 8, 9, 2, 0, tzinfo=UTC))
    store.add_activities([later, earlier])

    assert earlier.local_date == later.local_date == "2026-08-09"
    assert earlier.occurred_at > later.occurred_at  # 문자열 사전순은 뒤집혀 있다
    assert earlier.occurred_utc < later.occurred_utc

    found = store.activities_on("koala-back", date(2026, 8, 9))
    assert [item.ref for item in found] == ["earlier", "later"]


def _devlog(body: str = "초안") -> Document:
    return Document(
        project="koala-back",
        type=DocumentType.DEVLOG,
        title="2026-08-09 개발 일지",
        body_md=body,
        period_start="2026-08-09",
        period_end="2026-08-09",
        source_path="/logs/koala-back/2026-08-09.md",
    )


def test_save_draft_then_get(store):
    saved = store.save_draft(_devlog())

    assert saved.id is not None
    assert saved.status is DocumentStatus.DRAFT

    found = store.get_document(
        "koala-back", DocumentType.DEVLOG, "2026-08-09", "2026-08-09"
    )
    assert found is not None and found.id == saved.id


def test_rerunning_log_overwrites_draft_in_place(store):
    first = store.save_draft(_devlog("첫 초안"))
    second = store.save_draft(_devlog("다시 생성한 초안"))

    assert second.id == first.id
    assert second.body_md == "다시 생성한 초안"


def test_approved_document_is_protected_from_regeneration(store):
    saved = store.save_draft(_devlog("초안"))
    store.apply_edit(saved.id, "사람이 고친 최종본", DocumentStatus.APPROVED)

    with pytest.raises(ValueError, match="이미 승인된 문서"):
        store.save_draft(_devlog("LLM 재생성분"))

    kept = store.get_document(
        "koala-back", DocumentType.DEVLOG, "2026-08-09", "2026-08-09"
    )
    assert kept.body_md == "사람이 고친 최종본"


def test_apply_edit_unknown_id(store):
    with pytest.raises(KeyError):
        store.apply_edit(9999, "본문", DocumentStatus.APPROVED)


def test_sources_are_tracked_and_replaced(store):
    when = datetime(2026, 8, 9, 10, 0, tzinfo=KST)
    activities = store.add_activities([_commit("a", when), _commit("b", when)])
    ids = [item.id for item in activities]

    saved = store.save_draft(_devlog(), source_activity_ids=ids)
    assert store.source_activity_ids(saved.id) == sorted(ids)

    # 재생성하면 근거도 새로 쓴다 — 옛 근거가 남으면 본문과 어긋난다.
    store.save_draft(_devlog("재생성"), source_activity_ids=[ids[0]])
    assert store.source_activity_ids(saved.id) == [ids[0]]


def test_deleting_activity_cascades_to_sources(store):
    when = datetime(2026, 8, 9, 10, 0, tzinfo=KST)
    activity = store.add_activity(_commit("a", when))
    saved = store.save_draft(_devlog(), source_activity_ids=[activity.id])

    store._conn.execute("DELETE FROM activity WHERE id = ?", (activity.id,))
    store._conn.commit()

    assert store.source_activity_ids(saved.id) == []


def test_reopening_existing_db_keeps_data(tmp_path):
    path = tmp_path / "sextant.db"
    when = datetime(2026, 8, 9, 10, 0, tzinfo=KST)

    with SQLiteStore(path) as first:
        first.add_activity(_commit("a", when))

    with SQLiteStore(path) as second:
        assert second.known_refs("koala-back", ActivityType.COMMIT) == {"a"}


def test_future_schema_version_is_refused(tmp_path):
    path = tmp_path / "sextant.db"
    with SQLiteStore(path) as opened:
        opened._conn.execute("PRAGMA user_version = 99")
        opened._conn.commit()

    with pytest.raises(RuntimeError, match="스키마 버전"):
        SQLiteStore(path)