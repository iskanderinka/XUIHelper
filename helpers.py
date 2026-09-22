# =============================================================================
# helpers.py — общие утилиты для main.py и jobs.py
# =============================================================================
#
# Содержит:
#   1. Форматирование (байты, GB)
#   2. Валидация (email, URL панели, даты)
#   3. Клавиатуры (reply, inline confirm)
#   4. Работа с панелями (get_panel_api, check_panel_available)
#   5. Работа со связками (find_bindings_for_admin)
#   6. Работа с датами подписки (parse_expiry, days_to_expiry, days_between)
#   7. Хелперы для подтверждений и редактирования сообщений
#
# =============================================================================

import logging
import re
import string
import secrets
from datetime import datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.ext import ContextTypes

import config
from database import get_user_bindings
from xui_api import XUIApi

logger = logging.getLogger(__name__)


# ---------- Константы ----------

# Допустимые символы email: латиница, цифры, точка, дефис, подчёркивание
EMAIL_PATTERN = re.compile(r'^[a-zA-Z0-9._-]{1,64}$')

# Длина subId (генерируется автоматически)
SUB_ID_LENGTH = 16


# ---------- Часовой пояс ----------

def _tz() -> ZoneInfo:
    """Возвращает ZoneInfo из config.yml (fallback — Asia/Hong_Kong)."""
    return ZoneInfo(config.get_timezone())


# ---------- Форматирование ----------

def _format_bytes(size: int) -> str:
    """Форматирует размер в человекочитаемый вид (1.23 GB и т.п.)."""
    if size is None:
        return "N/A"
    power = 1024
    n = 0
    power_labels = {0: '', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size > power and n < len(power_labels) - 1:
        size /= power
        n += 1
    return f"{size:.2f} {power_labels[n]}B"


def _bytes_to_gb(size: int) -> float:
    """Байты → гигабайты (округлённо до 2 знаков)."""
    return round(size / (1024 ** 3), 2)


# ---------- Генерация subId ----------

def _make_sub_id(length: int = SUB_ID_LENGTH) -> str:
    """Генерирует случайный subId: строчные буквы и цифры."""
    alphabet = string.ascii_lowercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


# ---------- Валидация ----------

def _validate_email(email: str) -> Optional[str]:
    """
    Проверяет email на допустимые символы.

    Возвращает текст ошибки или None, если всё в порядке.
    """
    if not email:
        return "❌ Email не может быть пустым."
    if not EMAIL_PATTERN.match(email):
        return (
            "❌ Email может содержать только латинские буквы, цифры, точку, "
            "дефис и подчёркивание. Длина 1–64 символа. Без пробелов."
        )
    return None


# ---------- Панели ----------

def _get_panel_api(panel_name: str) -> XUIApi:
    """Создаёт клиент XUIApi по имени панели."""
    panel_config = config.get_panel_config(panel_name)
    return XUIApi(
        url=panel_config.get("url", ""),
        username=panel_config.get("username", ""),
        password=panel_config.get("password", ""),
        sub_url=panel_config.get("sub_url", ""),
    )


def _check_panel_available(panel_name: str) -> Optional[str]:
    """
    Проверяет, доступна ли панель для работы.

    Возвращает текст ошибки (str) — если работать нельзя,
             None — если всё в порядке.
    """
    panel_config = config.get_panel_config(panel_name)
    if not panel_config:
        return (
            f"❌ Панель '{panel_name}' не найдена в config.yml. "
            f"Возможно, она была удалена через /delpanel."
        )
    if panel_config.get("disabled", False):
        return f"❌ Панель '{panel_name}' отключена. Включи в config.yml или через /setting."
    return None


# ---------- Работа со связками клиентов ----------

def _find_bindings_for_admin(tg_id: int, email_filter: str = None) -> list:
    """Находит связки клиента, опционально фильтруя по email."""
    bindings = get_user_bindings(tg_id)
    if email_filter:
        bindings = [b for b in bindings if b["email"] == email_filter]
    return bindings


# ---------- Даты подписки ----------

def _days_to_expiry(days: int) -> tuple:
    """
    Возвращает (expiry_ts_ms, expiry_date_str) для now + days.

    Дата берётся в конце дня (23:59:59) по часовому поясу сервиса —
    так клиент не теряет часть последнего оплаченного дня.
    """
    now = datetime.now(_tz())
    target_date = (now + timedelta(days=days)).date()
    target_dt = datetime.combine(target_date, time(23, 59, 59), tzinfo=_tz())
    return int(target_dt.timestamp() * 1000), target_date.strftime("%Y-%m-%d")


def _parse_expiry_input(text: str) -> tuple:
    """
    Парсит текстовый ввод даты окончания.

    Возвращает (expiry_ts_ms, expiry_date_str, error).
      - /skip: (0, None, None) — бессрочно
      - дата YYYY-MM-DD: (ts, 'YYYY-MM-DD', None)
      - ошибка: (None, None, 'сообщение')

    Дата трактуется как конец дня (23:59:59) в часовом поясе сервиса.
    """
    text = text.strip().lower()
    if text in ("/skip", "skip", "пропустить"):
        return 0, None, None

    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None, None, "Не понял формат. Введи дату `ГГГГ-ММ-ДД`, или нажми кнопку, или `/skip`."

    now = datetime.now(_tz())
    if parsed.date() < now.date():
        return None, None, "Дата уже прошла. Введи будущую дату."

    max_date = now + timedelta(days=365 * 10)
    if parsed > max_date:
        return None, None, "Дата слишком далеко (максимум 10 лет вперёд)."

    target_dt = datetime.combine(parsed.date(), time(23, 59, 59), tzinfo=_tz())
    return int(target_dt.timestamp() * 1000), parsed.strftime("%Y-%m-%d"), None


def _days_between(start_str: str, end_str: str) -> int:
    """Сколько дней между двумя строками YYYY-MM-DD. Если что-то не так — 0."""
    try:
        d1 = datetime.strptime(start_str, "%Y-%m-%d").date()
        d2 = datetime.strptime(end_str, "%Y-%m-%d").date()
        return max(0, (d2 - d1).days)
    except (ValueError, TypeError):
        return 0


def _parse_extend_argument(text: str) -> tuple:
    """
    Парсит аргумент для /extendsub.

    Принимает:
      - '+30' — добавить 30 дней к текущей дате окончания
      - '2026-12-31' — установить конкретную дату

    Возвращает (kind, value, error):
      - kind='days', value=int
      - kind='date', value='YYYY-MM-DD'
      - error != None при ошибке
    """
    text = text.strip()
    if text.startswith("+"):
        try:
            days = int(text[1:])
            if days <= 0 or days > 3650:
                return None, None, "Число дней должно быть от 1 до 3650."
            return "days", days, None
        except ValueError:
            return None, None, "После '+' должно идти целое число дней."

    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None, None, "Введи `+N` (дней) или дату `ГГГГ-ММ-ДД`."

    if parsed.date() < datetime.now().date():
        return None, None, "Дата уже прошла."

    return "date", parsed.strftime("%Y-%m-%d"), None


# ---------- Клавиатуры ----------

def _client_reply_keyboard() -> ReplyKeyboardMarkup:
    """
    Reply-клавиатура для клиента с активной подпиской.

    Тексты кнопок — на туркменском (целевая аудитория).
    Остаётся в чате навсегда, пока бот не пришлёт новую или не уберёт.
    """
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔗 Abuna salgysy")],
            [KeyboardButton("📊 Nyrhlar")],
            [KeyboardButton("🆘 Kömek gerek")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _admin_reply_keyboard() -> ReplyKeyboardMarkup:
    """
    Reply-клавиатура для админа и суперадмина.

    Одна и та же для обеих ролей. Остаётся в чате навсегда.
    """
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("➕ Добавить пользователя")],
            [KeyboardButton("✏️ Переименовать"), KeyboardButton("💬 Комментарий")],
            [KeyboardButton("📅 Продлить"), KeyboardButton("🗑️ Удалить")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _confirm_keyboard(token: str) -> InlineKeyboardMarkup:
    """Клавиатура подтверждения: Да / Нет. Токен защищает от нажатия на устаревшую кнопку."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Да", callback_data=f"confirm:yes:{token}"),
            InlineKeyboardButton("❌ Нет", callback_data=f"confirm:no:{token}"),
        ]
    ])


# ---------- Подтверждения и редактирование сообщений ----------

async def _ask_confirm(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    payload: dict,
    preview: str,
) -> None:
    """
    Сохраняет pending action и отправляет превью с кнопками Да/Нет.

    Каждому подтверждению присваивается уникальный токен, чтобы нажатие
    на устаревшее сообщение не выполнило новое действие.

    :param action: 'pause' | 'resume' | 'extend' | 'revoke' | 'addclient'
    :param payload: данные, нужные для выполнения действия
    :param preview: текст превью (Markdown)
    """
    token = secrets.token_hex(4)
    context.user_data['pending'] = {
        "action": action,
        "payload": payload,
        "token": token,
    }
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"⚠️ **Подтверди действие**\n\n{preview}",
        parse_mode='Markdown',
        reply_markup=_confirm_keyboard(token),
    )


def _render_binding_line(panel_name: str, email: str, mark: str, extra: str = "") -> str:
    """Строка отчёта вида: Подписка `user123` — ✅ возобновлена (+30 дн., до 2026-XX-XX)"""
    base = f"Подписка `{email}` — {mark}"
    if extra:
        base += f" {extra}"
    return base


async def _send_client_notice(context: ContextTypes.DEFAULT_TYPE, tg_id: int, text: str) -> None:
    """Отправляет клиенту уведомление с защитой от ошибок."""
    try:
        await context.bot.send_message(chat_id=tg_id, text=text, parse_mode='Markdown')
    except Exception as e:
        logger.warning(f"Не удалось уведомить клиента {tg_id}: {e}")


async def _edit_query_safely(query, text: str) -> None:
    """Редактирует сообщение с защитой от «текст не изменился»."""
    try:
        await query.edit_message_text(text, parse_mode='Markdown')
    except Exception as e:
        logger.debug(f"edit_message_text: {e}")


# ---------- Inline-клавиатура выбора клиента для админских действий ----------

_ADMIN_ACTION_TITLES = {
    "extend": "📅 Продлить — выбери клиента",
    "revoke": "🗑️ Удалить — выбери клиента",
    "rename": "✏️ Переименовать — выбери клиента",
    "comment": "💬 Комментарий — выбери клиента",
}

ADMIN_ACTION_PAGE_SIZE = 5


def _admin_action_keyboard(
    bindings: list,
    action: str,
    page: int,
    page_size: int = ADMIN_ACTION_PAGE_SIZE,
) -> InlineKeyboardMarkup:
    """Inline-клавиатура выбора клиента для админского действия."""
    total = len(bindings)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))

    start = page * page_size
    end = start + page_size
    chunk = bindings[start:end]

    rows = []
    for b in chunk:
        label = f"{b['panel_name']}/{b['email']}"
        # Telegram ограничивает callback_data 64 байтами.
        # panel_name и email не должны содержать ':' — предполагаем это.
        cb = f"admact:sel:{action}:{b['tg_id']}:{b['panel_name']}"
        if len(cb.encode("utf-8")) > 60:
            # Fallback: обрезаем
            label = label[:27] + "…"
        rows.append([InlineKeyboardButton(label, callback_data=cb)])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "◀️", callback_data=f"admact:page:{action}:{page - 1}"
        ))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(
            "▶️", callback_data=f"admact:page:{action}:{page + 1}"
        ))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="admact:cancel")])
    return InlineKeyboardMarkup(rows)


def _render_admin_action_page(
    bindings: list,
    action: str,
    page: int,
    page_size: int = ADMIN_ACTION_PAGE_SIZE,
) -> tuple:
    """Возвращает (text, keyboard) для страницы выбора клиента."""
    total = len(bindings)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))

    start = page * page_size
    end = start + page_size
    chunk = bindings[start:end]

    title = _ADMIN_ACTION_TITLES.get(action, action)
    lines = [f"**{title}** (страница {page + 1}/{total_pages}):\n"]
    for b in chunk:
        expiry = b.get("expiry_date") or "бессрочно"
        status_icon = "⏸️" if b.get("paused_at") else "▶️"
        lines.append(f"{status_icon} `{b['panel_name']}/{b['email']}` — до `{expiry}`")

    text = "\n".join(lines)
    keyboard = _admin_action_keyboard(bindings, action, page, page_size)
    return text, keyboard