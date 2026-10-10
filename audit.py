# =============================================================================
# audit.py — журнал действий администраторов
# =============================================================================
#
# Формат: одна строка на событие.
#   2026-10-09 15:30:22 [197066617 @iskan] /addclient args=[...]
#
# Файл: data/audit.log (рядом с traffic.db).
# Ротация: при превышении 1 МБ → audit.log.1 (старый .1 удаляется).
# Разделитель: при смене календарного дня в файл пишется визуальный блок из "*".
# =============================================================================

import logging
import os
from datetime import datetime
from functools import wraps
from typing import Optional
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ContextTypes

import config

logger = logging.getLogger(__name__)

# Путь — рядом с data/ (там же, где БД)
AUDIT_DIR = os.environ.get(
    "AUDIT_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"),
)
AUDIT_FILE = os.path.join(AUDIT_DIR, "audit.log")
AUDIT_MAX_SIZE = 1 * 1024 * 1024  # 1 МБ

# Визуальный разделитель между днями (генерируется на лету — дата внутри)
_SEP_WIDTH = 74


def _make_day_separator(date_str: str) -> str:
    """Создаёт рамку с датой внутри: ╔═══╗ / ║ 📅 YYYY-MM-DD ║ / ╚═══╝."""
    top = "╔" + "═" * _SEP_WIDTH + "╗"
    inner_text = f"  📅  {date_str}"
    padding = max(0, _SEP_WIDTH - len(inner_text))
    middle = "║" + inner_text + " " * padding + "║"
    bottom = "╚" + "═" * _SEP_WIDTH + "╝"
    return f"\n{top}\n{middle}\n{bottom}\n\n"


def _rotate_if_needed() -> None:
    """Ротирует audit.log → audit.log.1, если файл вырос больше лимита."""
    if not os.path.exists(AUDIT_FILE):
        return
    if os.path.getsize(AUDIT_FILE) < AUDIT_MAX_SIZE:
        return
    backup = AUDIT_FILE + ".1"
    try:
        if os.path.exists(backup):
            os.remove(backup)
        os.rename(AUDIT_FILE, backup)
    except Exception as e:
        logger.error(f"Не удалось ротировать audit.log: {e}")


def _get_last_date_from_file() -> Optional[str]:
    """
    Читает последнюю непустую строку audit.log и возвращает её дату (YYYY-MM-DD).

    Читает только хвост файла (~4 КБ), чтобы не тормозить на больших логах.
    Возвращает None, если файла нет, он пуст или дату не удалось распарсить.
    """
    if not os.path.exists(AUDIT_FILE):
        return None
    try:
        with open(AUDIT_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return None
            chunk_size = min(4096, size)
            f.seek(size - chunk_size)
            raw = f.read().decode("utf-8", errors="ignore")

        for line in reversed(raw.splitlines()):
            line = line.strip()
            if not line:
                continue
            if line.startswith("*"):  # строка-разделитель
                continue
            # Дата — первые 10 символов: YYYY-MM-DD
            if len(line) >= 10 and line[4] == "-" and line[7] == "-":
                return line[:10]
            return None
        return None
    except Exception as e:
        logger.debug(f"_get_last_date_from_file: {e}")
        return None


def audit_log(
    action: str,
    tg_id: Optional[int] = None,
    username: str = "",
    details: str = "",
) -> None:
    """
    Записывает одно событие в audit.log.

    :param action: короткое имя действия, например "/addclient" или "addclient.create"
    :param tg_id:  TG ID администратора
    :param username: @username без @ или пустая строка
    :param details: строка с параметрами/деталями
    """
    os.makedirs(AUDIT_DIR, exist_ok=True)
    _rotate_if_needed()

    try:
        ts = datetime.now(ZoneInfo(config.get_timezone())).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    today = ts[:10]
    user_part = f"[{tg_id} @{username}]" if username else f"[{tg_id}]"
    line = f"{ts} {user_part} {action}"
    if details:
        line += f" {details}"
    line += "\n"

    try:
        # Если день сменился — сначала пишем разделитель
        last_date = _get_last_date_from_file()
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            if last_date and last_date != today:
                f.write(_make_day_separator(today))
            f.write(line)
    except Exception as e:
        logger.error(f"Не удалось записать в audit.log: {e}")


def audit_command(action: str):
    """
    Декоратор для хендлеров команд. Логирует вызов команды админом.

    Ставить ВЫШЕ @admin_only / @superadmin_only:
        @audit_command("/addclient")
        @admin_only
        async def addclient_start(...)
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user = update.effective_user
            if user and config.is_admin(user.id):
                args_list = list(context.args) if context and context.args else []
                details = f"args={args_list}" if args_list else ""
                audit_log(
                    action=action,
                    tg_id=user.id,
                    username=user.username or "",
                    details=details,
                )
            return await func(update, context, *args, **kwargs)
        return wrapper
    return decorator