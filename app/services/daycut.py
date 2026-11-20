"""日切与片归属。

唯一归属规则：**片归属 = 事件自然日，绝不滚进次日片**。切点只有收拢到 00:00
（午夜边界）才与片归属同源；配置展示、现行调度、同源切点三者任一不是 00:00，
即为“双界”——串日队列、日界详情、切点策略、对账附页同亮拒签，落点、切片计划、
重放一律拒签，直到一次纠偏把三者一起收拢。

串日脏片处置（择一边落库）：**旁路串日凭证原子改写后再拒旧切点片**。一次事务里
先在事件自然日落下活动凭证，再把旧切点那一行标成拒签留痕；``day_slices(row_key)
WHERE status='已落库'`` 上的唯一索引兜底，竞争下也只有一个落点。拒签行是追溯凭证，
永不进入计划、不参与重放。
"""

import sqlite3
from datetime import datetime

from app.db import get_db, log_op, write_tx
from app.services.errors import ServiceError

# 片归属按自然日，唯一的同源切点就是午夜。
SOURCE_CUT = "00:00"
SOURCE_BELONG = "day"
_CUT_KEYS = ("cut_point", "cut_display", "cut_live", "cut_belong", "cut_version")

_TS_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M",
)


def _parse_ts(event_ts: str) -> datetime:
    raw = str(event_ts or "").strip()
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ServiceError("事件时间格式无效，请使用 2026-11-10T23:30:00 这样的格式", 400)


def _parse_hhmm(value, label: str = "切点") -> str:
    raw = str(value or "").strip()
    try:
        hh, mm = raw.split(":")
        hour, minute = int(hh), int(mm)
    except (ValueError, AttributeError):
        raise ServiceError(f"{label}格式无效，请使用 HH:MM（例如 00:00）", 400)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ServiceError(f"{label}超出范围：小时 00-23、分钟 00-59", 400)
    return f"{hour:02d}:{minute:02d}"


def read_state(conn) -> dict:
    keys = ",".join("?" * len(_CUT_KEYS))
    meta = {
        row["key"]: row["value"]
        for row in conn.execute(
            f"SELECT key, value FROM meta WHERE key IN ({keys})", _CUT_KEYS
        )
    }
    point = meta.get("cut_point") or meta.get("cut_display") or SOURCE_CUT
    display = meta.get("cut_display") or point
    live = meta.get("cut_live") or point
    belong = meta.get("cut_belong") or SOURCE_BELONG
    try:
        version = int(meta.get("cut_version") or 0)
    except (TypeError, ValueError):
        version = 0
    consistent = point == display == live == SOURCE_CUT
    return {
        "cut_point": point,
        "display": display,
        "live": live,
        "belong": belong,
        "version": version,
        "consistent": consistent,
    }


def _natural_day(event_ts: str) -> str:
    """片归属日：事件自然日。没有事件时间的历史行由调用方另行处理。"""
    return _parse_ts(event_ts).date().isoformat()


def _is_cross_day(row) -> bool:
    return bool(row["event_date"]) and row["biz_date"] != row["event_date"]


def _double_bound_reason(state: dict) -> str:
    offenders = []
    if state["display"] != SOURCE_CUT:
        offenders.append(f"配置展示 {state['display']}")
    if state["live"] != SOURCE_CUT:
        offenders.append(f"现行调度 {state['live']}")
    if state["cut_point"] != SOURCE_CUT:
        offenders.append(f"同源切点 {state['cut_point']}")
    detail = "、".join(offenders) or "三处切点不一致"
    return (
        f"切点双界：{detail} 未收拢到午夜 {SOURCE_CUT}；片归属按自然日，"
        "本片已拒签、绝不并入次日片。请先在切点策略页执行一次纠偏"
    )


def _insert_row(conn, row_key, biz_date, cut_point, event_ts, event_date,
                status, chip, reason) -> int:
    cur = conn.execute(
        "INSERT INTO day_slices(biz_date, row_key, cut_point, event_ts, event_date, "
        "cross_day, status, chip, reason, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))",
        (
            biz_date,
            row_key,
            cut_point,
            event_ts,
            event_date,
            int(biz_date != event_date) if event_date else 0,
            status,
            chip,
            reason,
        ),
    )
    return cur.lastrowid


def _get_row(conn, row_id: int):
    return conn.execute("SELECT * FROM day_slices WHERE id=?", (row_id,)).fetchone()


def _row_payload(row, state: dict, chip: str, **extra) -> dict:
    data = {
        "ok": True,
        "id": row["id"],
        "row_key": row["row_key"],
        "biz_date": row["biz_date"],
        "event_date": row["event_date"],
        "cross_day": bool(row["cross_day"]),
        "cut_point": row["cut_point"],
        "status": row["status"],
        "chip": chip,
        "reason": row["reason"],
        "version": state["version"],
    }
    data.update(extra)
    return data


def cut_and_place(row_key: str, event_ts: str, db_path: str | None = None) -> dict:
    """放行一条日界事件。双界或串日冲突时拒签，且末日行绝不并入次日片。"""
    key = str(row_key or "").strip()
    if not key:
        raise ServiceError("落点键不能为空", 400)
    event_day = _natural_day(event_ts)

    rejection: tuple[str, dict] | None = None
    with write_tx(db_path) as conn:
        state = read_state(conn)

        # 1) 双界：硬拦，只留拒签凭证；凭证落在事件自然日，绝不占次日落点。
        if not state["consistent"]:
            reason = _double_bound_reason(state)
            row_id = _insert_row(
                conn, key, event_day, state["cut_point"], event_ts, event_day,
                "拒签", "拒签", reason,
            )
            log_op(conn, "place", f"{key}@{event_ts} 双界拒签，末日行未并入次日", "拒签")
            rejection = (reason, _row_payload(_get_row(conn, row_id), state, "拒签"))
        else:
            existing = conn.execute(
                "SELECT * FROM day_slices WHERE row_key=? AND status='已落库'", (key,)
            ).fetchone()

            # 2) 同键幂等；若旧凭证落在别的日子（串日脏片），改写回自然日并拒旧片。
            if existing and existing["biz_date"] == event_day:
                log_op(conn, "place", f"{key} 已占落点 {event_day}", existing["chip"])
                return _row_payload(existing, state, existing["chip"], already=True)

            if existing:
                old_day = existing["biz_date"]
                conn.execute(
                    "UPDATE day_slices SET status='拒签', chip='拒签', reason=? WHERE id=?",
                    (
                        f"旧切点片拒签：该键已改写到事件自然日 {event_day}；"
                        f"旧切点 {existing['cut_point']} 归属 {old_day} 不再占落点",
                        existing["id"],
                    ),
                )
                new_id = _insert_row(
                    conn, key, event_day, state["cut_point"], event_ts, event_day,
                    "已落库", "已切", "",
                )
                log_op(
                    conn, "rewrite",
                    f"{key} 凭证 {old_day}→{event_day}；旧切点片 #{existing['id']} 拒签",
                    "拒签",
                )
                return _row_payload(
                    _get_row(conn, new_id), state, "已切", outcome="改写",
                    rewritten=True, old_biz_date=old_day,
                    rejected_id=existing["id"], rejected_chip="拒签",
                )

            # 3) 全新键：落事件自然日。
            new_id = _insert_row(
                conn, key, event_day, state["cut_point"], event_ts, event_day,
                "已落库", "已切", "",
            )
            log_op(conn, "place", f"{key}@{event_ts} → {event_day}（自然日归属）", "已切")
            return _row_payload(_get_row(conn, new_id), state, "已切")

    assert rejection is not None
    message, payload = rejection
    raise ServiceError(message, 409, payload)


def correct(
    cut_point: str | None = None,
    expected_version: int | None = None,
    db_path: str | None = None,
) -> dict:
    """一次收拢纠偏：切点同源 + 串日凭证改回自然日 + 旧切点片拒签 + 版本号递增。"""
    requested = _parse_hhmm(cut_point) if cut_point not in (None, "") else None

    with write_tx(db_path) as conn:
        state = read_state(conn)
        if expected_version is not None:
            try:
                expected = int(expected_version)
            except (TypeError, ValueError):
                raise ServiceError("期望版本号无效，请刷新后重试", 400)
            if expected != state["version"]:
                raise ServiceError(
                    f"切点已被另一侧调度改写（当前版本 v{state['version']}），"
                    "本次纠偏未落库，请刷新后重试",
                    409,
                    {"chip": "拒签", "version": state["version"]},
                )

        target = _parse_hhmm(
            requested or state["display"] or state["cut_point"] or SOURCE_CUT, "目标切点"
        )

        rows = conn.execute("SELECT * FROM day_slices WHERE status='已落库'").fetchall()
        rewrites = []
        for row in rows:
            if not row["event_ts"]:
                continue  # 历史行无事件时间，无从改归属，保持原样留待人工核对
            natural = _natural_day(row["event_ts"])
            if natural != row["biz_date"]:
                rewrites.append((row, natural))

        cut_changed = (
            state["cut_point"] != target
            or state["display"] != target
            or state["live"] != target
            or state["belong"] != SOURCE_BELONG
        )
        if not rewrites and not cut_changed:
            return {
                "ok": True,
                "cut_point": target,
                "version": state["version"],
                "rewritten": 0,
                "rewrites": [],
                "chip": "已切",
                "note": f"切点已同源在 {target}，无需纠偏",
            }

        new_rows = []
        try:
            for row, natural in rewrites:
                conn.execute(
                    "UPDATE day_slices SET status='拒签', chip='拒签', reason=? WHERE id=?",
                    (
                        f"纠偏改写：旧切点 {row['cut_point']} 归属 {row['biz_date']} → "
                        f"事件自然日 {natural}；旧切点片拒签",
                        row["id"],
                    ),
                )
                _insert_row(
                    conn, row["row_key"], natural, target, row["event_ts"],
                    row["event_date"], "已落库", "已切", "",
                )
                log_op(
                    conn, "rewrite",
                    f"纠偏 {row['row_key']} 归属 {row['biz_date']}→{natural}；"
                    f"旧切点片 #{row['id']} 拒签",
                    "拒签",
                )
                new_rows.append({"row_key": row["row_key"], "old": row["biz_date"], "new": natural})
        except sqlite3.IntegrityError:
            raise ServiceError(
                "纠偏失败：同一切点下出现重复落点，已整体回滚；请人工核对串日队列后重试",
                409,
                {"chip": "拒签"},
            )

        version = state["version"] + 1
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('cut_point',?)", (target,))
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('cut_display',?)", (target,))
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('cut_live',?)", (target,))
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES ('cut_belong',?)",
            (SOURCE_BELONG,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES ('cut_version',?)", (str(version),)
        )
        log_op(
            conn,
            "correct",
            f"切点 {state['cut_point']}→{target}（同源 {SOURCE_CUT}）；"
            f"改写串日片 {len(rewrites)} 条，旧切点片全部拒签",
            "拒签" if rewrites else "已切",
        )
        chip = "拒签" if rewrites else "已切"
        synced = target == SOURCE_CUT
        note = (
            f"切点已收拢到 {target}：改写串日片 {len(rewrites)} 条，旧切点片已拒签"
            if rewrites
            else f"切点已收拢到 {target}"
        )
        if not synced:
            note += f"；但 {target} 不是午夜边界，片归属仍按自然日，双界未解，串日队列继续亮拒签"
        else:
            note += "，展示与调度同源"
        return {
            "ok": True,
            "cut_point": target,
            "consistent": synced,
            "version": version,
            "rewritten": len(rewrites),
            "rewrites": new_rows,
            "chip": chip,
            "note": note,
        }


def ensure_plannable(start_date: str, end_date: str, db_path: str | None = None) -> None:
    """切片计划前置闸：串日片键与未收拢的双界，都不得把键带进任何一天的计划。"""
    with get_db(db_path) as conn:
        state = read_state(conn)
        rows = conn.execute(
            "SELECT * FROM day_slices WHERE status='已落库' "
            "AND (biz_date BETWEEN ? AND ? OR event_date BETWEEN ? AND ?)",
            (start_date, end_date, start_date, end_date),
        ).fetchall()
        bad = sorted({row["row_key"] for row in rows if _is_cross_day(row)})
        if bad:
            raise ServiceError(
                f"存在串日活动片（{'、'.join(bad)}），片归属与自然日切点不同源，"
                "切片计划已拒签；请先在切点策略页执行纠偏",
                409,
                {"chip": "拒签"},
            )
        if not state["consistent"]:
            touched = conn.execute(
                "SELECT COUNT(*) AS c FROM day_slices "
                "WHERE biz_date BETWEEN ? AND ? OR event_date BETWEEN ? AND ?",
                (start_date, end_date, start_date, end_date),
            ).fetchone()["c"]
            if touched:
                raise ServiceError(
                    f"切点双界：配置展示 {state['display']} 与现行调度 {state['live']} "
                    f"未收拢到 {SOURCE_CUT}，该窗口已触及日界数据，切片计划拒签；"
                    "请先在切点策略页执行纠偏",
                    409,
                    {"chip": "拒签"},
                )


def blockers_in(conn, start_date: str, end_date: str) -> list[dict]:
    """在给定连接上算该窗口范围的重放拦截证据（供重放事务内复用，避免检写分离）。

    拒签凭证不占落点，不拦重放——它是“没落进去”的留痕。双界时只要窗口触及日界
    数据就拦；与日界无关的普通回灌窗口照常重放。与计划闸同口径，避免计划已拒签
    却还能重放的缝隙。
    """
    state = read_state(conn)
    rows = conn.execute(
        "SELECT * FROM day_slices "
        "WHERE biz_date BETWEEN ? AND ? OR event_date BETWEEN ? AND ?",
        (start_date, end_date, start_date, end_date),
    ).fetchall()
    if not rows:
        return []
    if not state["consistent"]:
        return [{"reason": _double_bound_reason(state), "chip": "拒签"}]
    return [dict(row) for row in rows if row["status"] == "已落库" and _is_cross_day(row)]


def panels(db_path: str | None = None) -> dict:
    """串日队列 / 日界详情 / 切点策略 / 对账附页 四处同源出数。"""
    with get_db(db_path) as conn:
        state = read_state(conn)
        rows = [dict(r) for r in conn.execute("SELECT * FROM day_slices ORDER BY id").fetchall()]
        trace = [
            dict(r)
            for r in conn.execute(
                "SELECT action, detail, chip, created_at FROM ops_log ORDER BY id DESC LIMIT 12"
            ).fetchall()
        ]

    for row in rows:
        row["chip"] = row["chip"] or ("拒签" if row["status"] == "拒签" else "已切")
        row["cross_day"] = bool(row["cross_day"])

    active = [row for row in rows if row["status"] == "已落库"]
    rejected = [row for row in rows if row["status"] == "拒签" or row["chip"] == "拒签"]
    dirty = [row for row in active if _is_cross_day(row)]

    # 计划只吃活动片且按键归属日归档；串日脏片键单列出来，绝不进任何一天计划。
    plan: dict[str, list[str]] = {}
    for row in active:
        if not _is_cross_day(row):
            plan.setdefault(row["biz_date"], []).append(row["row_key"])
    for key in plan:
        plan[key].sort()
    planned_keys = {key for keys in plan.values() for key in keys}
    blocked_keys = sorted({row["row_key"] for row in dirty + rejected} - planned_keys)

    # 当前健康度：还有双界或串日活动片就是拒签。
    health_chip = "拒签" if (dirty or not state["consistent"]) else "已切"
    # 处置结局：以最近一次纠偏为准，没有纠偏就看健康度。
    last_correction = next((row for row in trace if row["action"] == "correct"), None)
    disposition_chip = (last_correction or {}).get("chip") or health_chip

    verified = not dirty and state["consistent"]
    bad_keys = "、".join(sorted({row["row_key"] for row in dirty}))
    if not state["consistent"]:
        attach_summary = "复验未通过：切点双界，落点与计划均拒签；末日行未并入次日片"
    elif dirty:
        attach_summary = f"复验未通过：存在串日活动片（{bad_keys}），片键已挡在计划之外"
    else:
        attach_summary = "复验通过：无串日活动片，末日行与片键均未进入次日计划"
    if verified and rejected:
        attach_summary += f"；拒签留痕 {len(rejected)} 条为追溯凭证，不占落点"

    return {
        "queue": rows,
        "queue_chip": health_chip,
        "health_chip": health_chip,
        "disposition_chip": disposition_chip,
        "rejected_count": len(rejected),
        "bound": {
            "display": state["display"],
            "live": state["live"],
            "cut_point": state["cut_point"],
            "belong": f"自然日（同源 {SOURCE_CUT}）",
            "version": state["version"],
            "consistent": state["consistent"],
            "chip": "拒签" if not state["consistent"] else "已切",
        },
        "strategy": {
            "chip": disposition_chip,
            "health_chip": health_chip,
            "fate": "片归属按事件自然日；切点只有收拢到 00:00 才与片归属同源",
            "disposition": "旁路 atomic 改写回自然日 + 旧切点片拒签 + 活动行唯一约束兜底",
            "detail": "同一键只保留一个落点；末日行绝不并入次日片；拒签片留痕且不占落点。",
            "version": state["version"],
            "last_correction": last_correction,
        },
        "attach": {
            "chip": disposition_chip,
            "health_chip": health_chip,
            "verified": verified,
            "summary": attach_summary,
            "cross": dirty,
            "mismatched": dirty,
            "rejected": rejected,
            "blocked_keys": blocked_keys,
            "plan": plan,
            "trace": trace,
            "steps": [
                "1. 日界详情核对配置展示、现行调度与同源切点是否都收拢到 00:00",
                "2. 双界时串日队列亮拒签，先执行纠偏收拢切点再放行",
                "3. 放行后核对对账附页：串日样本为 0，末日行片键不在次日计划",
                "4. 再跨一个自然日切点，重放前复验附页仍为通过",
            ],
        },
        "plan": plan,
        "trace": trace,
    }
