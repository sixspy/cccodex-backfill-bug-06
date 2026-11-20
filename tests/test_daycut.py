import pytest


async def _panels(authed):
    return (await authed.get("/api/daycut/panels")).json()


@pytest.mark.asyncio
async def test_planted_double_bound_rejects_cross_day(authed):
    """种植态：展示 00:00 / 现行 23:00 —— 双界，末日行必拒签且不占次日。"""
    panels = await _panels(authed)
    assert panels["bound"]["display"] == "00:00"
    assert panels["bound"]["live"] == "23:00"
    assert panels["bound"]["consistent"] is False
    assert panels["bound"]["chip"] == "拒签"

    r = await authed.post(
        "/api/daycut/place", json={"row_key": "eod-1", "event_ts": "2026-11-10T23:30:00"}
    )
    assert r.status_code == 409
    body = r.json()
    assert body["chip"] == "拒签"
    assert "双界" in body["error"]
    assert body["biz_date"] == "2026-11-10"  # 凭证留在自然日

    panels = await _panels(authed)
    assert panels["queue_chip"] == "拒签"
    assert panels["attach"]["verified"] is False
    assert "eod-1" not in panels["plan"].get("2026-11-10", [])
    assert "eod-1" not in panels["plan"].get("2026-11-11", [])


@pytest.mark.asyncio
async def test_late_row_never_swallowed_by_next_day_slice(authed):
    """末日行永不并入次日片：23:30 归属 11-10，次日计划里没有它。"""
    fixed = (await authed.post("/api/daycut/correct", json={"cut_point": "00:00"})).json()
    assert fixed["cut_point"] == "00:00"
    assert fixed["consistent"] is True

    placed = (
        await authed.post(
            "/api/daycut/place", json={"row_key": "eod-late", "event_ts": "2026-11-10T23:30:00"}
        )
    ).json()
    assert placed["biz_date"] == "2026-11-10"
    assert placed["cross_day"] is False

    panels = await _panels(authed)
    assert "eod-late" in panels["plan"]["2026-11-10"]
    assert "eod-late" not in panels["plan"].get("2026-11-11", [])


@pytest.mark.asyncio
async def test_non_midnight_cut_stays_double_bound(authed):
    """纠偏到 23:00 不是午夜边界：双界未解，末日行仍不滚进次日，队列继续拒签。"""
    result = (await authed.post("/api/daycut/correct", json={"cut_point": "23:00"})).json()
    assert result["cut_point"] == "23:00"
    assert result["consistent"] is False
    assert "双界未解" in result["note"]

    blocked = await authed.post(
        "/api/daycut/place", json={"row_key": "eod-2", "event_ts": "2026-11-11T23:40:00"}
    )
    assert blocked.status_code == 409
    assert blocked.json()["biz_date"] == "2026-11-11"

    panels = await _panels(authed)
    assert panels["bound"]["consistent"] is False
    assert panels["queue_chip"] == "拒签"
    assert "eod-2" not in panels["plan"].get("2026-11-12", [])


@pytest.mark.asyncio
async def test_double_bound_rejects_plan_and_replay(authed):
    """双界下切片计划与重放都要拒签（片归属写库必须同源）。"""
    windows = (await authed.get("/api/windows")).json()["items"]
    wid = windows[0]["id"]
    planned = (await authed.post(f"/api/windows/{wid}/plan")).json()
    sid = planned["slices"][0]["id"]

    await authed.post(
        "/api/daycut/place", json={"row_key": "eod-3", "event_ts": "2026-11-10T23:30:00"}
    )
    blocked = await authed.post(f"/api/windows/{wid}/plan")
    assert blocked.status_code == 409
    assert "双界" in blocked.json()["error"]

    # 收拢到午夜，重放恢复
    await authed.post("/api/daycut/correct", json={"cut_point": "00:00"})
    panels = await _panels(authed)
    assert panels["bound"]["consistent"] is True
    replay = await authed.post(f"/api/slices/{sid}/replay")
    assert replay.status_code == 200


@pytest.mark.asyncio
async def test_stale_version_rejected_on_concurrent_correct(authed):
    """两侧调度同抢纠偏：版本号过期的一方被拒，不覆盖切点行。"""
    first = (await authed.post("/api/daycut/correct", json={"cut_point": "00:00"})).json()
    stale = first["version"] - 1
    r = await authed.post(
        "/api/daycut/correct", json={"cut_point": "00:00", "expected_version": stale}
    )
    assert r.status_code == 409
    assert "另一侧调度" in r.json()["error"]
    panels = await _panels(authed)
    assert panels["bound"]["cut_point"] == "00:00"


@pytest.mark.asyncio
async def test_replay_rewrites_instead_of_duplicating(authed):
    """串日脏片只择一边落库：同键改写后只有一个活动落点，且不在次日计划。"""
    await authed.post("/api/daycut/correct", json={"cut_point": "00:00"})
    await authed.post(
        "/api/daycut/place", json={"row_key": "eod-x", "event_ts": "2026-11-10T23:30:00"}
    )
    again = (
        await authed.post(
            "/api/daycut/place", json={"row_key": "eod-x", "event_ts": "2026-11-10T23:30:00"}
        )
    ).json()
    assert again["already"] is True

    # 人为造一条旧切点归属 11-11 的活动片，模拟历史串日脏片
    from app.db import get_db
    import os

    db_path = os.environ["DATABASE_PATH"]
    with get_db(db_path) as conn:
        conn.execute(
            "UPDATE day_slices SET biz_date='2026-11-11' WHERE row_key='eod-x' AND status='已落库'"
        )

    fixed = (await authed.post("/api/daycut/correct", json={"cut_point": "00:00"})).json()
    assert fixed["rewritten"] == 1
    assert fixed["chip"] == "拒签"

    panels = await _panels(authed)
    active = [row for row in panels["queue"] if row["status"] == "已落库"]
    assert len([row for row in active if row["row_key"] == "eod-x"]) == 1
    assert [row["biz_date"] for row in active if row["row_key"] == "eod-x"] == ["2026-11-10"]
    assert "eod-x" not in panels["plan"].get("2026-11-11", [])
    assert "eod-x" in panels["plan"]["2026-11-10"]
    # 处置结局（拒签留痕）与当前健康度分开
    assert panels["disposition_chip"] == "拒签"
    assert panels["strategy"]["chip"] == "拒签"
    assert panels["attach"]["chip"] == "拒签"
    assert panels["queue_chip"] == "已切"


@pytest.mark.asyncio
async def test_unrelated_window_still_plannable_under_double_bound(authed):
    """与日界无关的窗口在双界下仍可计划，闸门不能误伤普通回灌。"""
    windows = (await authed.get("/api/windows")).json()["items"]
    wid = windows[1]["id"]
    planned = await authed.post(f"/api/windows/{wid}/plan")
    assert planned.status_code == 200
    assert len(planned.json()["slices"]) == 1


@pytest.mark.asyncio
async def test_replay_blocked_by_dirty_slice_and_cleared_by_correction(authed):
    """该日挂着串日脏片时重放拒签；纠偏回自然日后放行。"""
    windows = (await authed.get("/api/windows")).json()["items"]
    wid = windows[0]["id"]
    planned = (await authed.post(f"/api/windows/{wid}/plan")).json()
    sid = next(s["id"] for s in planned["slices"] if s["slice_date"] == "2026-11-12")

    # 先收拢切点，让双界不干扰；再注入一条历史串日脏片：
    # 事件在 11-11，却被旧切点错误归进 11-12 —— 次日片吞了末日行。
    await authed.post("/api/daycut/correct", json={"cut_point": "00:00"})

    from app.db import get_db
    import os

    with get_db(os.environ["DATABASE_PATH"]) as conn:
        conn.execute(
            "INSERT INTO day_slices(biz_date, row_key, cut_point, event_ts, event_date, "
            "cross_day, status, chip, reason, created_at) "
            "VALUES ('2026-11-12','eod-dirty','23:00','2026-11-11T23:50:00',"
            "'2026-11-11',1,'已落库','已切','',datetime('now'))"
        )

    blocked = await authed.post(f"/api/slices/{sid}/replay")
    assert blocked.status_code == 409
    assert "串日" in blocked.json()["error"]

    fixed = (await authed.post("/api/daycut/correct", json={"cut_point": "00:00"})).json()
    assert fixed["rewritten"] == 1
    replay = await authed.post(f"/api/slices/{sid}/replay")
    assert replay.status_code == 200
