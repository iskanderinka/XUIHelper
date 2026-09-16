import sqlite3
import os
import json
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DB_DIR = os.environ.get("DB_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
DB_PATH = os.path.join(DB_DIR, "traffic.db")


def _get_conn() -> sqlite3.Connection:
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS traffic_records (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            panel_name  TEXT    NOT NULL,
            email       TEXT    NOT NULL,
            record_date TEXT    NOT NULL,
            upload      INTEGER DEFAULT 0,
            download    INTEGER DEFAULT 0,
            total_bytes INTEGER DEFAULT 0,
            expiry_time INTEGER DEFAULT 0,
            created_at  TEXT    DEFAULT (datetime('now','localtime')),
            UNIQUE(panel_name, email, record_date)
        );
        CREATE INDEX IF NOT EXISTS idx_tr_date  ON traffic_records(record_date);
        CREATE INDEX IF NOT EXISTS idx_tr_panel ON traffic_records(panel_name);
        CREATE INDEX IF NOT EXISTS idx_tr_email ON traffic_records(email);

        CREATE TABLE IF NOT EXISTS bot_users (
            tg_id        INTEGER PRIMARY KEY,
            username     TEXT,
            first_name   TEXT,
            first_seen   TEXT    DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS client_bindings (
            tg_id        INTEGER NOT NULL,
            panel_name   TEXT    NOT NULL,
            email        TEXT    NOT NULL,
            inbound_ids  TEXT    NOT NULL,
            sub_id       TEXT    NOT NULL,
            uuid         TEXT    NOT NULL,
            limit_hwid   INTEGER DEFAULT 0,
            created_at   TEXT    DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (tg_id, panel_name, email)
        );
        CREATE INDEX IF NOT EXISTS idx_cb_tg    ON client_bindings(tg_id);
        CREATE INDEX IF NOT EXISTS idx_cb_email ON client_bindings(panel_name, email);
    """)
    conn.commit()
    conn.close()
    logger.info("Database initialised at %s", DB_PATH)


# ---------- Снимки трафика ----------

def batch_record_traffic(records: List[Tuple]):
    """Each tuple: (panel_name, email, upload, download, total_bytes, expiry_time, record_date)"""
    if not records:
        return
    conn = _get_conn()
    conn.executemany(
        """INSERT INTO traffic_records
               (panel_name, email, upload, download, total_bytes, expiry_time, record_date)
          VALUES (?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(panel_name, email, record_date)
           DO UPDATE SET upload=excluded.upload, download=excluded.download,
                         total_bytes=excluded.total_bytes, expiry_time=excluded.expiry_time,
                         created_at=datetime('now','localtime')""",
        records,
    )
    conn.commit()
    conn.close()


def cleanup_old_traffic(retention_days: int = 365,
                        reference_date: Optional[str] = None) -> int:
    """Удаляет снимки трафика за пределами окна хранения."""
    retention_days = max(1, int(retention_days))
    if reference_date:
        reference = datetime.strptime(reference_date, "%Y-%m-%d").date()
    else:
        reference = datetime.now().date()
    cutoff = reference - timedelta(days=retention_days - 1)

    conn = _get_conn()
    cursor = conn.execute(
        "DELETE FROM traffic_records WHERE record_date < ?",
        (cutoff.strftime("%Y-%m-%d"),),
    )
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    if deleted:
        logger.info("Удалено %s записей трафика старше %s.", deleted, cutoff)
    return deleted


def _delta_cte() -> str:
    """CTE: вычисляет посуточные дельты из кумулятивных снимков."""
    return """
    WITH deltas AS (
        SELECT
            a.record_date,
            a.panel_name,
            a.email,
            CASE
                WHEN b.upload   IS NOT NULL AND a.upload   >= b.upload
                    THEN a.upload   - b.upload
               WHEN b.upload   IS NOT NULL AND a.upload   <  b.upload
                   THEN a.upload
                ELSE 0
           END AS delta_up,
           CASE
               WHEN b.download IS NOT NULL AND a.download >= b.download
                   THEN a.download - b.download
               WHEN b.download IS NOT NULL AND a.download <  b.download
                   THEN a.download
                ELSE 0
           END AS delta_down
        FROM traffic_records a
        LEFT JOIN traffic_records b
            ON b.panel_name  = a.panel_name
           AND b.email       = a.email
           AND b.record_date = date(a.record_date, '-1 day')
    )
    """


def get_daily_stats(start_date: str, end_date: str,
                    panel_name: Optional[str] = None) -> List[Dict]:
    conn = _get_conn()
    where = ["deltas.record_date >= ?", "deltas.record_date <= ?"]
    params: list = [start_date, end_date]
    if panel_name:
        where.append("deltas.panel_name = ?")
        params.append(panel_name)
    sql = _delta_cte() + f"""
        SELECT record_date,
               SUM(delta_up) AS total_upload,
               SUM(delta_down) AS total_download,
               SUM(delta_up + delta_down) AS daily_total
        FROM deltas
        WHERE {' AND '.join(where)}
        GROUP BY record_date
        ORDER BY record_date
    """
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [{"record_date": r["record_date"],
             "total_upload": r["total_upload"] or 0,
             "total_download": r["total_download"] or 0,
             "daily_total": r["daily_total"] or 0} for r in rows]


def get_panel_daily_stats(start_date: str, end_date: str) -> List[Dict]:
    conn = _get_conn()
    sql = _delta_cte() + """
        SELECT record_date, panel_name,
               SUM(delta_up + delta_down) AS daily_total
        FROM deltas
        WHERE record_date >= ? AND record_date <= ?
        GROUP BY record_date, panel_name
        ORDER BY record_date, panel_name
    """
    rows = conn.execute(sql, [start_date, end_date]).fetchall()
    conn.close()
    return [{"record_date": r["record_date"],
             "panel_name": r["panel_name"],
             "daily_total": r["daily_total"] or 0} for r in rows]


def get_user_daily_stats(start_date: str, end_date: str,
                          panel_name: str, email: Optional[str] = None,
                          limit: int = 100) -> List[Dict]:
    conn = _get_conn()
    params: list = [start_date, end_date, panel_name]
    where = ["deltas.record_date >= ?", "deltas.record_date <= ?", "deltas.panel_name = ?"]
    if email:
        where.append("deltas.email = ?")
        params.append(email)
    sql = _delta_cte() + f"""
        SELECT record_date, email,
               SUM(delta_up + delta_down) AS daily_total
        FROM deltas
        WHERE {' AND '.join(where)}
        GROUP BY record_date, email
        ORDER BY record_date, daily_total DESC
        LIMIT ?
    """
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [{"record_date": r["record_date"],
             "email": r["email"],
             "daily_total": r["daily_total"] or 0} for r in rows]


def get_top_users(start_date: str, end_date: str,
                  panel_name: Optional[str] = None, limit: int = 20) -> List[Dict]:
    conn = _get_conn()
    where = ["deltas.record_date >= ?", "deltas.record_date <= ?"]
    params: list = [start_date, end_date]
    if panel_name:
        where.append("deltas.panel_name = ?")
        params.append(panel_name)
    sql = _delta_cte() + f"""
        SELECT email, panel_name,
               SUM(delta_up + delta_down) AS total_usage
        FROM deltas
        WHERE {' AND '.join(where)}
        GROUP BY email, panel_name
        ORDER BY total_usage DESC
        LIMIT ?
    """
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [{"email": r["email"],
             "panel_name": r["panel_name"],
             "total_usage": r["total_usage"] or 0} for r in rows]


def get_latest_snapshot(panel_name: Optional[str] = None) -> List[Dict]:
    conn = _get_conn()
    if panel_name:
        sql = """
            SELECT t.* FROM traffic_records t
            INNER JOIN (
                SELECT panel_name, email, MAX(record_date) AS maxd
                FROM traffic_records GROUP BY panel_name, email
            ) m ON t.panel_name = m.panel_name
               AND t.email = m.email
               AND t.record_date = m.maxd
            WHERE t.panel_name = ?
            ORDER BY (t.upload + t.download) DESC
        """
        rows = conn.execute(sql, (panel_name,)).fetchall()
    else:
        sql = """
            SELECT t.* FROM traffic_records t
            INNER JOIN (
                SELECT panel_name, email, MAX(record_date) AS maxd
                FROM traffic_records GROUP BY panel_name, email
            ) m ON t.panel_name = m.panel_name
               AND t.email = m.email
               AND t.record_date = m.maxd
            ORDER BY t.panel_name, (t.upload + t.download) DESC
        """
        rows = conn.execute(sql).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_date_range() -> Optional[Tuple[str, str]]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT MIN(record_date) AS mind, MAX(record_date) AS maxd FROM traffic_records"
    ).fetchone()
    conn.close()
    if row and row["mind"]:
        return row["mind"], row["maxd"]
    return None


def has_daily_traffic_snapshot(record_date: str) -> bool:
    """Проверяет, есть ли за дату снимок, сделанный по расписанию (после 23:00)."""
    conn = _get_conn()
    row = conn.execute(
        """SELECT 1 FROM traffic_records
           WHERE record_date = ? AND time(created_at) >= '23:00:00'
           LIMIT 1""",
        (record_date,),
    ).fetchone()
    conn.close()
    return row is not None


def get_panel_user_list(panel_name: str) -> List[str]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT DISTINCT email FROM traffic_records WHERE panel_name = ? ORDER BY email",
        (panel_name,),
    ).fetchall()
    conn.close()
    return [r["email"] for r in rows]


def get_panel_summary_for_date(panel_name: str, record_date: str) -> List[Dict]:
    conn = _get_conn()
    rows = conn.execute(
        """SELECT * FROM traffic_records
           WHERE panel_name = ? AND record_date = ?
           ORDER BY (upload + download) DESC""",
        (panel_name, record_date),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------- bot_users: кто запускал бота ----------

def upsert_bot_user(tg_id: int, username: str = "", first_name: str = "") -> None:
    """Записывает или обновляет информацию о пользователе, запустившем бота."""
    conn = _get_conn()
    conn.execute(
        """INSERT INTO bot_users (tg_id, username, first_name)
           VALUES (?, ?, ?)
           ON CONFLICT(tg_id) DO UPDATE SET
               username   = excluded.username,
               first_name = excluded.first_name""",
        (int(tg_id), username or "", first_name or ""),
    )
    conn.commit()
    conn.close()


def is_bot_user(tg_id: int) -> bool:
    """Проверяет, запускал ли пользователь бота."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT 1 FROM bot_users WHERE tg_id = ? LIMIT 1", (int(tg_id),)
    ).fetchone()
    conn.close()
    return row is not None


def get_bot_user(tg_id: int) -> Optional[Dict]:
    """Возвращает запись о пользователе бота."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM bot_users WHERE tg_id = ?", (int(tg_id),)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_bot_users() -> List[Dict]:
    """Список всех, кто когда-либо запускал бота."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM bot_users ORDER BY first_seen DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------- client_bindings: связки клиентов ----------

def save_binding(
    tg_id: int,
    panel_name: str,
    email: str,
    inbound_ids: list,
    sub_id: str,
    uuid: str,
    limit_hwid: int = 0,
) -> None:
    """Сохраняет или обновляет связку клиента."""
    conn = _get_conn()
    conn.execute(
        """INSERT INTO client_bindings
               (tg_id, panel_name, email, inbound_ids, sub_id, uuid, limit_hwid)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(tg_id, panel_name, email) DO UPDATE SET
               inbound_ids = excluded.inbound_ids,
               sub_id      = excluded.sub_id,
               uuid        = excluded.uuid,
               limit_hwid  = excluded.limit_hwid""",
        (int(tg_id), panel_name, email, json.dumps(inbound_ids),
         sub_id, uuid, int(limit_hwid)),
    )
    conn.commit()
    conn.close()


def _parse_binding_row(row) -> Dict:
    """Преобразует строку из БД в словарь с распарсенным inbound_ids."""
    d = dict(row)
    try:
        d["inbound_ids"] = json.loads(d["inbound_ids"])
    except (ValueError, TypeError):
        d["inbound_ids"] = []
    return d


def get_user_bindings(tg_id: int) -> List[Dict]:
    """Возвращает все связки пользователя."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM client_bindings WHERE tg_id = ? ORDER BY panel_name, email",
        (int(tg_id),),
    ).fetchall()
    conn.close()
    return [_parse_binding_row(r) for r in rows]


def get_binding_by_email(panel_name: str, email: str) -> Optional[Dict]:
    """Находит связку по панели и email (для проверки занятости)."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM client_bindings WHERE panel_name = ? AND email = ?",
        (panel_name, email),
    ).fetchone()
    conn.close()
    return _parse_binding_row(row) if row else None


def delete_binding(tg_id: int, panel_name: str, email: str) -> bool:
    """Удаляет конкретную связку. Возвращает True, если что-то удалили."""
    conn = _get_conn()
    cursor = conn.execute(
        "DELETE FROM client_bindings WHERE tg_id = ? AND panel_name = ? AND email = ?",
        (int(tg_id), panel_name, email),
    )
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def list_all_bindings() -> List[Dict]:
    """Возвращает все связки (для админских команд)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM client_bindings ORDER BY panel_name, email"
    ).fetchall()
    conn.close()
    return [_parse_binding_row(r) for r in rows]