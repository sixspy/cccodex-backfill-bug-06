import sqlite3
import threading

import pytest

from app.db import get_db, init_db
from app.services import daycut
from app.services.errors import ServiceError


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "svc.db")
    init_db(path)
    return path


def test_in_process_lock_serializes_corrections(db):
    """两侧调度同抢纠偏：只有一个版本号落地，其余按过期拒签，不覆盖切点行。"""
    daycut.correct("00:00", db_path=db)
    with get_db(db) as conn:
        base = daycut.read_state(conn)["version"]

    outcomes = []
    barrier = threading.Barrier(6)

    def worker():
        barrier.wait()
        try:
            result = daycut.correct("23:00", expected_version=base, db_path=db)
            outcomes.append(("ok", result["version"]))
        except ServiceError as exc:
            outcomes.append(("rejected", exc.message))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [item for item in outcomes if item[0] == "ok"]
    assert len(winners) == 1
    assert len(outcomes) == 6
    with get_db(db) as conn:
        state = daycut.read_state(conn)
        assert state["version"] == base + 1
        assert (state["cut_point"], state["display"], state["live"]) == ("23:00", "23:00", "23:00")


def test_same_cut_is_idempotent_across_threads(db):
    """同一目标切点并发纠偏：已同源时是空操作，不反复递增版本。"""
    daycut.correct("00:00", db_path=db)
    with get_db(db) as conn:
        base = daycut.read_state(conn)["version"]
    barrier = threading.Barrier(5)
    seen = []
    errors = []

    def worker():
        barrier.wait()
        try:
            seen.append(daycut.correct("00:00", expected_version=base, db_path=db)["version"])
        except ServiceError as exc:
            errors.append(exc.message)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert set(seen) == {base}


def test_active_row_unique_constraint_blocks_second_landing(db):
    """唯一约束兜底：同一键不能同时占两个活动落点。"""
    daycut.correct("00:00", db_path=db)
    daycut.cut_and_place("k1", "2026-11-10T10:00:00", db_path=db)
    with pytest.raises(sqlite3.IntegrityError):
        with get_db(db) as conn:
            conn.execute(
                "INSERT INTO day_slices(biz_date, row_key, cut_point, event_ts, event_date, "
                "cross_day, status, chip, reason, created_at) "
                "VALUES ('2026-11-11','k1','00:00','2026-11-11T10:00:00','2026-11-11',"
                "0,'已落库','已切','',datetime('now'))"
            )
    with get_db(db) as conn:
        active = conn.execute(
            "SELECT COUNT(*) AS c FROM day_slices WHERE row_key='k1' AND status='已落库'"
        ).fetchone()["c"]
        assert active == 1


def test_bad_event_ts_returns_plain_language(db):
    with pytest.raises(ServiceError) as exc:
        daycut.cut_and_place("k", "not-a-time", db_path=db)
    assert exc.value.status_code == 400
    assert "格式" in exc.value.message


def test_correction_rewrites_old_slice_to_rejected(db):
    """旁路改写：旧切点片拒签留痕，自然日占唯一落点。"""
    daycut.correct("00:00", db_path=db)
    daycut.cut_and_place("eod", "2026-11-10T23:30:00", db_path=db)
    # 模拟历史串日脏片：活动行被错放到次日
    with get_db(db) as conn:
        conn.execute(
            "UPDATE day_slices SET biz_date='2026-11-11' WHERE row_key='eod' AND status='已落库'"
        )
    result = daycut.correct("00:00", db_path=db)
    assert result["rewritten"] == 1
    with get_db(db) as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT row_key, biz_date, status FROM day_slices WHERE row_key='eod' ORDER BY id"
            )
        ]
    assert [row["status"] for row in rows] == ["拒签", "已落库"]
    assert rows[0]["biz_date"] == "2026-11-11"
    assert rows[1]["biz_date"] == "2026-11-10"
