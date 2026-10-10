import html
import logging
from urllib.parse import unquote
import uuid as uuid_module
from functools import wraps
from datetime import datetime, timedelta, time
from typing import Optional
from audit import audit_log, audit_command

from telegram import (
    Update, BotCommand,
    InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ConversationHandler,
)

import config
from xui_api import XUIApi, _parse_settings
from database import (
    init_db, upsert_bot_user, is_bot_user,
    save_binding, get_user_bindings, get_binding_by_email,
    delete_binding, list_all_bindings_with_users, list_all_bindings,
    set_binding_paused, update_binding_expiry,
    update_binding_comment, update_binding_limit_hwid,
    update_binding_email, rename_traffic_email,
    update_binding_enabled,
)
from helpers import (
    _tz, _esc,
    _format_bytes, _make_sub_id,
    _validate_email,
    _get_panel_api, _check_panel_available,
    _find_bindings_for_admin,
    _days_to_expiry, _parse_expiry_input, _days_between, _parse_extend_argument,
    _client_reply_keyboard, _admin_reply_keyboard, _ask_confirm,
    _render_binding_line, _send_client_notice, _edit_query_safely,
    _admin_action_keyboard, _render_admin_action_page,
    _parse_broadcast_args,
)
from jobs import (
    record_traffic_job, daily_report_job,
    check_inbounds_job,
    client_expiry_notification_job, admin_expired_notification_job,
    client_expired_notification_job,
    auto_sync_job, sync_all_panels,
    _generate_daily_report_text,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("xui_api").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# =============================================================================
# main.py — точка входа и хендлеры бота
# =============================================================================
#
# СТРУКТУРА ФАЙЛА (поиск по метке в редакторе):
#
#   [1] Импорты и настройки (вверху)
#   [2] Глобальные переменные и абстракции диалогов
#   [3] Декораторы @admin_only, @superadmin_only
#   [4] Базовые команды: /start, /help, /policy
#   [5] Админ-команды панелей: /status, /inbounds, /listpanels, /delpanel
#   [6] Диалог /addclient: addclient_start … addclient_cancel
#   [7] Команды подписок: /pausesub, /resumesub, /extendsub, /revoke, /getlink
#   [8] Клиентские команды: /mylink и reply-кнопки
#   [9] Диалог /setting: setting_start … cancel_setting
#   [10] Обработчики callback: confirm_callback, clients_nav_callback
#   [11] Вспомогательные UI: _expiry_keyboard, _render_clients_page
#   [12] Обработчик ошибок и post_init
#   [13] main() — регистрация хендлеров и запуск
#
# Задачи по расписанию → jobs.py
# Общие утилиты и валидаторы → helpers.py
# =============================================================================


# Ссылки на активные ConversationHandler — чтобы можно было отменять их программно
_conv_setting_ref: Optional[ConversationHandler] = None
_conv_addclient_ref: Optional[ConversationHandler] = None


# Защита от двойного /start (специфика iOS после очистки истории)
_last_start_time: dict = {}  # tg_id -> datetime


def _scheduled_time(hour: int, minute: int = 0) -> time:
    """Создаёт время ежедневной задачи в часовом поясе сервиса."""
    return time(hour=hour, minute=minute, tzinfo=_tz())


# Инициализируем БД при старте
init_db()


def _abort_conversation(update: Update, conv: Optional[ConversationHandler]) -> None:
    """Принудительно завершает ConversationHandler для текущего пользователя."""
    if conv is None:
        return
    try:
        key = conv._get_key(update)
        conv._conversations.pop(key, None)
    except Exception as e:
        logger.warning(f"Не удалось сбросить состояние диалога: {e}")


def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if not config.is_admin(user_id):
            await update.message.reply_text("Извините, эта команда доступна только администраторам.")
            return
        if not config.has_superadmin():
            await update.message.reply_text(
                "⚠️ Суперадмин не назначен.\n\n"
                "Зайди на сервер и добавь ID суперадмина первым в "
                "`users.admin_users` файла `config.yml`. После этого "
                "команды станут доступны."
            )
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


def superadmin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if not config.is_admin(user_id):
            await update.message.reply_text("Извините, эта команда доступна только администраторам.")
            return
        if not config.has_superadmin():
            await update.message.reply_text(
                "⚠️ Суперадмин не назначен.\n\n"
                "Зайди на сервер и добавь ID суперадмина первым в "
                "`users.admin_users` файла `config.yml`."
            )
            return
        if not config.is_superadmin(user_id):
            await update.message.reply_text(
                "🔒 Эта команда доступна только главному администратору "
                "(первому в списке `admin_users`)."
            )
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


# --- Базовые команды ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    # Защита от двойного /start (iOS)
    now = datetime.now()
    last = _last_start_time.get(user.id)
    if last and (now - last).total_seconds() < 2:
        logger.debug(f"Пропускаю повторный /start от {user.id}")
        return
    _last_start_time[user.id] = now

    try:
        upsert_bot_user(user.id, user.username or "", user.first_name or "")
    except Exception as e:
        logger.error(f"Не удалось записать bot_user {user.id}: {e}")

    # Админ — своя ветка
    if config.is_admin(user.id):
        await update.message.reply_html(
            rf"Привет, {user.mention_html()}! "
            f"Твой Telegram ID: <code>{user.id}</code>\n\n"
            f"Ты администратор. Используй /help для списка команд."
        )
        await update.message.reply_text(
            "👇 Быстрый доступ:",
            reply_markup=_admin_reply_keyboard(),
        )
        return

    # Клиент / неавторизованный — приветствие с inline-кнопкой "Отправить заявку"
    apply_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Arza ibermek", callback_data="apply:submit")]
    ])
    await update.message.reply_html(
        rf"Salam, {user.mention_html()}! "
        f"Seniň Telegram ID: <code>{user.id}</code>\n\n"
        f"Eger sen müşderi bolsaň — administrator saňa abuna berer, ol awtomatik şu çata gelýär.\n"
        f"Buýruklaryň sanawy üçin /help ulanyp bilersiň.",
        reply_markup=apply_kb,
    )

    # Если у клиента уже есть подписка — покажем reply-клавиатуру
    try:
        bindings = get_user_bindings(user.id)
        if bindings:
            await update.message.reply_text(
                "👇 Abuna üçin çalt düwmeler:",
                reply_markup=_client_reply_keyboard(),
            )
    except Exception as e:
        logger.error(f"Не удалось показать клавиатуру {user.id}: {e}")


async def apply_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка кнопки «Отправить заявку» в приветствии."""
    query = update.callback_query
    await query.answer()

    user = query.from_user
    full_name = user.full_name or "—"
    username = f"@{user.username}" if user.username else "—"
    user_id = user.id

    # Убираем кнопку, приветствие остаётся
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as e:
        logger.warning(f"Не удалось убрать inline-кнопку: {e}")

    # Уведомляем всех админов
    notif = (
        f"🆕 <b>Новая заявка</b> 🆕\n\n"
        f"👤 {html.escape(full_name)}\n"
        f"🆔 <code>{user_id}</code>\n"
        f"🐶 <code>{html.escape(username)}</code>\n\n"
        f"<i>Важно: создавай клиента лишь тем, кто прошёл через тебя. "
        f"Все заявки, которые ты не ждёшь, игнорируй!</i>"
    )
    for uid in config.get_admin_users():
        try:
            await context.bot.send_message(chat_id=uid, text=notif, parse_mode='HTML')
        except Exception as e:
            logger.error(f"Не удалось уведомить админа {uid}: {e}")


# ---------- Reply-кнопки админской клавиатуры ----------

async def btn_admin_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «➕ Добавить пользователя» — подсказка по /addclient."""
    await update.message.reply_text(
        "➕ <b>Чтобы добавить клиента</b>, введи команду:\n\n"
        "<code>/addclient &#60;tg_id&#62; &#60;email&#62; &#60;панель&#62; &#60;id1&#62; [id2] ...</code>\n\n"
        "<b>Пример:</b>\n"
        "<code>/addclient 123456789 ivan TMT 8 9</code>\n\n"
        "Список панелей: <code>/listpanels</code>\n"
        "Список инбаундов: <code>/inbounds TMT</code>",
        parse_mode='HTML',
    )


async def _open_admin_action_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
) -> None:
    """Открывает список клиентов для админского действия.

    Список сохраняется в user_data, чтобы по клику на кнопку
    взять связку по индексу, а не переискивать все.
    """
    bindings = list_all_bindings_with_users()
    if not bindings:
        await update.message.reply_text("Пока нет ни одного выданного клиента.")
        return

    # Сохраняем список — он нужен для callback-обработчика
    context.user_data["admact_list"] = bindings
    context.user_data["admact_action"] = action

    text, keyboard = _render_admin_action_page(bindings, action, 0)
    await update.message.reply_text(text, parse_mode='HTML', reply_markup=keyboard)


async def btn_admin_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «✏️ Переименовать» — открывает список клиентов."""
    await _open_admin_action_list(update, context, "rename")


async def btn_admin_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «💬 Комментарий» — открывает список клиентов."""
    await _open_admin_action_list(update, context, "comment")


async def btn_admin_extend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «📅 Продлить» — открывает список клиентов."""
    await _open_admin_action_list(update, context, "extend")


async def btn_admin_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «🗑️ Удалить» — открывает список клиентов."""
    await _open_admin_action_list(update, context, "revoke")


async def admin_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка навигации и выбора в админском списке клиентов."""
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    parts = data.split(":")

    # admact:cancel — удаляем сообщение со списком
    if data == "admact:cancel":
        try:
            await query.message.delete()
        except Exception:
            pass
        context.user_data.pop("admact_list", None)
        context.user_data.pop("admact_action", None)
        return

    # admact:page:<action>:<page> — навигация по страницам
    if len(parts) == 4 and parts[1] == "page":
        action = parts[2]
        try:
            page = int(parts[3])
        except ValueError:
            return

        saved = context.user_data.get("admact_list")
        if not saved:
            await query.edit_message_text(
                "⚠️ Список устарел (бот перезапускался). Открой заново через кнопку."
            )
            return

        text, keyboard = _render_admin_action_page(saved, action, page)
        try:
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=keyboard)
        except Exception as e:
            logger.debug(f"edit_message_text: {e}")
        return

    # admact:sel:<action>:<index> — выбор клиента по индексу
    if len(parts) == 4 and parts[1] == "sel":
        action = parts[2]
        try:
            index = int(parts[3])
        except ValueError:
            return

        saved = context.user_data.get("admact_list")
        if not saved or index < 0 or index >= len(saved):
            await query.edit_message_text(
                "⚠️ Список устарел (бот перезапускался). Открой заново через кнопку."
            )
            context.user_data.pop("admact_list", None)
            context.user_data.pop("admact_action", None)
            return

        b = saved[index]
        tg_id = b["tg_id"]
        panel_name = b["panel_name"]
        email = b["email"]

        # Всё дальше — работа с ОДНОЙ связкой
        if action == "pause":
            status = "уже на паузе" if b.get("paused_at") else "будет приостановлена"
            preview = (
                f"Поставить на паузу клиента `{tg_id}`:\n\n"
                f"Подписка `{email}` — {status}"
            )
            await _ask_confirm(
                query.message.chat_id, context, "pause",
                {"tg_id": tg_id, "bindings": [b]}, preview,
            )

        elif action == "resume":
            line = (
                f"Подписка `{email}` — будет возобновлена"
                if b.get("paused_at") else
                f"Подписка `{email}` — не на паузе"
            )
            preview = f"Возобновить подписку клиента `{tg_id}`:\n\n{line}"
            await _ask_confirm(
                query.message.chat_id, context, "resume",
                {"tg_id": tg_id, "bindings": [b]}, preview,
            )

        elif action == "extend":
            old = b.get("expiry_date") or "бессрочно"
            await query.edit_message_text(
                f"📅 <b>Продлить подписку</b>\n\n"
                f"Клиент: <code>{tg_id}</code>, подписка <code>{_esc(email)}</code> (сейчас: {_esc(old)})\n\n"
                f"Отправь в чат команду:\n"
                f"<code>/extendsub {tg_id} +30 {_esc(email)}</code>\n\n"
                f"Или с конкретной датой:\n"
                f"<code>/extendsub {tg_id} 2027-01-01 {_esc(email)}</code>",
                parse_mode='HTML',
            )

        elif action == "revoke":
            preview = (
                f"**Удалить клиента** `{tg_id}`:\n\n"
                f"Подписка `{email}`\n\n"
                f"⚠️ Клиент будет **удалён из панели** и БД."
            )
            await _ask_confirm(
                query.message.chat_id, context, "revoke",
                {"tg_id": tg_id, "bindings": [b]}, preview,
            )

        elif action == "rename":
            context.user_data["pending_input"] = {
                "action": "rename",
                "tg_id": tg_id,
                "panel_name": panel_name,
                "email": email,
                "prompt_msg_id": query.message.message_id,
            }
            await query.edit_message_text(
                f"✏️ <b>Переименовать подписку</b>\n\n"
                f"Клиент: <code>{tg_id}</code>\n"
                f"Текущий email: <code>{_esc(email)}</code>\n\n"
                f"<b>Введи новый email</b> в ответ на это сообщение.\n"
                f"Только латиница, цифры, точка, дефис, подчёркивание.\n\n"
                f"Отмена: <code>/cancel_input</code>",
                parse_mode='HTML',
            )

        elif action == "comment":
            context.user_data["pending_input"] = {
                "action": "comment",
                "tg_id": tg_id,
                "panel_name": panel_name,
                "email": email,
                "prompt_msg_id": query.message.message_id,
            }
            await query.edit_message_text(
                f"💬 <b>Изменить комментарий</b>\n\n"
                f"Клиент: <code>{tg_id}</code>, подписка <code>{_esc(email)}</code>\n\n"
                f"<b>Введи новый комментарий</b> в ответ на это сообщение.\n"
                f"Чтобы очистить — отправь <code>-</code>.\n\n"
                f"Отмена: <code>/cancel_input</code>",
                parse_mode='HTML',
            )

        else:
            await query.edit_message_text(f"❓ Неизвестное действие: {action}")
        return

    logger.warning(f"Неизвестный admin_action callback: {data}")


async def pending_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[bool]:
    """Ловит текст, когда админ вводит новый email или комментарий после кнопки."""
    pending = context.user_data.get("pending_input")
    if not pending:
        # Нет ожидающего ввода — пропускаем апдейт дальше по цепочке
        return False

    text = (update.message.text or "").strip()
    action = pending["action"]
    tg_id = pending["tg_id"]
    panel_name = pending["panel_name"]
    old_email = pending["email"]
    chat_id = update.effective_chat.id

    admin_id = update.effective_user.id
    admin_username = update.effective_user.username or ""

    # Удаляем сообщение с вводом, чтобы не мусорить
    try:
        await update.message.delete()
    except Exception:
        pass

    # Удаляем подсказку (тот edit_message_text от admin_action_callback)
    prompt_msg_id = pending.get("prompt_msg_id")
    if prompt_msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=prompt_msg_id)
        except Exception:
            pass

    context.user_data.pop("pending_input", None)

    # Проверка панели
    err = _check_panel_available(panel_name)
    if err:
        audit_log(action, admin_id, admin_username,
                  f"FAIL reason=\"панель недоступна\" tg_id={tg_id} email={old_email} panel={panel_name}")
        await context.bot.send_message(chat_id=chat_id, text=err)
        return

    if action == "rename":
        err = _validate_email(text)
        if err:
            audit_log("rename", admin_id, admin_username,
                      f"FAIL reason=\"email невалиден\" tg_id={tg_id} old={old_email}")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ {_esc(err)}\nНачни заново через кнопку «✏️ Переименовать»."
            )
            return
        new_email = text

        if get_binding_by_email(panel_name, new_email):
            audit_log("rename", admin_id, admin_username,
                      f"FAIL reason=\"email занят в БД\" old={old_email} new={new_email} panel={panel_name}")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Email <code>{_esc(new_email)}</code> уже занят в БД на '{_esc(panel_name)}'.",
                parse_mode='HTML',
            )
            return

        result = None
        try:
            async with _get_panel_api(panel_name) as api:
                await api.login()
                existing = await api.get_client_object(new_email)
                if existing is not None:
                    audit_log("rename", admin_id, admin_username,
                              f"FAIL reason=\"email занят в панели\" old={old_email} new={new_email} panel={panel_name}")
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"❌ Клиент с email <code>{_esc(new_email)}</code> уже есть в панели.",
                        parse_mode='HTML',
                    )
                    return
                result = await api.update_client(old_email, new_email=new_email)
        except Exception as e:
            logger.error(f"[admin={admin_id}] Ошибка rename '{panel_name}': {e}")

        if result is not True:
            audit_log("rename", admin_id, admin_username,
                      f"FAIL reason=\"панель отказала\" old={old_email} new={new_email} panel={panel_name}")
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"❌ Не удалось переименовать.\n"
                    f"Возможно, клиента <code>{_esc(old_email)}</code> нет в панели.\n"
                    f"Проверь через <code>/sync</code>."
                ),
                parse_mode='HTML',
            )
            return

        db_ok = update_binding_email(tg_id, panel_name, old_email, new_email)
        if not db_ok:
            audit_log("rename", admin_id, admin_username,
                      f"PARTIAL reason=\"панель ок, БД ошибка\" old={old_email} new={new_email} panel={panel_name}")
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Панель обновлена, но в БД ошибка. Запусти <code>/sync</code>.",
                parse_mode='HTML',
            )
            return

        renamed = rename_traffic_email(panel_name, old_email, new_email)
        audit_log("rename", admin_id, admin_username,
                  f"OK tg_id={tg_id} old={old_email} new={new_email} panel={panel_name} traffic_renamed={renamed}")
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ <b>Email изменён</b>\n\n"
                f"Было: <code>{_esc(old_email)}</code>\n"
                f"Стало: <code>{_esc(new_email)}</code>\n"
                f"Панель: <code>{_esc(panel_name)}</code>\n"
                f"Записей трафика обновлено: {renamed}"
            ),
            parse_mode='HTML',
        )

    elif action == "comment":
        new_comment = "" if text == "-" else text

        result = None
        try:
            async with _get_panel_api(panel_name) as api:
                await api.login()
                result = await api.update_client(old_email, comment=new_comment)
        except Exception as e:
            logger.error(f"[admin={admin_id}] Ошибка setcomment '{panel_name}/{old_email}': {e}")

        if result is not True:
            audit_log("comment", admin_id, admin_username,
                      f"FAIL reason=\"панель отказала\" tg_id={tg_id} email={old_email} panel={panel_name}")
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"❌ Не удалось обновить комментарий.\n"
                    f"Возможно, клиента <code>{_esc(old_email)}</code> нет в панели."
                ),
                parse_mode='HTML',
            )
            return

        try:
            update_binding_comment(tg_id, panel_name, old_email, new_comment)
        except Exception as e:
            logger.error(f"Не удалось обновить комментарий в БД: {e}")

        audit_log("comment", admin_id, admin_username,
                  f"OK tg_id={tg_id} email={old_email} panel={panel_name}")

        shown = f"<code>{_esc(new_comment)}</code>" if new_comment else "<i>(пустой)</i>"
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ Комментарий обновлён.\n\n"
                f"Панель: <code>{_esc(panel_name)}</code>\n"
                f"Клиент: <code>{_esc(old_email)}</code>\n"
                f"Комментарий: {shown}"
            ),
            parse_mode='HTML',
        )


async def cancel_input_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отменяет ожидание ввода после кнопок переименования/комментария."""
    if context.user_data.pop("pending_input", None):
        await update.message.reply_text("Отменено.")
    else:
        await update.message.reply_text("Нечего отменять.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    policy_line = "<code>/policy</code> - 🔒 Gizlinlik syýasaty\n" if config.get_policy_url() else ""
    user_id = update.effective_user.id

    if config.is_admin(user_id):
        common = (
            "<b>✨ Команды администратора:</b>\n"
            "<code>/start</code> - 🚀 Начать работу с ботом\n"
            "<code>/help</code> - ℹ️ Показать эту справку\n"
            f"{policy_line}"
            "<code>/guideadmin</code> - 📚 Гайд для администратора\n"
            "<code>/listpanels</code> - 📋 Список панелей со статусом\n"
            "<code>/status &#60;панель&#62;</code> - 📊 Подробный статус панели\n"
            "<code>/inbounds &#60;панель&#62;</code> - 📡 Список инбаундов с ID\n"
            "<code>/addclient &#60;tg_id&#62; &#60;email&#62; &#60;панель&#62; &#60;id1&#62; [id2] ...</code> - 🆕 Создать клиента\n"
            "<code>/revoke &#60;tg_id&#62; [email]</code> - 🗑️ Удалить клиента\n"
            "<code>/pausesub &#60;tg_id&#62; [email]</code> - ⏸️ Приостановить подписку\n"
            "<code>/resumesub &#60;tg_id&#62; [email]</code> - ▶️ Возобновить подписку\n"
            "<code>/extendsub &#60;tg_id&#62; &#60;+N | дата&#62; [email]</code> - 📅 Продлить подписку\n"
            "<code>/broadcast [tg_id] &#60;текст&#62;</code> - 📢 Рассылка пользователям\n"
            "<code>/listclients</code> - 📋 Список выданных клиентов\n"
            "<code>/getlink &#60;tg_id&#62; [email]</code> - 🔗 Получить sub-ссылку клиента\n"
            "<code>/setcomment &#60;tg_id&#62; &#60;email&#62; &#60;текст&#62;</code> - 💬 Изменить комментарий\n"
            "<code>/rename &#60;tg_id&#62; &#60;старый&#62; &#60;новый&#62;</code> - ✏️ Изменить email клиента\n"
            "<code>/report</code> - 📈 Отправить дневной отчёт сейчас"
        )

        if config.is_superadmin(user_id):
            common += (
                "\n\n<b>🔐 Только суперадмин:</b>\n"
                "<code>/guidesuper</code> - 📚 Гайд для суперадминистратора\n"
                "<code>/setting</code> - ⚙️ Добавить или обновить панель\n"
                "<code>/delpanel &#60;имя&#62;</code> - 🗑️ Удалить панель\n"
                "<code>/sync</code> - 🔄 Синхронизировать БД с панелью"
            )

        help_text = common
    else:
        help_text = (
            "<b>👋 Müşderi buýruklary:</b>\n"
            "<code>/start</code> - 🚀 Boty başlamak\n"
            "<code>/help</code> - ℹ️ Şu gollanmany görkezmek\n"
            f"{policy_line}"
            "<code>/guide</code> - 📚 Bot boýunça gollanma\n"
            "<code>/mylink</code> - 🔗 Abuna salgysyny almak"
        )

    await update.message.reply_text(help_text, parse_mode='HTML')

    if config.is_admin(user_id):
        await update.message.reply_text(
            "👇 Быстрый доступ:",
            reply_markup=_admin_reply_keyboard(),
        )


# --- Админские команды по панелям ---
@audit_command("/status")
@admin_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Подробный статус одной панели. Без аргумента — подсказка."""
    if not context.args:
        await update.message.reply_text(
            "Укажи панель: <code>/status &#60;имя&#62;</code>\n"
            "Обзор всех панелей: <code>/listpanels</code>",
            parse_mode='HTML',
        )
        return

    panel_name = context.args[0]
    panel_config = config.get_panel_config(panel_name)
    if not panel_config:
        await update.message.reply_text(f"Панель с именем '{panel_name}' не найдена.")
        return

    await update.message.reply_text(f"Получаю статус сервера '{panel_name}', подождите...")

    async with _get_panel_api(panel_name) as api:
        status = await api.get_server_status()
    if status and 'cpu' in status and 'mem' in status and 'disk' in status:
        cpu_percent = status.get('cpu', 0)
        mem = status.get('mem', {})
        mem_current = mem.get('current', 0)
        mem_total = mem.get('total', 0)
        mem_percent = (mem_current / mem_total * 100) if mem_total > 0 else 0
        disk = status.get('disk', {})
        disk_current = disk.get('current', 0)
        disk_total = disk.get('total', 0)
        disk_percent = (disk_current / disk_total * 100) if disk_total > 0 else 0
        net_traffic = status.get('netTraffic', {})
        net_sent = net_traffic.get('sent', 0)
        net_recv = net_traffic.get('recv', 0)
        uptime_seconds = status.get('uptime', 0)
        uptime_delta = timedelta(seconds=uptime_seconds)
        days = uptime_delta.days
        hours, rem = divmod(uptime_delta.seconds, 3600)
        minutes, _ = divmod(rem, 60)
        uptime_str = f"{days} д. {hours} ч. {minutes} мин."
        xray = status.get('xray', {})
        xray_status = xray.get('state', 'N/A')
        xray_version = xray.get('version', 'N/A')

        status_text = (
            f"<b>Статус панели {_esc(panel_name)}</b>\n"
            f"- Версия Xray: <code>{_esc(xray_version)}</code>\n"
            f"- Статус Xray: <b>{_esc(xray_status.capitalize())}</b>\n\n"
            f"<b>Состояние сервера</b>\n"
            f"- CPU: {cpu_percent:.2f}%\n"
            f"- Память: {_format_bytes(mem_current)} / {_format_bytes(mem_total)} ({mem_percent:.2f}%)\n"
            f"- Диск: {_format_bytes(disk_current)} / {_format_bytes(disk_total)} ({disk_percent:.2f}%)\n"
            f"- Время работы: {uptime_str}\n"
            f"<b>Сеть</b>\n"
            f"- Отдано: {_format_bytes(net_sent)}\n"
            f"- Принято: {_format_bytes(net_recv)}"
        )
        await update.message.reply_text(status_text, parse_mode='HTML')
    else:
        await update.message.reply_text(f"Не удалось получить полный статус '{panel_name}'. Проверьте подключение или повторите позже.")


@audit_command("/inbounds")
@admin_only
async def inbounds_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает список инбаундов панели с их ID."""
    if not context.args:
        await update.message.reply_text("Формат: /inbounds <имя панели>")
        return

    panel_name = context.args[0]
    err = _check_panel_available(panel_name)
    if err:
        await update.message.reply_text(err)
        return

    await update.message.reply_text(f"Загружаю инбаунды панели '{panel_name}'...")

    async with _get_panel_api(panel_name) as api:
        ok = await api.login()
        if not ok:
            await update.message.reply_text("❌ Не удалось подключиться к панели.")
            return
        inbounds = await api.get_inbounds_list()

    if not inbounds:
        await update.message.reply_text("Инбаунды не найдены.")
        return

    lines = [f"<b>Инбаунды панели '{_esc(panel_name)}':</b>\n"]
    for ib in inbounds:
        status = "✅" if ib.get("enable") else "⛔"
        remark = _esc(str(ib.get("remark") or "без имени"))
        protocol = _esc(str(ib.get("protocol") or "?"))
        lines.append(
            f"{status} ID <code>{ib['id']}</code> — {remark} "
            f"({protocol}:{ib.get('port', '?')})"
        )
    lines.append("\nИспользуй ID в команде <code>/addclient</code>.")
    await update.message.reply_text("\n".join(lines), parse_mode='HTML')


@audit_command("/listpanels")
@admin_only
async def listpanels_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Список всех панелей с их текущим статусом."""
    all_panels = config.get_all_panels()
    if not all_panels:
        await update.message.reply_text("Панели не настроены.")
        return

    await update.message.reply_text(f"Проверяю {len(all_panels)} панель(ей)...")

    lines = ["<b>Список панелей:</b>\n"]
    for name, pconf in all_panels.items():
        lines.append(f"\n<b>{_esc(name)}</b> — <code>{_esc(pconf['url'])}</code>")

        if pconf.get("disabled", False):
            lines.append("  ⛔ <code>отключена</code>")
            continue

        try:
            async with _get_panel_api(name) as api:
                status = await api.get_server_status()
            if status and 'xray' in status:
                xray_state = status['xray'].get('state', 'N/A')
                lines.append(f"  Xray: <b>{_esc(xray_state.capitalize())}</b>")
            else:
                lines.append("  Xray: <code>не удалось подключиться</code>")
        except Exception as e:
            logger.warning(f"Ошибка получения статуса '{name}': {e}")
            lines.append("  Xray: <code>ошибка запроса</code>")

    await update.message.reply_text("\n".join(lines), parse_mode='HTML')


@audit_command("/delpanel")
@superadmin_only
async def delpanel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Формат: /delpanel <имя панели>")
        return
    panel_name = context.args[0]
    admin_id = update.effective_user.id
    admin_username = update.effective_user.username or ""
    if config.delete_panel(panel_name):
        audit_log("delpanel", admin_id, admin_username, f"OK panel={panel_name}")
        await update.message.reply_text(f"🗑️ Панель '{panel_name}' успешно удалена.")
    else:
        audit_log("delpanel", admin_id, admin_username, f"FAIL reason=\"панель не найдена\" panel={panel_name}")
        await update.message.reply_text(f"Панель '{panel_name}' не найдена.")


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка нажатия Да / Нет с проверкой токена."""
    query = update.callback_query
    await query.answer()

    # Разбираем callback_data: confirm:yes:token или confirm:no:token
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "confirm":
        return
    answer, token = parts[1], parts[2]

    pending = context.user_data.get('pending')
    if not pending:
        await query.edit_message_text("⏱️ Подтверждение устарело. Начни заново.")
        return

    # Сверяем токен — защита от нажатия на старую кнопку
    if pending.get("token") != token:
        await query.edit_message_text(
            "⚠️ Это подтверждение устарело — его перекрыло новое действие.\n"
            "Посмотри последнее сообщение с кнопками и нажми там."
        )
        return

    # Токен совпал — забираем pending
    context.user_data.pop('pending', None)

    if answer == "no":
        await query.edit_message_text("❌ Действие отменено.")
        return

    # "Да" — выполняем
    action = pending["action"]
    payload = pending["payload"]

    if action == "pause":
        await _do_pause(update, context, payload, query)
    elif action == "resume":
        await _do_resume(update, context, payload, query)
    elif action == "extend":
        await _do_extend(update, context, payload, query)
    elif action == "revoke":
        await _do_revoke(update, context, payload, query)
    elif action == "addclient":
        await _do_addclient(update, context, payload, query)
    elif action == "broadcast":
        await _do_broadcast(update, context, payload, query)
    else:
        await query.edit_message_text(f"❓ Неизвестное действие: {action}")


async def clients_nav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Навигация по страницам /listclients и удаление сообщения по «Готово»."""
    query = update.callback_query
    await query.answer()

    data = query.data or ""

    if data == "clients:done":
        # Удаляем сообщение со списком (к которому привязаны кнопки)
        try:
            await query.message.delete()
        except Exception as e:
            logger.warning(f"Не удалось удалить список клиентов: {e}")

        # Удаляем исходную команду /listclients
        cmd_msg_id = context.user_data.pop('clients_cmd_msg_id', None)
        if cmd_msg_id:
            try:
                await context.bot.delete_message(
                    chat_id=query.message.chat_id,
                    message_id=cmd_msg_id,
                )
            except Exception as e:
                logger.warning(f"Не удалось удалить команду /listclients: {e}")
        return

    if not data.startswith("clients:page:"):
        return

    try:
        page = int(data.split(":")[2])
    except (IndexError, ValueError):
        return

    bindings = list_all_bindings_with_users()
    if not bindings:
        await query.edit_message_text("Пока нет ни одного выданного клиента.")
        return

    text, keyboard = _render_clients_page(bindings, page)
    try:
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=keyboard)
    except Exception as e:
        logger.debug(f"edit_message_text: {e}")


def _expiry_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора срока подписки."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("7 дней", callback_data="exp:7"),
            InlineKeyboardButton("14 дней", callback_data="exp:14"),
        ],
        [
            InlineKeyboardButton("1 месяц", callback_data="exp:30"),
            InlineKeyboardButton("6 месяцев", callback_data="exp:180"),
        ],
        [
            InlineKeyboardButton("1 год", callback_data="exp:365"),
        ],
    ])


async def _ask_expiry(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Задаёт вопрос о дате окончания с inline-клавиатурой."""
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "📅 <b>До какого числа подписка?</b>\n\n"
            "Нажми кнопку, или введи дату в формате <code>ГГГГ-ММ-ДД</code>.\n"
            "<code>/skip</code> — бессрочная подписка."
        ),
        parse_mode='HTML',
        reply_markup=_expiry_keyboard(),
    )


async def _ask_comment(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Задаёт вопрос о комментарии к клиенту."""
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "💬 <b>Комментарий к клиенту</b>\n\n"
            "Напиши что-нибудь для себя и других админов "
            "(например: «Иван с работы», «Друг Миши»).\n"
            "Клиент этот текст не увидит.\n\n"
            "Или <code>/skip</code>, чтобы пропустить."
        ),
        parse_mode='HTML',
    )

# --- /addclient ---
AC_HWID = 1
AC_EXPIRY = 2
AC_COMMENT = 3


@audit_command("/addclient")
@admin_only
async def addclient_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Отменяем параллельный диалог /setting
    _abort_conversation(update, _conv_setting_ref)
    # Чистим его данные
    for key in ('panel_name', 'panel_url', 'panel_username',
                'panel_password', 'prompt_msg_id'):
        context.user_data.pop(key, None)
    # Чистим свои данные от предыдущей попытки
    context.user_data.pop('ac', None)

    args = context.args
    if len(args) < 4:
        await update.message.reply_text(
            "Формат: /addclient <tg_id> <email> <панель> <inbound_id1> [inbound_id2] ...\n"
            "Пример: /addclient 123456789 user123 TMT 8 9"
        )
        return ConversationHandler.END

    # Парсим TG ID
    try:
        tg_id = int(args[0])
        if tg_id <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("TG ID должен быть положительным числом.")
        return ConversationHandler.END

    email = args[1].strip()
    panel_name = args[2].strip()

    err = _validate_email(email)
    if err:
        await update.message.reply_text(err)
        return ConversationHandler.END

    # Парсим ID инбаундов
    try:
        inbound_ids = [int(x) for x in args[3:]]
    except ValueError:
        await update.message.reply_text("ID инбаундов должны быть числами.")
        return ConversationHandler.END

    if len(inbound_ids) > 10:
        await update.message.reply_text(
            f"❌ Слишком много инбаундов (максимум 10). У тебя: {len(inbound_ids)}."
        )
        return ConversationHandler.END

    # Проверки
    if not is_bot_user(tg_id):
        await update.message.reply_text(
            f"❌ Пользователь {tg_id} не запускал бота.\n"
            f"Попроси его нажать /start."
        )
        return ConversationHandler.END

    err = _check_panel_available(panel_name)
    if err:
        await update.message.reply_text(err)
        return ConversationHandler.END

    panel_config = config.get_panel_config(panel_name)

    # Без sub_url клиент не получит ссылку — отказываемся на старте
    sub_url = (panel_config.get("sub_url") or "").strip()
    if not sub_url:
        await update.message.reply_text(
            f"❌ У панели '{panel_name}' не настроен sub_url.\n\n"
            f"Настрой его через /setting или в config.yml, потом повтори /addclient."
        )
        return ConversationHandler.END

    if get_binding_by_email(panel_name, email):
        await update.message.reply_text(
            f"❌ Клиент с email '{email}' уже существует в базе на панели '{panel_name}'."
        )
        return ConversationHandler.END

    # Проверка через API: логин, отсутствие клиента, существование инбаундов
    async with _get_panel_api(panel_name) as api:
        ok = await api.login()
        if not ok:
            await update.message.reply_text("❌ Не удалось подключиться к панели.")
            return ConversationHandler.END

        # Клиент может существовать в панели, даже если его нет в БД
        # (например, создан вручную через UI). Проверяем оба источника.
        existing = await api.get_client_object(email)
        if existing:
            await update.message.reply_text(
                f"❌ Клиент с email '{email}' уже существует в панели '{panel_name}'.\n"
                f"Если это ошибка — удали его через /revoke или в UI панели."
            )
            return ConversationHandler.END

        available = await api.get_inbounds_list()

    available_ids = {ib["id"] for ib in available}
    missing = [i for i in inbound_ids if i not in available_ids]
    if missing:
        await update.message.reply_text(
            f"❌ Инбаунды {missing} не найдены на панели '{panel_name}'.\n"
            f"Доступные ID: {sorted(available_ids)}\n"
            f"Посмотреть список: /inbounds {panel_name}"
        )
        return ConversationHandler.END

    # Сохраняем промежуточные данные
    context.user_data['ac'] = {
        "tg_id": tg_id,
        "email": email,
        "panel_name": panel_name,
        "inbound_ids": inbound_ids,
    }

    await update.message.reply_text(
        f"Создаю клиента:\n"
        f"- TG ID: <code>{tg_id}</code>\n"
        f"- Email: <code>{_esc(email)}</code>\n"
        f"- Панель: <code>{_esc(panel_name)}</code>\n"
        f"- Инбаунды: <code>{inbound_ids}</code>\n\n"
        f"Введи лимит HWID (0–10, где 0 = безлимит), или <code>/skip</code>:",
        parse_mode='HTML',
    )
    return AC_HWID


async def addclient_hwid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг ввода HWID-лимита. После — спрашивает дату окончания."""
    text = update.message.text.strip()
    try:
        hwid = int(text)
    except ValueError:
        await update.message.reply_text("Нужно целое число от 0 до 10, или /skip.")
        return AC_HWID

    if hwid < 0 or hwid > 11:
        await update.message.reply_text("Лимит HWID — от 0 до 10 (0 = безлимит).")
        return AC_HWID

    if "ac" not in context.user_data:
        await update.message.reply_text("Сессия потеряна. Начни заново /addclient.")
        return ConversationHandler.END

    context.user_data["ac"]["hwid"] = hwid
    await update.message.reply_text(f"HWID лимит: <code>{hwid}</code>", parse_mode='HTML')
    await _ask_expiry(update.effective_chat.id, context)
    return AC_EXPIRY


async def addclient_hwid_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка /skip на шаге HWID — лимит 0, переход к дате."""
    if "ac" not in context.user_data:
        await update.message.reply_text("Сессия потеряна. Начни заново /addclient.")
        return ConversationHandler.END
    context.user_data["ac"]["hwid"] = 0
    await update.message.reply_text("HWID лимит: <code>0</code> (безлимит)", parse_mode='HTML')
    await _ask_expiry(update.effective_chat.id, context)
    return AC_EXPIRY


async def addclient_expiry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка нажатия inline-кнопки выбора срока (7/14/30/180/365 дней)."""
    query = update.callback_query
    await query.answer()
    try:
        days = int(query.data.split(":")[1])
    except (IndexError, ValueError):
        await query.edit_message_text("Ошибка выбора. Начни заново /addclient.")
        context.user_data.pop("ac", None)
        return ConversationHandler.END

    expiry_ts, expiry_date_str = _days_to_expiry(days)
    context.user_data["ac"]["expiry_ts"] = expiry_ts
    context.user_data["ac"]["expiry_date_str"] = expiry_date_str

    await query.edit_message_text(
        f"⏳ Срок: до <b>{_esc(expiry_date_str)}</b> ({days} дн.)",
        parse_mode='HTML',
    )
    await _ask_comment(update.effective_chat.id, context)
    return AC_COMMENT


async def addclient_expiry_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка текстового ввода даты (ГГГГ-ММ-ДД)."""
    text = update.message.text.strip()
    expiry_ts, expiry_date_str, error = _parse_expiry_input(text)
    if error:
        await update.message.reply_text(f"❌ {error}")
        return AC_EXPIRY

    context.user_data["ac"]["expiry_ts"] = expiry_ts
    context.user_data["ac"]["expiry_date_str"] = expiry_date_str

    if expiry_date_str:
        await update.message.reply_text(f"⏳ Срок: до <b>{_esc(expiry_date_str)}</b>", parse_mode='HTML')
    else:
        await update.message.reply_text("⏳ Срок: <b>бессрочно</b>", parse_mode='HTML')

    await _ask_comment(update.effective_chat.id, context)
    return AC_COMMENT


async def addclient_expiry_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка /skip на шаге даты — бессрочно."""
    context.user_data["ac"]["expiry_ts"] = 0
    context.user_data["ac"]["expiry_date_str"] = None
    await update.message.reply_text("⏳ Срок: <b>бессрочно</b>", parse_mode='HTML')
    await _ask_comment(update.effective_chat.id, context)
    return AC_COMMENT


async def addclient_comment_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка текстового ввода комментария."""
    text = update.message.text.strip()
    if text.lower() in ("/skip", "skip", "пропустить"):
        comment = ""
    else:
        comment = text
    context.user_data["ac"]["comment"] = comment
    return await _finalize_addclient(update, context)


async def addclient_comment_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка /skip на шаге комментария."""
    context.user_data["ac"]["comment"] = ""
    return await _finalize_addclient(update, context)


async def _finalize_addclient(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Показывает превью и запрашивает подтверждение на создание клиента."""
    data = context.user_data.pop("ac", None)
    chat_id = update.effective_chat.id

    if not data or "hwid" not in data:
        await context.bot.send_message(
            chat_id=chat_id, text="Сессия потеряна. Начни заново /addclient."
        )
        return ConversationHandler.END

    panel_name = data["panel_name"]
    tg_id = data["tg_id"]
    email = data["email"]
    inbound_ids = data["inbound_ids"]
    hwid = data["hwid"]
    expiry_date_str = data.get("expiry_date_str")
    comment = data.get("comment") or ""

    expiry_line = f"до <code>{_esc(expiry_date_str)}</code>" if expiry_date_str else "<code>бессрочно</code>"
    comment_line = f"\n- 💬 Комментарий: {_esc(comment)}" if comment else ""

    preview = (
        f"<b>Создать клиента:</b>\n\n"
        f"- TG ID: <code>{tg_id}</code>\n"
        f"- Email: <code>{_esc(email)}</code>\n"
        f"- Панель: <code>{_esc(panel_name)}</code>\n"
        f"- Инбаунды: <code>{inbound_ids}</code>\n"
        f"- HWID лимит: <code>{hwid}</code>\n"
        f"- Срок: {expiry_line}{comment_line}"
    )

    await _ask_confirm(
        chat_id=chat_id,
        context=context,
        action="addclient",
        payload=data,
        preview=preview,
    )
    return ConversationHandler.END


async def addclient_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("ac", None)
    await update.message.reply_text("Создание клиента отменено.")
    return ConversationHandler.END


# --- /pausesub ---
@audit_command("/pausesub")
@admin_only
async def pausesub_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запрашивает подтверждение на постановку подписки на паузу."""
    if not context.args:
        await update.message.reply_text("Формат: /pausesub <tg_id> [email]")
        return

    try:
        tg_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("TG ID должен быть числом.")
        return

    email_filter = context.args[1].strip() if len(context.args) > 1 else None
    bindings = _find_bindings_for_admin(tg_id, email_filter)

    if not bindings:
        await update.message.reply_text("Не найдено связок для этого клиента.")
        return

    for b in bindings:
        err = _check_panel_available(b["panel_name"])
        if err:
            await update.message.reply_text(err)
            return

    preview_lines = [f"Поставить на паузу клиента <code>{tg_id}</code>:\n"]
    for b in bindings:
        status = "уже на паузе" if b.get("paused_at") else "будет приостановлена"
        preview_lines.append(f"Подписка <code>{_esc(b['email'])}</code> — {status}")

    await _ask_confirm(
        chat_id=update.effective_chat.id,
        context=context,
        action="pause",
        payload={"tg_id": tg_id, "bindings": bindings},
        preview="\n".join(preview_lines),
    )


async def _do_pause(update, context, payload, query) -> None:
    """Выполняет постановку на паузу после подтверждения."""
    tg_id = payload["tg_id"]
    bindings = payload["bindings"]
    pause_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = ["<b>Ставлю на паузу:</b>\n"]
    any_paused = False
    ok_count = 0
    emails_ok = []

    for b in bindings:
        panel_name = b["panel_name"]
        email = b["email"]

        if b.get("paused_at"):
            lines.append(_render_binding_line(panel_name, email, "уже на паузе"))
            continue

        result = None
        try:
            async with _get_panel_api(panel_name) as api:
                await api.login()
                result = await api.update_client(email, enable=False)
        except Exception as e:
            logger.error(f"[admin={update.effective_user.id}] Ошибка паузы '{panel_name}/{email}': {e}")

        if result is True:
            set_binding_paused(tg_id, panel_name, email, pause_ts)
            update_binding_enabled(tg_id, panel_name, email, False)
            lines.append(_render_binding_line(panel_name, email, "✅ приостановлена"))
            any_paused = True
            ok_count += 1
            emails_ok.append(email)
        elif result is False:
            lines.append(_render_binding_line(panel_name, email, "❌ клиента нет в панели"))
        else:
            lines.append(_render_binding_line(panel_name, email, "❌ ошибка связи с панелью"))

    await _edit_query_safely(query, "\n".join(lines))

    if any_paused:
        await _send_client_notice(
            context, tg_id,
            "⏸️ <b>Abuna saklandy</b>\n\n"
            "Saklanyş günleri harç edilmeýär. "
            "Täzeden işledeniňde — möhlet awtomatik uzaldylar.\n\n"
            "Administrator bilen habarlaşmak: 🆘 Kömek gerek"
        )

    total = len(bindings)
    if ok_count == 0:
        status = "FAIL"
    elif ok_count == total:
        status = "OK"
    else:
        status = "PARTIAL"
    emails_str = ",".join(emails_ok) if emails_ok else "-"
    audit_log(
        action="pause",
        tg_id=update.effective_user.id,
        username=update.effective_user.username or "",
        details=f"{status} tg_id={tg_id} count={ok_count}/{total} email={emails_str}",
    )


# --- /resumesub ---
@audit_command("/resumesub")
@admin_only
async def resumesub_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запрашивает подтверждение на возобновление подписки."""
    if not context.args:
        await update.message.reply_text("Формат: /resumesub <tg_id> [email]")
        return

    try:
        tg_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("TG ID должен быть числом.")
        return

    email_filter = context.args[1].strip() if len(context.args) > 1 else None
    bindings = _find_bindings_for_admin(tg_id, email_filter)

    if not bindings:
        await update.message.reply_text("Не найдено связок для этого клиента.")
        return

    for b in bindings:
        err = _check_panel_available(b["panel_name"])
        if err:
            await update.message.reply_text(err)
            return

    preview_lines = [f"Возобновить подписку клиента <code>{tg_id}</code>:\n"]
    for b in bindings:
        if b.get("paused_at"):
            preview_lines.append(f"Подписка <code>{_esc(b['email'])}</code> — будет возобновлена")
        else:
            preview_lines.append(f"Подписка <code>{_esc(b['email'])}</code> — не на паузе")

    await _ask_confirm(
        chat_id=update.effective_chat.id,
        context=context,
        action="resume",
        payload={"tg_id": tg_id, "bindings": bindings},
        preview="\n".join(preview_lines),
    )


async def _do_resume(update, context, payload, query) -> None:
    """Выполняет возобновление после подтверждения."""
    tg_id = payload["tg_id"]
    bindings = payload["bindings"]
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    lines = ["<b>Снимаю с паузы:</b>\n"]
    resumed = False
    ok_count = 0
    emails_ok = []

    for b in bindings:
        panel_name = b["panel_name"]
        email = b["email"]
        paused_at = b.get("paused_at")

        if not paused_at:
            lines.append(_render_binding_line(panel_name, email, "не на паузе"))
            continue

        pause_days = _days_between(paused_at.split()[0], today_str)

        old_expiry = b.get("expiry_date")

        if old_expiry:
            try:
                old_dt = datetime.strptime(old_expiry, "%Y-%m-%d")
            except ValueError:
                old_dt = now
            new_dt = old_dt + timedelta(days=pause_days)
            new_expiry_str = new_dt.strftime("%Y-%m-%d")
            new_expiry_dt = datetime.combine(
                new_dt.date(), time(23, 59, 59), tzinfo=_tz()
            )
            new_expiry_ts = int(new_expiry_dt.timestamp() * 1000)
            update_kwargs = {"enable": True, "expiryTime": new_expiry_ts}
            line_extra = f"(+{pause_days} дн., до {new_expiry_str})"
            resumed_status = "✅ возобновлена"
        else:
            update_kwargs = {"enable": True}
            new_expiry_str = None
            line_extra = "(бессрочная, срок не изменён)"
            resumed_status = "✅ возобновлена"

        result = None
        try:
            async with _get_panel_api(panel_name) as api:
                await api.login()
                result = await api.update_client(email, **update_kwargs)
        except Exception as e:
            logger.error(f"[admin={update.effective_user.id}] Ошибка снятия с паузы '{panel_name}/{email}': {e}")

        if result is True:
            set_binding_paused(tg_id, panel_name, email, None)
            update_binding_enabled(tg_id, panel_name, email, True)
            if new_expiry_str:
                update_binding_expiry(tg_id, panel_name, email, new_expiry_str)
            lines.append(_render_binding_line(panel_name, email, resumed_status, line_extra))
            resumed = True
            ok_count += 1
            emails_ok.append(email)
        elif result is False:
            lines.append(_render_binding_line(panel_name, email, "❌ клиента нет в панели"))
        else:
            lines.append(_render_binding_line(panel_name, email, "❌ ошибка связи с панелью"))

    await _edit_query_safely(query, "\n".join(lines))

    if resumed:
        await _send_client_notice(
            context, tg_id,
            "▶️ <b>Abuna täzeden işledildi.</b> Möhlet saklanyş günlerine uzaldylar."
        )

    total_paused = sum(1 for b in bindings if b.get("paused_at"))
    if ok_count == 0:
        status = "FAIL"
    elif ok_count == total_paused:
        status = "OK"
    else:
        status = "PARTIAL"
    emails_str = ",".join(emails_ok) if emails_ok else "-"
    audit_log(
        action="resume",
        tg_id=update.effective_user.id,
        username=update.effective_user.username or "",
        details=f"{status} tg_id={tg_id} count={ok_count}/{total_paused} email={emails_str}",
    )


# --- /extendsub ---
@audit_command("/extendsub")
@admin_only
async def extendsub_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запрашивает подтверждение на продление подписки."""
    if len(context.args) < 2:
        await update.message.reply_text(
            "Формат: /extendsub <tg_id> <+N дней | дата> [email]\n"
            "Примеры:\n"
            "  /extendsub 123456789 +30\n"
            "  /extendsub 123456789 2026-12-31\n"
            "  /extendsub 123456789 +30 user123"
        )
        return

    try:
        tg_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("TG ID должен быть числом.")
        return

    arg = context.args[1].strip()
    email_filter = context.args[2].strip() if len(context.args) > 2 else None

    kind, value, error = _parse_extend_argument(arg)
    if error:
        await update.message.reply_text(f"❌ {error}")
        return

    bindings = _find_bindings_for_admin(tg_id, email_filter)
    if not bindings:
        await update.message.reply_text("Не найдено связок для этого клиента.")
        return

    for b in bindings:
        err = _check_panel_available(b["panel_name"])
        if err:
            await update.message.reply_text(err)
            return

    preview_lines = [f"Продлить подписку клиента <code>{tg_id}</code>:\n"]
    if kind == "days":
        preview_lines.append(f"Аргумент: <b>+{value} дней</b>\n")
    else:
        preview_lines.append(f"Аргумент: <b>до {_esc(value)}</b>\n")
    for b in bindings:
        old = b.get("expiry_date") or "бессрочно"
        preview_lines.append(f"Подписка <code>{_esc(b['email'])}</code> (сейчас: {_esc(old)})")

    await _ask_confirm(
        chat_id=update.effective_chat.id,
        context=context,
        action="extend",
        payload={"tg_id": tg_id, "bindings": bindings, "kind": kind, "value": value},
        preview="\n".join(preview_lines),
    )


async def _do_extend(update, context, payload, query) -> None:
    """Выполняет продление после подтверждения."""
    tg_id = payload["tg_id"]
    bindings = payload["bindings"]
    kind = payload["kind"]
    value = payload["value"]
    now = datetime.now()
    lines = ["<b>Продлеваю подписку:</b>\n"]
    last_new_expiry_str = None
    ok_count = 0
    emails_ok = []

    for b in bindings:
        panel_name = b["panel_name"]
        email = b["email"]
        old_expiry = b.get("expiry_date")

        if kind == "days":
            if old_expiry:
                try:
                    base = datetime.strptime(old_expiry, "%Y-%m-%d")
                    if base.date() < now.date():
                        base = now
                except ValueError:
                    base = now
            else:
                base = now
            new_dt = base + timedelta(days=value)
            result_str = f"+{value} дн."
        else:
            new_dt = datetime.strptime(value, "%Y-%m-%d")
            result_str = f"до {value}"

        new_expiry_str = new_dt.strftime("%Y-%m-%d")
        new_expiry_dt = datetime.combine(
            new_dt.date(), time(23, 59, 59), tzinfo=_tz()
        )
        new_expiry_ts = int(new_expiry_dt.timestamp() * 1000)

        result = None
        try:
            async with _get_panel_api(panel_name) as api:
                await api.login()
                result = await api.update_client(email, expiryTime=new_expiry_ts)
        except Exception as e:
            logger.error(f"[admin={update.effective_user.id}] Ошибка продления '{panel_name}/{email}': {e}")

        if result is True:
            update_binding_expiry(tg_id, panel_name, email, new_expiry_str)
            lines.append(_render_binding_line(
                panel_name, email, f"✅ {result_str}",
                f"до {new_expiry_str}"
            ))
            last_new_expiry_str = new_expiry_str
            ok_count += 1
            emails_ok.append(email)
        elif result is False:
            lines.append(_render_binding_line(panel_name, email, "❌ клиента нет в панели"))
        else:
            lines.append(_render_binding_line(panel_name, email, "❌ ошибка связи с панелью"))

    await query.edit_message_text("\n".join(lines), parse_mode='HTML')

    if last_new_expiry_str:
        try:
            await context.bot.send_message(
                chat_id=tg_id,
                text=f"🎉 <b>Abuna uzaldylar.</b> Täze möhlet: {_esc(last_new_expiry_str)} çenli.",
                parse_mode='HTML',
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить клиента {tg_id} о продлении: {e}")

    total = len(bindings)
    if ok_count == 0:
        status = "FAIL"
    elif ok_count == total:
        status = "OK"
    else:
        status = "PARTIAL"
    emails_str = ",".join(emails_ok) if emails_ok else "-"
    arg = f"+{value}d" if kind == "days" else f"until={value}"
    audit_log(
        action="extend",
        tg_id=update.effective_user.id,
        username=update.effective_user.username or "",
        details=f"{status} tg_id={tg_id} {arg} email={emails_str}",
    )


# --- /revoke и /listclients ---
@audit_command("/revoke")
@admin_only
async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запрашивает подтверждение на удаление клиента."""
    if not context.args:
        await update.message.reply_text("Формат: /revoke <tg_id> [email]")
        return

    try:
        tg_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("TG ID должен быть числом.")
        return

    email_filter = context.args[1].strip() if len(context.args) > 1 else None
    bindings = _find_bindings_for_admin(tg_id, email_filter)

    if not bindings:
        await update.message.reply_text("Не найдено связок для удаления.")
        return

    for b in bindings:
        err = _check_panel_available(b["panel_name"])
        if err:
            await update.message.reply_text(err)
            return

    preview_lines = [f"<b>Удалить клиента</b> <code>{tg_id}</code>:\n"]
    for b in bindings:
        preview_lines.append(f"Подписка <code>{_esc(b['email'])}</code>")
    preview_lines.append("\n⚠️ Клиент будет <b>удалён из панели</b> и БД.")

    await _ask_confirm(
        chat_id=update.effective_chat.id,
        context=context,
        action="revoke",
        payload={"tg_id": tg_id, "bindings": bindings},
        preview="\n".join(preview_lines),
    )


async def _do_revoke(update, context, payload, query) -> None:
    """Выполняет удаление после подтверждения."""
    tg_id = payload["tg_id"]
    bindings = payload["bindings"]
    lines = ["<b>Удаляю клиентов:</b>\n"]

    # Счётчики для audit
    ok_panels = 0
    ok_db = 0
    emails_deleted = []

    for b in bindings:
        panel_name = b["panel_name"]
        email = b["email"]

        panel_ok = False
        try:
            async with _get_panel_api(panel_name) as api:
                await api.login()
                panel_ok = await api.delete_client(email)
        except Exception as e:
            logger.error(
                f"[admin={update.effective_user.id}] Ошибка удаления из панели '{panel_name}/{email}': {e}")

        db_ok = delete_binding(tg_id, panel_name, email)

        if panel_ok:
            ok_panels += 1
        if db_ok:
            ok_db += 1
        if panel_ok or db_ok:
            emails_deleted.append(email)

        mark_panel = "✅" if panel_ok else "❌"
        mark_db = "✅" if db_ok else "❌"
        lines.append(
            f"- <code>{_esc(panel_name)}/{_esc(email)}</code> — "
            f"панель: {mark_panel}, БД: {mark_db}"
        )

    await _edit_query_safely(query, "\n".join(lines))

    # Audit-запись
    total = len(bindings)
    emails_str = ",".join(_esc(e) for e in emails_deleted) if emails_deleted else "-"
    if ok_panels == total and ok_db == total:
        status = "OK"
    elif ok_panels == 0 and ok_db == 0:
        status = "FAIL"
    else:
        status = "PARTIAL"
    audit_log(
        action="revoke",
        tg_id=update.effective_user.id,
        username=update.effective_user.username or "",
        details=f"{status} tg_id={tg_id} email={emails_str}",
    )


async def _do_broadcast(update, context, payload, query) -> None:
    """Выполняет рассылку после подтверждения."""
    import asyncio

    recipients = payload["recipients"]
    message = payload["message"]
    total = len(recipients)

    delivered = 0
    failed = 0
    failed_ids = []

    # Начальный прогресс
    try:
        await query.edit_message_text(f"📤 Отправляю... 0/{total}")
    except Exception:
        pass

    for i, tg_id in enumerate(recipients, start=1):
        try:
            await context.bot.send_message(chat_id=tg_id, text=message, parse_mode='HTML')
            delivered += 1
        except Exception as e:
            err_str = str(e)
            # Если HTML сломался — пробуем без разметки
            if "can't parse entities" in err_str.lower():
                try:
                    await context.bot.send_message(chat_id=tg_id, text=message)
                    delivered += 1
                except Exception as e2:
                    failed += 1
                    failed_ids.append(tg_id)
                    logger.warning(f"[broadcast] Не доставлено {tg_id}: {e2}")
            else:
                failed += 1
                failed_ids.append(tg_id)
                logger.warning(f"[broadcast] Не доставлено {tg_id}: {e}")

        # Обновляем прогресс каждые 50 отправок или на последнем
        if i % 50 == 0 or i == total:
            try:
                await query.edit_message_text(f"📤 Отправляю... {i}/{total}")
            except Exception:
                pass

        # Задержка 50 мс между отправками (не более 20 msg/sec)
        if i < total:
            await asyncio.sleep(0.05)

    # Итоговый отчёт
    lines = [
        f"✅ <b>Рассылка завершена</b>\n",
        f"<b>Доставлено: {delivered} из {total}</b>",
        f"- Успешно: {delivered}",
        f"- Не доставлено: {failed}",
    ]
    if failed_ids:
        sample = failed_ids[:10]
        lines.append(
            f"\nПервые неудачные ID: "
            f"<code>{', '.join(str(x) for x in sample)}</code>"
        )
        if len(failed_ids) > 10:
            lines.append(f"...и ещё {len(failed_ids) - 10}")

    try:
        await query.edit_message_text("\n".join(lines), parse_mode='HTML')
    except Exception:
        await query.edit_message_text("\n".join(lines))

    # Audit-запись
    if failed == 0:
        status = "OK"
    elif delivered == 0:
        status = "FAIL"
    else:
        status = "PARTIAL"
    audit_log(
        action="broadcast",
        tg_id=update.effective_user.id,
        username=update.effective_user.username or "",
        details=f"{status} recipients={total} delivered={delivered} failed={failed}",
    )


# --- /getlink ---
@audit_command("/getlink")
@admin_only
async def getlink_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запрашивает: выдать sub-ссылку или ссылки на конфиги."""
    if not context.args:
        await update.message.reply_text("Формат: /getlink <tg_id> [email]")
        return

    try:
        tg_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("TG ID должен быть числом.")
        return

    email_filter = context.args[1].strip() if len(context.args) > 1 else None
    bindings = _find_bindings_for_admin(tg_id, email_filter)

    if not bindings:
        await update.message.reply_text("Не найдено связок для этого клиента.")
        return

    for b in bindings:
        err = _check_panel_available(b["panel_name"])
        if err:
            await update.message.reply_text(err)
            return

    # Сохраняем список в user_data — понадобится в callback
    context.user_data["getlink_bindings"] = bindings
    context.user_data["getlink_tg_id"] = tg_id

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Ссылка на подписку", callback_data="getlink:sub")],
        [InlineKeyboardButton("🧩 Ссылки на все конфигурации", callback_data="getlink:confs")],
        [InlineKeyboardButton("❌ Отмена", callback_data="getlink:cancel")],
    ])

    await update.message.reply_text(
        f"🔗 <b>Что выдать для клиента</b> <code>{tg_id}</code>?\n"
        f"Найдено подписок: <b>{len(bindings)}</b>\n\n"
        f"<i>«Ссылка на подписку» — приложение само подтянет список серверов "
        f"(может не работать при блокировках провайдера).\n"
        f"«Ссылки на все конфигурации» — прямые ссылки на каждый инбаунд, "
        f"которые можно импортировать по одной.</i>",
        parse_mode='HTML',
        reply_markup=keyboard,
    )


async def getlink_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает выбор: sub-ссылка или конфиги."""
    query = update.callback_query
    await query.answer()

    data = query.data or ""

    if data == "getlink:cancel":
        try:
            await query.edit_message_text("❌ Отменено.")
        except Exception:
            pass
        context.user_data.pop("getlink_bindings", None)
        context.user_data.pop("getlink_tg_id", None)
        return

    bindings = context.user_data.get("getlink_bindings")
    tg_id = context.user_data.get("getlink_tg_id")

    if not bindings or tg_id is None:
        try:
            await query.edit_message_text(
                "⚠️ Список устарел (бот перезапускался). Запусти /getlink снова."
            )
        except Exception:
            pass
        return

    # Убираем сообщение с кнопками — оно больше не нужно
    try:
        await query.message.delete()
    except Exception:
        pass

    if data == "getlink:sub":
        await _getlink_send_sub(query.message.chat_id, context, tg_id, bindings)
    elif data == "getlink:confs":
        await _getlink_send_configs(query.message.chat_id, context, tg_id, bindings)

    context.user_data.pop("getlink_bindings", None)
    context.user_data.pop("getlink_tg_id", None)


async def _getlink_send_sub(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    tg_id: int,
    bindings: list,
) -> None:
    """Отправляет sub-ссылку по каждой связке (как раньше)."""
    lines = [f"🔗 <b>Ссылки клиента</b> <code>{tg_id}</code>:\n"]
    for b in bindings:
        panel_name = b["panel_name"]
        email = b["email"]
        panel_config = config.get_panel_config(panel_name)
        sub_url = panel_config.get("sub_url", "").rstrip("/")
        status = "⏸️ приостановлена" if b.get("paused_at") else "▶️ активна"

        warn_line = ""
        try:
            async with _get_panel_api(panel_name) as api:
                ok = await api.login()
                if ok:
                    client = await api.get_client_object(email)
                    if client is None:
                        warn_line = "\n⚠️ клиента нет в панели — ссылка не работает"
                else:
                    warn_line = "\n⚠️ не удалось подключиться к панели"
        except Exception as e:
            logger.warning(f"Проверка клиента '{panel_name}/{email}': {e}")
            warn_line = "\n⚠️ не удалось подключиться к панели"

        if sub_url:
            link = f"{sub_url}/{b['sub_id']}"
            lines.append(
                f"<b>{_esc(panel_name)}</b> ({_esc(email)}) — {status}:\n"
                f"<code>{_esc(link)}</code>{warn_line}\n"
            )
        else:
            lines.append(
                f"<b>{_esc(panel_name)}</b> ({_esc(email)}) — {status}: "
                f"sub_url не настроен\n"
            )

    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode='HTML')


async def _getlink_send_configs(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    tg_id: int,
    bindings: list,
) -> None:
    """Отправляет по одному сообщению на каждую связку со списком конфигов."""
    for b in bindings:
        panel_name = b["panel_name"]
        email = b["email"]
        status = "⏸️ приостановлена" if b.get("paused_at") else "▶️ активна"

        links = None
        error_note = ""
        try:
            async with _get_panel_api(panel_name) as api:
                ok = await api.login()
                if not ok:
                    error_note = "❌ Не удалось подключиться к панели."
                else:
                    links = await api.get_client_links(email)
        except Exception as e:
            logger.error(f"[getlink] Ошибка загрузки конфигов '{panel_name}/{email}': {e}")
            error_note = f"❌ Ошибка: {_esc(str(e))}"

        header = (
            f"🧩 <b>Конфигурации клиента</b> <code>{tg_id}</code>\n"
            f"Панель: <b>{_esc(panel_name)}</b>\n"
            f"Подписка: <code>{_esc(email)}</code> — {status}\n"
        )

        if error_note:
            await context.bot.send_message(
                chat_id=chat_id, text=header + "\n" + error_note, parse_mode='HTML'
            )
            continue

        if links is None:
            await context.bot.send_message(
                chat_id=chat_id,
                text=header + "\n⚠️ Панель не отдала конфигурации. Попробуй позже.",
                parse_mode='HTML',
            )
            continue

        if not links:
            await context.bot.send_message(
                chat_id=chat_id,
                text=header + "\n⚠️ Клиент не привязан ни к одному инбаунду.",
                parse_mode='HTML',
            )
            continue

        # Собираем сообщение: по одному пункту на конфиг
        lines = [header, f"Найдено конфигов: <b>{len(links)}</b>\n"]
        for i, link in enumerate(links, 1):
            # Анкор (после #) — это имя вида "🇹🇲 Acar_VL_R-prk000"
            name = ""
            if "#" in link:
                name = unquote(link.split("#", 1)[1])
            name_line = f"\n<b>{i}. {_esc(name)}</b>" if name else f"\n<b>{i}.</b>"
            lines.append(f"{name_line}\n<code>{_esc(link)}</code>")

        text = "\n".join(lines)

        # Telegram не пропустит > 4096. Делим, если не влезло.
        if len(text) <= 4000:
            try:
                await context.bot.send_message(
                    chat_id=chat_id, text=text, parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"[getlink] Не удалось отправить конфиги: {e}")
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=header + "\n⚠️ Не удалось отправить конфигурации. Проверь логи.",
                    parse_mode='HTML',
                )
        else:
            # Шлём шапку отдельно, потом по одному конфигу отдельным сообщением
            await context.bot.send_message(
                chat_id=chat_id,
                text=header + f"\nНайдено конфигов: <b>{len(links)}</b>\n"
                              f"<i>Из-за размера шлём по одному.</i>",
                parse_mode='HTML',
            )
            for i, link in enumerate(links, 1):
                name = ""
                if "#" in link:
                    name = unquote(link.split("#", 1)[1])
                name_line = f"<b>{i}. {_esc(name)}</b>" if name else f"<b>{i}.</b>"
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"{name_line}\n<code>{_esc(link)}</code>",
                        parse_mode='HTML',
                    )
                except Exception as e:
                    logger.error(f"[getlink] Не удалось отправить конфиг #{i}: {e}")


# ---------- /setcomment ----------
@audit_command("/setcomment")
@admin_only
async def setcomment_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Меняет комментарий клиента — и в БД, и в панели."""
    if len(context.args) < 3:
        await update.message.reply_text(
            "Формат: <code>/setcomment &#60;tg_id&#62; &#60;email&#62; &#60;новый комментарий&#62;</code>\n\n"
            "Пустой комментарий — используй <code>-</code> вместо текста.",
            parse_mode='HTML',
        )
        return

    try:
        tg_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("TG ID должен быть числом.")
        return

    email = context.args[1].strip()
    raw_comment = " ".join(context.args[2:]).strip()
    new_comment = "" if raw_comment == "-" else raw_comment

    admin_id = update.effective_user.id
    admin_username = update.effective_user.username or ""

    bindings = _find_bindings_for_admin(tg_id, email)
    if not bindings:
        audit_log("setcomment", admin_id, admin_username,
                  f"FAIL reason=\"связка не найдена\" tg_id={tg_id} email={email}")
        await update.message.reply_text(
            f"Связка <code>{_esc(email)}</code> у клиента {tg_id} не найдена.",
            parse_mode='HTML',
        )
        return

    b = bindings[0]
    panel_name = b["panel_name"]

    err = _check_panel_available(panel_name)
    if err:
        audit_log("setcomment", admin_id, admin_username,
                  f"FAIL reason=\"панель недоступна\" tg_id={tg_id} email={email} panel={panel_name}")
        await update.message.reply_text(err)
        return

    result = None
    try:
        async with _get_panel_api(panel_name) as api:
            await api.login()
            result = await api.update_client(email, comment=new_comment)
    except Exception as e:
        logger.error(f"[admin={admin_id}] Ошибка setcomment '{panel_name}/{email}': {e}")

    if result is True:
        try:
            update_binding_comment(tg_id, panel_name, email, new_comment)
        except Exception as e:
            logger.error(f"Не удалось обновить комментарий в БД: {e}")
        audit_log("setcomment", admin_id, admin_username,
                  f"OK tg_id={tg_id} email={email} panel={panel_name}")
        shown = f"<code>{_esc(new_comment)}</code>" if new_comment else "<i>(пустой)</i>"
        await update.message.reply_text(
            f"✅ Комментарий обновлён.\n\n"
            f"Панель: <code>{_esc(panel_name)}</code>\n"
            f"Клиент: <code>{_esc(email)}</code>\n"
            f"Комментарий: {shown}",
            parse_mode='HTML',
        )
    elif result is False:
        audit_log("setcomment", admin_id, admin_username,
                  f"FAIL reason=\"клиента нет в панели\" tg_id={tg_id} email={email} panel={panel_name}")
        await update.message.reply_text("❌ Клиента нет в панели.")
    else:
        audit_log("setcomment", admin_id, admin_username,
                  f"FAIL reason=\"ошибка связи с панелью\" tg_id={tg_id} email={email} panel={panel_name}")
        await update.message.reply_text("❌ Ошибка связи с панелью.")


# ---------- /rename ----------
@audit_command("/rename")
@admin_only
async def rename_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Меняет email клиента — и в БД, и в панели, и в истории трафика."""
    if len(context.args) < 3:
        await update.message.reply_text(
            "Формат: /rename <tg_id> <старый_email> <новый_email>"
        )
        return

    try:
        tg_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("TG ID должен быть числом.")
        return

    old_email = context.args[1].strip()
    new_email = context.args[2].strip()

    admin_id = update.effective_user.id
    admin_username = update.effective_user.username or ""

    err = _validate_email(new_email)
    if err:
        audit_log("rename", admin_id, admin_username,
                  f"FAIL reason=\"email невалиден\" tg_id={tg_id} old={old_email} new={new_email}")
        await update.message.reply_text(err)
        return

    bindings = _find_bindings_for_admin(tg_id, old_email)
    if not bindings:
        audit_log("rename", admin_id, admin_username,
                  f"FAIL reason=\"связка не найдена\" tg_id={tg_id} old={old_email}")
        await update.message.reply_text(
            f"Связка <code>{_esc(old_email)}</code> у клиента {tg_id} не найдена.",
            parse_mode='HTML',
        )
        return

    b = bindings[0]
    panel_name = b["panel_name"]

    err = _check_panel_available(panel_name)
    if err:
        audit_log("rename", admin_id, admin_username,
                  f"FAIL reason=\"панель недоступна\" tg_id={tg_id} old={old_email} panel={panel_name}")
        await update.message.reply_text(err)
        return

    # Проверка в БД
    if get_binding_by_email(panel_name, new_email):
        audit_log("rename", admin_id, admin_username,
                  f"FAIL reason=\"email занят в БД\" old={old_email} new={new_email} panel={panel_name}")
        await update.message.reply_text(
            f"❌ Клиент с email <code>{_esc(new_email)}</code> уже существует в БД на '{_esc(panel_name)}'.",
            parse_mode='HTML',
        )
        return

    # Проверка в панели + смена email
    result = None
    try:
        async with _get_panel_api(panel_name) as api:
            await api.login()
            existing = await api.get_client_object(new_email)
            if existing is not None:
                audit_log("rename", admin_id, admin_username,
                          f"FAIL reason=\"email занят в панели\" old={old_email} new={new_email} panel={panel_name}")
                await update.message.reply_text(
                    f"❌ Клиент с email <code>{_esc(new_email)}</code> уже есть в панели.",
                    parse_mode='HTML',
                )
                return
            result = await api.update_client(old_email, new_email=new_email)
    except Exception as e:
        logger.error(f"[admin={admin_id}] Ошибка rename '{panel_name}': {e}")

    if result is not True:
        if result is False:
            audit_log("rename", admin_id, admin_username,
                      f"FAIL reason=\"клиента нет в панели\" old={old_email} new={new_email} panel={panel_name}")
            await update.message.reply_text(
                f"❌ Клиента <code>{_esc(old_email)}</code> нет в панели.",
                parse_mode='HTML',
            )
        else:
            audit_log("rename", admin_id, admin_username,
                      f"FAIL reason=\"ошибка связи с панелью\" old={old_email} new={new_email} panel={panel_name}")
            await update.message.reply_text("❌ Ошибка связи с панелью.")
        return

    # Обновляем БД
    db_ok = update_binding_email(tg_id, panel_name, old_email, new_email)
    if not db_ok:
        logger.error(f"БД: не удалось переименовать {old_email} -> {new_email}")
        audit_log("rename", admin_id, admin_username,
                  f"PARTIAL reason=\"панель ок, БД ошибка\" old={old_email} new={new_email} panel={panel_name}")
        await update.message.reply_text(
            "⚠️ Панель обновлена, но в БД ошибка.\nЗапусти <code>/sync</code> для восстановления.",
            parse_mode='HTML',
        )
        return

    renamed = rename_traffic_email(panel_name, old_email, new_email)

    audit_log("rename", admin_id, admin_username,
              f"OK tg_id={tg_id} old={old_email} new={new_email} panel={panel_name} traffic_renamed={renamed}")

    await update.message.reply_text(
        f"✅ <b>Email изменён</b>\n\n"
        f"Было: <code>{_esc(old_email)}</code>\n"
        f"Стало: <code>{_esc(new_email)}</code>\n"
        f"Панель: <code>{_esc(panel_name)}</code>\n"
        f"Записей трафика обновлено: {renamed}",
        parse_mode='HTML',
    )


# ---------- /sync ----------
@audit_command("/sync")
@superadmin_only
async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Синхронизирует БД из панели."""
    await update.message.reply_text("🔄 Запускаю синхронизацию. Это займёт несколько секунд...")

    result = await sync_all_panels()

    if not result["details"]:
        await update.message.reply_text("Панели не настроены.")
        return

    lines = []
    for d in result["details"]:
        if d["status"] == "ok":
            lines.append(
                f"✅ <b>{_esc(d['panel_name'])}</b>: обновлено {d['updated']}, "
                f"переименовано {d['renamed']}, потеряно {d['missing']}"
            )
        elif d["status"] == "disabled":
            lines.append(f"⏭️ <b>{_esc(d['panel_name'])}</b>: отключена")
        elif d["status"] == "error":
            lines.append(f"❌ <b>{_esc(d['panel_name'])}</b>: ошибка связи")

    lines.append(
        f"\n<b>Итого:</b> обновлено {result['total_updated']}, "
        f"переименовано {result['total_renamed']}, не найдено в панели {result['total_missing']}"
    )
    await update.message.reply_text("\n".join(lines), parse_mode='HTML')

    audit_log(
        action="sync",
        tg_id=update.effective_user.id,
        username=update.effective_user.username or "",
        details=(
            f"OK updated={result['total_updated']} "
            f"renamed={result['total_renamed']} "
            f"missing={result['total_missing']}"
        ),
    )


@audit_command("/broadcast")
@admin_only
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Рассылка сообщений пользователям бота."""
    target_tg_id, message, error = _parse_broadcast_args(context.args or [])
    if error:
        await update.message.reply_text(
            f"❌ {_esc(error)}\n\n"
            f"Примеры:\n"
            f"<code>/broadcast Привет всем</code>\n"
            f"<code>/broadcast 123456789 Привет Ивану</code>",
            parse_mode='HTML',
        )
        return

    # Кому отправляем
    if target_tg_id is None:
        recipients = _get_broadcast_recipients_all()
        recipient_desc = f"<b>всем</b> ({len(recipients)} получателей)"
    else:
        recipients = [target_tg_id]
        recipient_desc = f"пользователю <code>{target_tg_id}</code>"

    if not recipients:
        await update.message.reply_text("Нет получателей для рассылки.")
        return

    # Инициатор — для прозрачности при двух админах
    initiator = update.effective_user
    init_name = initiator.full_name or initiator.username or str(initiator.id)

    # Превью
    preview_lines = [
        f"<b>Рассылка</b>\n",
        f"Кому: {recipient_desc}",
        f"Инициатор: <b>{_esc(init_name)}</b> (<code>{initiator.id}</code>)\n",
        f"<b>Текст сообщения:</b>",
        f"---",
        _esc(message),
        f"---",
        f"\n⚠️ Сообщение будет отправлено <b>сразу</b> после нажатия «Да».",
    ]

    await _ask_confirm(
        chat_id=update.effective_chat.id,
        context=context,
        action="broadcast",
        payload={"recipients": recipients, "message": message},
        preview="\n".join(preview_lines),
    )


def _get_broadcast_recipients_all() -> list:
    """
    Возвращает список tg_id для рассылки всем — включая админов.

    Админы тоже получают — для фактчека: убедиться, что рассылка
    действительно ушла и всё работает.
    """
    from database import list_bot_users
    return [u["tg_id"] for u in list_bot_users()]


async def _do_addclient(update, context, payload, query) -> None:
    """Выполняет создание клиента после подтверждения."""
    panel_name = payload["panel_name"]
    tg_id = payload["tg_id"]
    email = payload["email"]
    inbound_ids = payload["inbound_ids"]
    hwid = payload["hwid"]
    expiry_ts = payload.get("expiry_ts", 0)
    expiry_date_str = payload.get("expiry_date_str")
    comment = payload.get("comment") or ""

    admin_id = update.effective_user.id
    admin_username = update.effective_user.username or ""

    client_uuid = str(uuid_module.uuid4())
    sub_id = _make_sub_id()

    async with _get_panel_api(panel_name) as api:
        ok = await api.login()
        if not ok:
            audit_log("addclient", admin_id, admin_username,
                      f"FAIL reason=\"логин в панель\" email={email} panel={panel_name}")
            await query.edit_message_text("❌ Не удалось подключиться к панели.")
            logger.error(f"[admin={admin_id}] Не удалось залогиниться в панель '{panel_name}'")
            return

        created = await api.create_client(
            email=email,
            client_uuid=client_uuid,
            sub_id=sub_id,
            inbound_ids=inbound_ids,
            limit_hwid=hwid,
            tg_id=tg_id,
            expiry_time=expiry_ts,
            comment=comment,
        )
        sub_link = api.get_client_sub_link(sub_id)

    if not created:
        audit_log("addclient", admin_id, admin_username,
                  f"FAIL reason=\"панель отказала\" email={email} panel={panel_name}")
        await query.edit_message_text("❌ Не удалось создать клиента. Проверь логи бота.")
        logger.error(f"[admin={admin_id}] Не удалось создать клиента '{email}' на '{panel_name}'")
        return

    if not sub_link:
        audit_log("addclient", admin_id, admin_username,
                  f"PARTIAL reason=\"sub_url не настроен\" tg_id={tg_id} email={email} panel={panel_name}")
        await query.edit_message_text(
            "⚠️ Клиент создан в панели, но sub_url у панели не настроен.\n"
            "Настрой sub_url через /setting и выдай ссылку клиенту вручную через /getlink."
        )
        return

    try:
        save_binding(
            tg_id=tg_id,
            panel_name=panel_name,
            email=email,
            inbound_ids=inbound_ids,
            sub_id=sub_id,
            uuid=client_uuid,
            limit_hwid=hwid,
            expiry_date=expiry_date_str,
            comment=comment,
        )
    except Exception as e:
        logger.error(f"Не удалось сохранить связку: {e}")

    expiry_line_client = (
        f"Möhlet: <b>{_esc(expiry_date_str)}</b> çenli"
        if expiry_date_str else "Möhlet: <b>möhletsiz</b>"
    )
    keyboard = None if config.is_admin(tg_id) else _client_reply_keyboard()
    delivered = True
    try:
        await context.bot.send_message(
            chat_id=tg_id,
            text=(
                f"🎉 <b>Seniň abunaň taýýar!</b>\n\n"
                f"Panel: <b>{_esc(panel_name)}</b>\n"
                f"Login: <code>{_esc(email)}</code>\n"
                f"{expiry_line_client}\n\n"
                f"<b>Abuna salgysy:</b>\n<code>{_esc(sub_link)}</code>\n\n"
                f"Salgyny bas — brauzer açylar. "
                f"Göçürmek üçin, barmagyňy salgynyň üstündäki sakla."
            ),
            parse_mode='HTML',
            reply_markup=keyboard,
        )
    except Exception as e:
        delivered = False
        logger.error(f"Не удалось отправить клиенту {tg_id}: {e}")

    comment_line = f"💬 Комментарий: <code>{_esc(comment)}</code>\n" if comment else ""
    expiry_line_admin = f"до {_esc(expiry_date_str)}" if expiry_date_str else "бессрочно"

    admin_msg = (
        f"✅ <b>Клиент создан</b>\n\n"
        f"TG ID: <code>{tg_id}</code>\n"
        f"Email: <code>{_esc(email)}</code>\n"
        f"Панель: <code>{_esc(panel_name)}</code>\n"
        f"Инбаунды: <code>{inbound_ids}</code>\n"
        f"HWID лимит: <code>{hwid}</code>\n"
        f"UUID: <code>{client_uuid}</code>\n"
        f"Sub ID: <code>{sub_id}</code>\n"
        f"Срок: {expiry_line_admin}\n"
        f"{comment_line}"
    )
    if not delivered:
        admin_msg += "\n\n⚠️ Не удалось доставить клиенту — возможно, он не запускал бота."

    await query.edit_message_text(admin_msg, parse_mode='HTML')

    delivered_str = "delivered=yes" if delivered else "delivered=no"
    audit_log(
        action="addclient",
        tg_id=admin_id,
        username=admin_username,
        details=f"OK tg_id={tg_id} email={email} panel={panel_name} inbound={inbound_ids} {delivered_str}",
    )


CLIENTS_PAGE_SIZE = 5


def _format_client_binding(b: dict) -> str:
    """Форматирует одну связку клиента для /listclients (HTML)."""
    username = b.get("bot_username")
    first_name = b.get("bot_first_name")
    if username:
        user_line = f"👤 <code>@{_esc(username)}</code>"
    elif first_name:
        user_line = f"👤 {_esc(first_name)}"
    else:
        user_line = "👤 <code>без username</code>"

    email = b.get("email") or "—"
    panel_name = b.get("panel_name") or "—"
    tg_id = b.get("tg_id")
    hwid = b.get("limit_hwid", 0)
    hwid_str = "безлимит" if hwid == 0 else str(hwid)

    expiry = b.get("expiry_date") or "бессрочно"

    panel_config = config.get_panel_config(panel_name)
    panel_available = bool(panel_config) and not panel_config.get("disabled", False)

    if not panel_available:
        status_icon = "🚫"
    elif b.get("paused_at"):
        status_icon = "⏸️"
    elif not b.get("enabled", True):
        status_icon = "⛔"
    else:
        status_icon = "▶️"

    lines = [
        user_line,
        f"✏️ <code>{_esc(email)}</code>",
        f"🎛️ <code>{_esc(panel_name)}</code>",
        f"🆔 <code>{tg_id}</code>",
        f"📳 HWID <code>{_esc(hwid_str)}</code>",
        f"{status_icon} до <code>{_esc(expiry)}</code>",
    ]
    if b.get("comment"):
        lines.append(f"💬 <i>{_esc(b['comment'])}</i>")
    return "\n".join(lines)


def _clients_keyboard(page: int, total_pages: int) -> InlineKeyboardMarkup:
    """Клавиатура навигации по страницам /listclients."""
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(
            "◀️ Предыдущие", callback_data=f"clients:page:{page - 1}"
        ))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(
            "Следующие ▶️", callback_data=f"clients:page:{page + 1}"
        ))

    keyboard = []
    if nav_row:
        keyboard.append(nav_row)
    keyboard.append([InlineKeyboardButton("✅ Готово", callback_data="clients:done")])
    return InlineKeyboardMarkup(keyboard)


def _render_clients_page(bindings: list, page: int) -> tuple:
    """Собирает текст и клавиатуру для страницы. Возвращает (text, keyboard)."""
    total = len(bindings)
    total_pages = max(1, (total + CLIENTS_PAGE_SIZE - 1) // CLIENTS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    start = page * CLIENTS_PAGE_SIZE
    end = start + CLIENTS_PAGE_SIZE
    chunk = bindings[start:end]

    lines = [f"👥 <b>Список клиентов</b> (страница {page + 1}/{total_pages}):\n"]
    for i, b in enumerate(chunk):
        lines.append("============================")
        lines.append(_format_client_binding(b))
    lines.append("============================")

    return "\n".join(lines), _clients_keyboard(page, total_pages)


@admin_only
async def listclients_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Список всех выданных клиентов с пагинацией."""
    bindings = list_all_bindings_with_users()
    if not bindings:
        await update.message.reply_text("Пока нет ни одного выданного клиента.")
        return

    # Запоминаем id команды /listclients — удалим её по кнопке «Готово»
    context.user_data['clients_cmd_msg_id'] = update.message.message_id

    text, keyboard = _render_clients_page(bindings, 0)
    await update.message.reply_text(text, parse_mode='HTML', reply_markup=keyboard)


# --- Политика конфиденциальности ---
async def policy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = config.get_policy_url()
    if not url:
        await update.message.reply_text(
            "🔒 Gizlinlik syýasaty entek çap edilmedi. "
            "Administratorda ýüz tut."
        )
        return

    message = config.get_policy_message()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Doly okamak", url=url)]
    ])

    try:
        await update.message.reply_text(
            message, parse_mode='HTML', reply_markup=keyboard,
        )
    except Exception as e:
        logger.warning(f"HTML для /policy не сработал: {e}. Отправляю как plain text.")
        await update.message.reply_text(message, reply_markup=keyboard)


# ---------- Гайды ----------

async def _send_guide(update: Update, role: str) -> None:
    """Отправляет сообщение гайда с inline-кнопкой, если URL задан."""
    url = config.get_guide_url(role)
    message = config.get_guide_message(role)

    keyboard = None
    if url:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📖 Doly okamak", url=url)]
        ])

    try:
        await update.message.reply_text(
            message, parse_mode='HTML', reply_markup=keyboard,
        )
    except Exception as e:
        logger.warning(f"HTML для гайда '{role}' не сработал: {e}. Отправляю как plain text.")
        await update.message.reply_text(message, reply_markup=keyboard)


async def guide_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Гайд для клиента (доступен всем)."""
    await _send_guide(update, "client")


async def guide_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Гайд для администратора."""
    if not config.is_admin(update.effective_user.id):
        await update.message.reply_text("Извините, эта команда доступна только администраторам.")
        return
    await _send_guide(update, "admin")


async def guide_super_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Гайд для суперадминистратора."""
    if not config.is_superadmin(update.effective_user.id):
        await update.message.reply_text(
            "Извините, эта команда доступна только главному администратору."
        )
        return
    await _send_guide(update, "superadmin")


# --- Клиентские команды ---
async def mylink_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет клиенту его sub-ссылку(-и)."""
    tg_id = update.effective_user.id
    bindings = get_user_bindings(tg_id)
    if not bindings:
        await update.message.reply_text(
            "Häzir seniň üçin berlen abuna ýok. Administratorda ýüz tut."
        )
        return

    # Разделяем на активные и приостановленные
    active = [b for b in bindings if not b.get("paused_at")]
    paused = [b for b in bindings if b.get("paused_at")]

    if not active and paused:
        await update.message.reply_text(
            "⏸️ Seniň abunaň saklandy.\n\n"
            "Täzeden işletmek üçin 🆘 Kömek gerek düwmesine bas."
        )
        return

    lines = ["🔗 <b>Seniň abuna salgylaryň:</b>\n"]
    for b in active:
        panel_config = config.get_panel_config(b["panel_name"])
        sub_url = panel_config.get("sub_url", "").rstrip("/")
        if sub_url:
            link = f"{sub_url}/{b['sub_id']}"
            lines.append(f"<b>{_esc(b['panel_name'])}</b> ({_esc(b['email'])}):\n<code>{_esc(link)}</code>\n")
        else:
            lines.append(f"<b>{_esc(b['panel_name'])}</b> ({_esc(b['email'])}): sub_url sazlanmady\n")

    if paused:
        lines.append("⏸️ <i>Käbir abunalar saklandy. Täzeden işletmek üçin 🆘 Kömek gerek düwmesine bas.</i>")

    await update.message.reply_text("\n".join(lines), parse_mode='HTML')


# --- Кнопки reply-клавиатуры ---
async def btn_my_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка '🔗 Ссылка подписки' — то же, что /mylink."""
    await mylink_command(update, context)


async def btn_tariffs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка '📊 Nyrhlar' — сообщение с inline-кнопкой на страницу тарифов."""
    url = config.get_tariffs_url()
    if not url:
        await update.message.reply_text(
            "📊 Nyrhlar entek sazlanmady. Administratorda ýüz tut."
        )
        return

    message = config.get_tariffs_message()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Nyrhlary açmak", url=url)]
    ])

    try:
        await update.message.reply_text(
            message, parse_mode='HTML', reply_markup=keyboard,
        )
    except Exception as e:
        logger.warning(f"HTML для тарифов не сработал: {e}. Отправляю как plain text.")
        await update.message.reply_text(message, reply_markup=keyboard)


async def btn_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка '🆘 Нужна помощь' — заглушка клиенту + уведомление админам."""
    user = update.effective_user
    full_name = user.full_name or "—"
    username = f"@{user.username}" if user.username else "—"

    await update.message.reply_text(
        "🆘 Haýyşyňy administrada iberdim.\n"
        "Ol tiz wagtyň içinde şahsy habarlaşar."
    )

    notif = (
        f"🆘 <b>Клиент просит помощи</b>\n\n"
        f"- Имя: {_esc(full_name)}\n"
        f"- Username: {_esc(username)}\n"
        f"- TG ID: <code>{user.id}</code>"
    )
    for uid in config.get_admin_users():
        try:
            await context.bot.send_message(
                chat_id=uid, text=notif, parse_mode='HTML',
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить админа {uid}: {e}")


async def client_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline-кнопка «🆘 Kömek gerek» из уведомления об истечении."""
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    full_name = user.full_name or "—"
    username = f"@{user.username}" if user.username else "—"

    await query.message.reply_text(
        "🆘 Haýyşyňy administrada iberdim.\n"
        "Ol tiz wagtyň içinde şahsy habarlaşar."
    )

    notif = (
        f"🆘 <b>Клиент просит помощи</b>\n\n"
        f"- Имя: {_esc(full_name)}\n"
        f"- Username: {_esc(username)}\n"
        f"- TG ID: <code>{user.id}</code>"
    )
    for uid in config.get_admin_users():
        try:
            await context.bot.send_message(chat_id=uid, text=notif, parse_mode='HTML')
        except Exception as e:
            logger.error(f"Не удалось уведомить админа {uid}: {e}")


# --- Диалог настройки панели ---
SET_NAME, SET_URL, SET_USERNAME, SET_PASSWORD, SET_SUB_URL = range(5)


@audit_command("/setting")
@superadmin_only
async def setting_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Отменяем параллельный диалог /addclient
    _abort_conversation(update, _conv_addclient_ref)
    # Чистим его данные
    context.user_data.pop('ac', None)

    # Чистим свои данные от предыдущей попытки
    for key in ('panel_name', 'panel_url', 'panel_username',
                'panel_password', 'prompt_msg_id'):
        context.user_data.pop(key, None)

    await update.message.reply_text("Введите имя панели для добавления или обновления:")
    return SET_NAME


async def set_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['panel_name'] = update.message.text.strip()
    await update.message.reply_text("Введите URL панели (с префиксом, например https://ip:port/XXXXXX):")
    return SET_URL


async def set_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    url = update.message.text.strip()
    ok, err = config._validate_panel_url(url)
    if not ok:
        await update.message.reply_text(f"❌ {err}\nПопробуй снова:")
        return SET_URL
    context.user_data['panel_url'] = url
    await update.message.reply_text("Введите логин панели:")
    return SET_USERNAME


async def set_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['panel_username'] = update.message.text.strip()
    prompt = await update.message.reply_text(
        "Введите пароль панели.\n"
        "⚠️ Сообщение с паролем будет автоматически удалено из чата."
    )
    context.user_data['prompt_msg_id'] = prompt.message_id
    return SET_PASSWORD


async def set_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Сохраняем пароль ДО удаления сообщения
    context.user_data['panel_password'] = update.message.text.strip()

    # Удаляем сообщение с паролем
    try:
        await update.message.delete()
    except Exception as e:
        logger.warning(f"Не удалось удалить сообщение с паролем: {e}")

    # Удаляем сообщение-приглашение
    prompt_msg_id = context.user_data.pop('prompt_msg_id', None)
    if prompt_msg_id:
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id, message_id=prompt_msg_id
            )
        except Exception as e:
            logger.warning(f"Не удалось удалить приглашение с паролем: {e}")

    await update.message.reply_text(
        "🔒 Сообщение с паролем удалено из чата.\n"
        "Введите URL подписки (например https://ip:2096/sub), или /skip:"
    )
    return SET_SUB_URL


async def set_sub_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text.lower() in ("/skip", "skip", "пропустить"):
        sub_url = ""
    else:
        ok, err = config._validate_panel_url(text)
        if not ok:
            await update.message.reply_text(
                f"❌ sub_url: {err}\nВведи снова или /skip:"
            )
            return SET_SUB_URL
        sub_url = text

    name = context.user_data['panel_name']
    url = context.user_data['panel_url']
    username = context.user_data['panel_username']
    password = context.user_data['panel_password']

    await update.message.reply_text("Пытаюсь подключиться к панели...")

    async with XUIApi(url, username, password, sub_url=sub_url) as api:
        connected = await api.login()
    if connected:
        try:
            config.add_or_update_panel(name, url, username, password, sub_url=sub_url)
            await update.message.reply_text(f"✅ Панель '{name}' подключена! Конфигурация сохранена.")
        except ValueError as e:
            await update.message.reply_text(f"❌ Ошибка сохранения: {e}")
    else:
        await update.message.reply_text("❌ Не удалось подключиться! Проверьте данные и повторите через /setting.")
    return ConversationHandler.END


async def cancel_setting(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Настройка отменена.")
    return ConversationHandler.END


@audit_command("/report")
@admin_only
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    report_text = await _generate_daily_report_text()
    await update.message.reply_text(report_text, parse_mode='HTML')


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Логирует исключения в хендлерах. Отвечает админам и клиентам на их языке."""
    logger.error("Исключение при обработке апдейта:", exc_info=context.error)

    if not (isinstance(update, Update) and update.effective_message):
        return

    # По умолчанию — сообщение на туркменском (для клиентов)
    text = "⚠️ Içki ýalňyşlyk. Administratorda ýüz tut."

    # Админам — на русском
    if update.effective_user and config.is_admin(update.effective_user.id):
        text = "⚠️ Внутренняя ошибка. Администратор уже видит её в логах."

    try:
        await update.effective_message.reply_text(text)
    except Exception:
        pass


async def post_init(application: Application) -> None:
    """
    Глобальное меню команд.

    Показываем только базовые команды:
      - /start, /help, /policy.
      - клиентские ссылки и тарифы — в reply-клавиатуре.
      - админские команды — только в /help (не в меню).
    """
    commands = [
        BotCommand("start", "🚀 Başlamak"),
        BotCommand("help", "ℹ️ Kömek"),
    ]
    if config.get_policy_url():
        commands.append(BotCommand("policy", "🔒 Gizlinlik syýasaty"))
    await application.bot.set_my_commands(commands)


def main() -> None:
    bot_token = config.get_bot_token()
    if not bot_token or bot_token == "YOUR_TELEGRAM_BOT_TOKEN":
        logger.error("Токен бота не настроен в config.yml.")
        return

    application = Application.builder().token(bot_token).build()
    application.post_init = post_init
    application.add_error_handler(error_handler)

    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(check_inbounds_job, interval=timedelta(hours=6), first=timedelta(seconds=10))
        job_queue.run_daily(record_traffic_job, time=_scheduled_time(23, 50))
        report_hour = config.get_daily_report_hour()
        job_queue.run_daily(daily_report_job, time=_scheduled_time(report_hour))
        job_queue.run_daily(client_expiry_notification_job, time=_scheduled_time(9, 0))
        job_queue.run_daily(client_expired_notification_job, time=_scheduled_time(23, 59))
        job_queue.run_daily(admin_expired_notification_job, time=_scheduled_time(6, 0))
        job_queue.run_daily(auto_sync_job, time=_scheduled_time(0, 1))
    else:
        logger.warning("JobQueue не инициализирован.")

    # Диалог /setting
    conv_setting = ConversationHandler(
        entry_points=[CommandHandler("setting", setting_start)],
        states={
            SET_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_name)],
            SET_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_url)],
            SET_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_username)],
            SET_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_password)],
            SET_SUB_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_sub_url)],
        },
        fallbacks=[CommandHandler("cancel", cancel_setting)],
        name="setting",
    )

    # Диалог /addclient
    conv_addclient = ConversationHandler(
        entry_points=[CommandHandler("addclient", addclient_start)],
        states={
            AC_HWID: [
                CommandHandler("skip", addclient_hwid_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addclient_hwid),
            ],
            AC_EXPIRY: [
                CallbackQueryHandler(addclient_expiry_callback, pattern=r"^exp:\d+$"),
                CommandHandler("skip", addclient_expiry_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addclient_expiry_text),
            ],
            AC_COMMENT: [
                CommandHandler("skip", addclient_comment_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addclient_comment_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", addclient_cancel)],
        name="addclient",
    )

    # Регистрируем ссылки для автоотмены параллельных диалогов
    global _conv_setting_ref, _conv_addclient_ref
    _conv_setting_ref = conv_setting
    _conv_addclient_ref = conv_addclient

    # Callback-подтверждения — регистрируем ДО ConversationHandler
    application.add_handler(CallbackQueryHandler(confirm_callback, pattern=r"^confirm:"))
    application.add_handler(CallbackQueryHandler(apply_callback, pattern=r"^apply:submit$"))
    application.add_handler(CallbackQueryHandler(admin_action_callback, pattern=r"^admact:"))
    application.add_handler(CallbackQueryHandler(clients_nav_callback, pattern=r"^clients:"))
    application.add_handler(CallbackQueryHandler(client_help_callback, pattern=r"^client:help$"))
    application.add_handler(CallbackQueryHandler(getlink_callback, pattern=r"^getlink:"))

    # cancel_input — обычная команда, группа 0
    application.add_handler(CommandHandler("cancel_input", cancel_input_command))

    # pending_input_handler — в группе 1.
    # Вызывается ТОЛЬКО если группа 0 не обработала апдейт.
    # Все кнопки и команды обрабатываются в группе 0, сюда не доходят.
    # А вот текстовый ввод (после выбора клиента) никем в группе 0 не перехватывается
    # и попадает сюда.
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, pending_input_handler),
        group=1,
    )

    # ConversationHandler-ы — ПОСЛЕ pending_input_handler
    application.add_handler(conv_setting)
    application.add_handler(conv_addclient)

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("policy", policy_command))
    application.add_handler(CommandHandler("guide", guide_command))
    application.add_handler(CommandHandler("guideadmin", guide_admin_command))
    application.add_handler(CommandHandler("guidesuper", guide_super_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("inbounds", inbounds_command))
    application.add_handler(CommandHandler("mylink", mylink_command))
    application.add_handler(CommandHandler("revoke", revoke_command))
    application.add_handler(CommandHandler("listclients", listclients_command))
    application.add_handler(CommandHandler("getlink", getlink_command))
    application.add_handler(CommandHandler("setcomment", setcomment_command))
    application.add_handler(CommandHandler("rename", rename_command))
    application.add_handler(CommandHandler("sync", sync_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("pausesub", pausesub_command))
    application.add_handler(CommandHandler("resumesub", resumesub_command))
    application.add_handler(CommandHandler("extendsub", extendsub_command))
    application.add_handler(CommandHandler("delpanel", delpanel_command))
    application.add_handler(CommandHandler("listpanels", listpanels_command))
    application.add_handler(CommandHandler("report", report_command))

    application.add_handler(MessageHandler(
        filters.Regex("^🔗 Abuna salgysy$"), btn_my_link
    ))
    application.add_handler(MessageHandler(
        filters.Regex("^📊 Nyrhlar$"), btn_tariffs
    ))
    application.add_handler(MessageHandler(
        filters.Regex("^🆘 Kömek gerek$"), btn_help
    ))

    # Reply-кнопки админа
    application.add_handler(MessageHandler(
        filters.Regex("^➕ Добавить пользователя$"), btn_admin_add
    ))
    application.add_handler(MessageHandler(
        filters.Regex("^✏️ Переименовать$"), btn_admin_rename
    ))
    application.add_handler(MessageHandler(
        filters.Regex("^💬 Комментарий$"), btn_admin_comment
    ))
    application.add_handler(MessageHandler(
        filters.Regex("^📅 Продлить$"), btn_admin_extend
    ))
    application.add_handler(MessageHandler(
        filters.Regex("^🗑️ Удалить$"), btn_admin_revoke
    ))

    logger.info("Бот запущен...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()