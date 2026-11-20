import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path,
        check_same_thread=False,
        timeout=30.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# 进程内写锁：保证同一进程里的两侧调度（多线程）不会互相覆盖切点行。
# 跨进程/多容器由 SQLite 的 BEGIN IMMEDIATE 串行化，busy_timeout 负责等待。
_WRITE_LOCK = threading.RLock()


@contextmanager
def write_tx(db_path: str | None = None):
    """写事务：进程内锁 + BEGIN IMMEDIATE，读改写整段原子完成。"""
    path = db_path or Config.database()
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()


def log_op(conn: sqlite3.Connection, action: str, detail: str = "", chip: str = "") -> None:
    """编排留痕：每次切点/落点/纠偏/重放写一条可追溯记录。"""
    conn.execute(
        "INSERT INTO ops_log(action, detail, chip) VALUES (?,?,?)",
        (action, detail, chip),
    )


# day_slices 在旧库里只有 (id, biz_date, row_key, cut_point)，其余列由迁移补齐。
DAY_SLICE_EXTRA_COLUMNS = {
    "event_ts": "TEXT NOT NULL DEFAULT ''",
    "event_date": "TEXT NOT NULL DEFAULT ''",
    "cross_day": "INTEGER NOT NULL DEFAULT 0",
    "status": "TEXT NOT NULL DEFAULT '已落库'",
    "chip": "TEXT NOT NULL DEFAULT '已切'",
    "reason": "TEXT NOT NULL DEFAULT ''",
    "created_at": "TEXT NOT NULL DEFAULT ''",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS backfill_windows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('草稿','已计划','执行中','已完成')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS backfill_slices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL REFERENCES backfill_windows(id),
  slice_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待重放','已落库','失败')),
  row_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE(window_id, slice_date)
);

CREATE TABLE IF NOT EXISTS coverage_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL,
  planned INTEGER NOT NULL,
  loaded INTEGER NOT NULL,
  missing INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS day_slices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  biz_date TEXT NOT NULL,
  row_key TEXT NOT NULL,
  cut_point TEXT NOT NULL,
  event_ts TEXT NOT NULL DEFAULT '',
  event_date TEXT NOT NULL DEFAULT '',
  cross_day INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT '已落库',
  chip TEXT NOT NULL DEFAULT '已切',
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS ops_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  chip TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """把旧库的 day_slices 补齐到同源切片模型，并建立活动行唯一约束。"""
    cols = _columns(conn, "day_slices")
    for name, ddl in DAY_SLICE_EXTRA_COLUMNS.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE day_slices ADD COLUMN {name} {ddl}")

    # 旧行没有事件时间，只能按原归属日回填，避免把历史行误判成串日。
    conn.execute("UPDATE day_slices SET event_date = biz_date WHERE event_date = ''")
    conn.execute("UPDATE day_slices SET created_at = datetime('now') WHERE created_at = ''")
    conn.execute("UPDATE day_slices SET chip = status WHERE chip = ''")
    cut = conn.execute("SELECT value FROM meta WHERE key='cut_point'").fetchone()
    if cut:
        conn.execute(
            "UPDATE day_slices SET cut_point = ? WHERE cut_point = ''",
            (cut["value"],),
        )

    # 先消歧：同一 row_key 若已有多条活动行，只保留最早一条，其余转拒签留痕。
    dups = conn.execute(
        "SELECT row_key, MIN(id) AS keep_id, COUNT(*) AS c FROM day_slices "
        "WHERE status = '已落库' GROUP BY row_key HAVING c > 1"
    ).fetchall()
    for row in dups:
        conn.execute(
            "UPDATE day_slices SET status='拒签', chip='拒签', "
            "reason=COALESCE(NULLIF(reason,''),'历史重复落点，迁移时拒签') "
            "WHERE row_key=? AND status='已落库' AND id != ?",
            (row["row_key"], row["keep_id"]),
        )

    # 唯一约束只约束“仍占落点”的活动行；拒签行保留为追溯凭证，不占落点。
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_day_slices_active_row "
        "ON day_slices(row_key) WHERE status = '已落库'"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_day_slices_biz_date ON day_slices(biz_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_ops_log_action ON ops_log(action)"
    )


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        if conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone():
            return
        cur = conn.execute(
            "INSERT INTO backfill_windows(table_name, start_date, end_date, status) "
            "VALUES ('dw_orders_d','2026-11-10','2026-11-12','草稿')"
        )
        wid = cur.lastrowid
        conn.execute(
            "INSERT INTO backfill_windows(table_name, start_date, end_date, status) "
            "VALUES ('dw_payments_d','2026-11-11','2026-11-11','草稿')"
        )
        # placeholder so list is non-empty; slices created by plan
        _ = wid
        conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
        # 种植态：运营把切点配置改成了 00:00，但现行切点仍是 23:00 —— 双界。
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('cut_display','00:00')")
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('cut_live','23:00')")
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('cut_belong','next_day')")
        # 同源切点（唯一事实来源）与版本号；版本号用于纠偏并发校验。
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('cut_point','00:00')")
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('cut_version','0')")
        # 片归属按事件自然日，末日行绝不并入次日片。
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('cut_belong','day')")
