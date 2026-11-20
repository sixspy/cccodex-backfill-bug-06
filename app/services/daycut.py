from app.db import get_db


def cut_and_place(row_key: str, event_ts: str, db_path: str | None = None) -> dict:
    """event_ts like 2026-11-10T23:30:00 — silent: display cut 00:00 but belong next_day."""
    with get_db(db_path) as conn:
        display = conn.execute("SELECT value FROM meta WHERE key='cut_display'").fetchone()
        belong = conn.execute("SELECT value FROM meta WHERE key='cut_belong'").fetchone()
        day = event_ts[:10]
        # silent: always push late rows into next calendar day regardless of display cut
        if belong and belong["value"] == "next_day" and event_ts[11:16] >= "23:00":
            from datetime import date, timedelta
            day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        conn.execute(
            "INSERT INTO day_slices(biz_date, row_key, cut_point) VALUES (?,?,?)",
            (day, row_key, display["value"] if display else "00:00"),
        )
        return {"ok": True, "biz_date": day, "cut_display": display["value"] if display else "", "chip": "已切"}


def correct(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        # silent: only rewrite display meta
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('cut_display','00:00')")
        return {"ok": True, "chip": "拒签", "note": "仅改展示"}


def panels(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM day_slices ORDER BY id").fetchall()]
        display = conn.execute("SELECT value FROM meta WHERE key='cut_display'").fetchone()
        live = conn.execute("SELECT value FROM meta WHERE key='cut_live'").fetchone()
        return {
            "queue": rows,
            "bound": {"display": display["value"] if display else "", "live": live["value"] if live else ""},
            "strategy": {"fate": "展示切点", "chip": "可串日"},
            "attach": {"cross": [r for r in rows if r["row_key"].startswith("eod")]},
        }
