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
    # Принудительный checkpoint WAL каждые 1000 страниц (~4 МБ)
    conn.execute("PRAGMA wal_autocheckpoint=1000")
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
            expiry_date  TEXT,
            paused_at    TEXT,
            comment      TEXT,
            created_at   TEXT    DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (tg_id, panel_name, email)
        );
        CREATE INDEX IF NOT EXISTS idx_cb_tg    ON client_bindings(tg_id);
        CREATE INDEX IF NOT EXISTS idx_cb_email ON client_bindings(panel_name, email);

        CREATE TABLE IF NOT EXISTS notification_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id       INTEGER NOT NULL,
            panel_name  TEXT    NOT NULL,
            email       TEXT    NOT NULL,
            kind        TEXT    NOT NULL,
            expiry_date TEXT    NOT NULL,
            sent_at     TEXT    DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_nl_key
            ON notification_log(tg_id, panel_name, email, kind);
    """)

    # Миграции для баз, созданных до этого обновления
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(client_bindings)").fetchall()]
    if "expiry_date" not in cols:
        conn.execute("ALTER TABLE client_bindings ADD COLUMN expiry_date TEXT")
        logger.info("Миграция: добавлена колонка expiry_date")
    if "paused_at" not in cols:
        conn.execute("ALTER TABLE client_bindings ADD COLUMN paused_at TEXT")
        logger.info("Миграция: добавлена колонка paused_at")
    if "comment" not in cols:
        conn.execute("ALTER TABLE client_bindings ADD COLUMN comment TEXT")
        logger.info("Миграция: добавлена колонка comment")
    if "enabled" not in cols:
        conn.execute("ALTER TABLE client_bindings ADD COLUMN enabled INTEGER DEFAULT 1")
        logger.info("Миграция: добавлена колонка enabled")

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


# ---------- client_bindings: связки клиентов ----------
def save_binding(
    tg_id: int,
    panel_name: str,
    email: str,
    inbound_ids: list,
    sub_id: str,
    uuid: str,
    limit_hwid: int = 0,
    expiry_date: str = None,
    comment: str = None,
    enabled: bool = True,
) -> None:
    """Сохраняет или обновляет связку клиента."""
    conn = _get_conn()
    conn.execute(
        """INSERT INTO client_bindings
               (tg_id, panel_name, email, inbound_ids, sub_id, uuid,
                limit_hwid, expiry_date, comment, enabled)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(tg_id, panel_name, email) DO UPDATE SET
               inbound_ids = excluded.inbound_ids,
               sub_id      = excluded.sub_id,
               uuid        = excluded.uuid,
               limit_hwid  = excluded.limit_hwid,
               expiry_date = excluded.expiry_date,
               comment     = excluded.comment,
               enabled     = excluded.enabled""",
        (int(tg_id), panel_name, email, json.dumps(inbound_ids),
         sub_id, uuid, int(limit_hwid), expiry_date, comment, 1 if enabled else 0),
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

# ---------- Лог уведомлений о сроке подписки ----------


def has_recent_notification(
    tg_id: int,
    panel_name: str,
    email: str,
    kind: str,
    expiry_date: str,
    within_hours: int = 20,
) -> bool:
    """
    Проверяет, отправляли ли это уведомление в последние within_hours часов.

    Учитывает expiry_date — если админ продлит подписку, старые записи
    не помешают новым напоминаниям.
    """
    conn = _get_conn()
    row = conn.execute(
        """SELECT 1 FROM notification_log
           WHERE tg_id = ? AND panel_name = ? AND email = ?
             AND kind = ? AND expiry_date = ?
             AND sent_at >= datetime('now', 'localtime', ?)
           LIMIT 1""",
        (int(tg_id), panel_name, email, kind, expiry_date, f'-{within_hours} hours'),
    ).fetchone()
    conn.close()
    return row is not None


def log_notification(
    tg_id: int,
    panel_name: str,
    email: str,
    kind: str,
    expiry_date: str,
) -> None:
    """Записывает факт отправки уведомления."""
    conn = _get_conn()
    conn.execute(
        """INSERT INTO notification_log
               (tg_id, panel_name, email, kind, expiry_date)
           VALUES (?, ?, ?, ?, ?)""",
        (int(tg_id), panel_name, email, kind, expiry_date),
    )
    conn.commit()
    conn.close()


def set_binding_paused(tg_id: int, panel_name: str, email: str,
                       paused_at: str = None) -> None:
    """Устанавливает/снимает метку паузы."""
    conn = _get_conn()
    conn.execute(
        """UPDATE client_bindings SET paused_at = ?
           WHERE tg_id = ? AND panel_name = ? AND email = ?""",
        (paused_at, int(tg_id), panel_name, email),
    )
    conn.commit()
    conn.close()


def update_binding_expiry(tg_id: int, panel_name: str, email: str,
                          new_expiry_date: str) -> None:
    """Обновляет срок действия в связке."""
    conn = _get_conn()
    conn.execute(
        """UPDATE client_bindings SET expiry_date = ?
           WHERE tg_id = ? AND panel_name = ? AND email = ?""",
        (new_expiry_date, int(tg_id), panel_name, email),
    )
    conn.commit()
    conn.close()


def list_all_bindings_with_users() -> List[Dict]:
    """Как list_all_bindings, но подтягивает username и first_name из bot_users."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT cb.*, bu.username AS bot_username, bu.first_name AS bot_first_name
           FROM client_bindings cb
           LEFT JOIN bot_users bu ON bu.tg_id = cb.tg_id
           ORDER BY cb.panel_name, cb.email"""
    ).fetchall()
    conn.close()
    return [_parse_binding_row(r) for r in rows]


def update_binding_email(tg_id: int, panel_name: str, old_email: str,
                          new_email: str) -> bool:
    """
    Меняет email в связке.

    Возвращает True при успехе, False если новый email уже занят
    (нарушение PRIMARY KEY).
    """
    conn = _get_conn()
    try:
        conn.execute(
            """UPDATE client_bindings SET email = ?
               WHERE tg_id = ? AND panel_name = ? AND email = ?""",
            (new_email, int(tg_id), panel_name, old_email),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def rename_traffic_email(panel_name: str, old_email: str, new_email: str) -> int:
    """Переписывает email в traffic_records. Возвращает число обновлённых строк."""
    conn = _get_conn()
    cursor = conn.execute(
        """UPDATE traffic_records SET email = ?
           WHERE panel_name = ? AND email = ?""",
        (new_email, panel_name, old_email),
    )
    updated = cursor.rowcount
    conn.commit()
    conn.close()
    return updated


def update_binding_limit_hwid(tg_id: int, panel_name: str, email: str,
                               new_limit: int) -> None:
    """Обновляет HWID-лимит в связке."""
    conn = _get_conn()
    conn.execute(
        """UPDATE client_bindings SET limit_hwid = ?
           WHERE tg_id = ? AND panel_name = ? AND email = ?""",
        (int(new_limit), int(tg_id), panel_name, email),
    )
    conn.commit()
    conn.close()


def update_binding_comment(tg_id: int, panel_name: str, email: str,
                           comment: str) -> None:
    """Обновляет комментарий в связке."""
    conn = _get_conn()
    conn.execute(
        """UPDATE client_bindings SET comment = ?
           WHERE tg_id = ? AND panel_name = ? AND email = ?""",
        (comment, int(tg_id), panel_name, email),
    )
    conn.commit()
    conn.close()


def update_binding_limit_hwid(tg_id: int, panel_name: str, email: str,
                               new_limit: int) -> None:
    """Обновляет HWID-лимит в связке."""
    conn = _get_conn()
    conn.execute(
        """UPDATE client_bindings SET limit_hwid = ?
           WHERE tg_id = ? AND panel_name = ? AND email = ?""",
        (int(new_limit), int(tg_id), panel_name, email),
    )
    conn.commit()
    conn.close()


def update_binding_email(tg_id: int, panel_name: str, old_email: str,
                          new_email: str) -> bool:
    """
    Меняет email в связке.

    Возвращает True при успехе, False если новый email уже занят
    (нарушение PRIMARY KEY).
    """
    conn = _get_conn()
    try:
        conn.execute(
            """UPDATE client_bindings SET email = ?
               WHERE tg_id = ? AND panel_name = ? AND email = ?""",
            (new_email, int(tg_id), panel_name, old_email),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def rename_traffic_email(panel_name: str, old_email: str, new_email: str) -> int:
    """Переписывает email в traffic_records. Возвращает число обновлённых строк."""
    conn = _get_conn()
    cursor = conn.execute(
        """UPDATE traffic_records SET email = ?
           WHERE panel_name = ? AND email = ?""",
        (new_email, panel_name, old_email),
    )
    updated = cursor.rowcount
    conn.commit()
    conn.close()
    return updated


def update_binding_enabled(tg_id: int, panel_name: str, email: str, enabled: bool) -> None:
    """Обновляет статус включён/отключён в связке."""
    conn = _get_conn()
    conn.execute(
        """UPDATE client_bindings SET enabled = ?
           WHERE tg_id = ? AND panel_name = ? AND email = ?""",
        (1 if enabled else 0, int(tg_id), panel_name, email),
    )
    conn.commit()
    conn.close()