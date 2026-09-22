import html
import logging
import uuid as uuid_module
from functools import wraps
from datetime import datetime, timedelta, time
from typing import Optional

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
)
from helpers import (
    _tz,
    _format_bytes, _make_sub_id,
    _validate_email,
    _get_panel_api, _check_panel_available,
    _find_bindings_for_admin,
    _days_to_expiry, _parse_expiry_input, _days_between, _parse_extend_argument,
    _client_reply_keyboard, _admin_reply_keyboard, _ask_confirm,
    _render_binding_line, _send_client_notice, _edit_query_safely,
    _admin_action_keyboard, _render_admin_action_page,
)
from jobs import (
    record_traffic_job, daily_report_job,
    check_inbounds_job, expiry_notification_job,
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
        "➕ **Чтобы добавить клиента**, введи команду:\n\n"
        "`/addclient <tg_id> <email> <панель> <id1> [id2] ...`\n\n"
        "**Пример:**\n"
        "`/addclient 123456789 ivan TMT 8 9`\n\n"
        "Список панелей: `/listpanels`\n"
        "Список инбаундов: `/inbounds TMT`",
        parse_mode='Markdown',
    )


async def _open_admin_action_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
) -> None:
    """Открывает список клиентов для админского действия."""
    bindings = list_all_bindings_with_users()
    if not bindings:
        await update.message.reply_text("Пока нет ни одного выданного клиента.")
        return

    text, keyboard = _render_admin_action_page(bindings, action, 0)
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=keyboard)


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
        return

    # admact:page:<action>:<page> — навигация
    if len(parts) == 4 and parts[1] == "page":
        action = parts[2]
        try:
            page = int(parts[3])
        except ValueError:
            return
        bindings = list_all_bindings_with_users()
        if not bindings:
            await query.edit_message_text("Пока нет ни одного выданного клиента.")
            return
        text, keyboard = _render_admin_action_page(bindings, action, page)
        try:
            await query.edit_message_text(text, parse_mode='Markdown', reply_markup=keyboard)
        except Exception as e:
            logger.debug(f"edit_message_text: {e}")
        return

    # admact:sel:<action>:<tg_id>:<panel> — выбор клиента
    if len(parts) == 5 and parts[1] == "sel":
        action = parts[2]
        try:
            tg_id = int(parts[3])
        except ValueError:
            return
        panel_name = parts[4]

        bindings = _find_bindings_for_admin(tg_id)
        bindings = [b for b in bindings if b["panel_name"] == panel_name]
        if not bindings:
            await query.edit_message_text(
                "Клиент не найден. Возможно, данные изменились."
            )
            return

        b = bindings[0]
        email = b["email"]

        if action == "pause":
            status = "уже на паузе" if b.get("paused_at") else "будет приостановлена"
            preview = (
                f"Поставить на паузу клиента `{tg_id}`:\n\n"
                f"Подписка `{email}` — {status}"
            )
            await _ask_confirm(
                query.message.chat_id, context, "pause",
                {"tg_id": tg_id, "bindings": bindings}, preview,
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
                {"tg_id": tg_id, "bindings": bindings}, preview,
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
                f"✏️ **Переименовать подписку**\n\n"
                f"Клиент: `{tg_id}`\n"
                f"Текущий email: `{email}`\n\n"
                f"**Введи новый email** в ответ на это сообщение.\n"
                f"Только латиница, цифры, точка, дефис, подчёркивание.\n\n"
                f"Отмена: `/cancel_input`",
                parse_mode='Markdown',
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
                f"💬 **Изменить комментарий**\n\n"
                f"Клиент: `{tg_id}`, подписка `{email}`\n\n"
                f"**Введи новый комментарий** в ответ на это сообщение.\n"
                f"Чтобы очистить — отправь `-`.\n\n"
                f"Отмена: `/cancel_input`",
                parse_mode='Markdown',
            )

        elif action == "extend":
            old = b.get("expiry_date") or "бессрочно"
            await query.edit_message_text(
                f"📅 **Продлить подписку**\n\n"
                f"Клиент: `{tg_id}`, подписка `{email}` (сейчас: {old})\n\n"
                f"Отправь в чат команду:\n"
                f"`/extendsub {tg_id} +30 {email}`\n\n"
                f"Или с конкретной датой:\n"
                f"`/extendsub {tg_id} 2027-01-01 {email}`",
                parse_mode='Markdown',
            )

        elif action == "revoke":
            preview_lines = [f"**Удалить клиента** `{tg_id}`:\n"]
            for bb in bindings:
                preview_lines.append(f"Подписка `{bb['email']}`")
            preview_lines.append("\n⚠️ Клиент будет **удалён из панели** и БД.")
            preview = "\n".join(preview_lines)
            await _ask_confirm(
                query.message.chat_id, context, "revoke",
                {"tg_id": tg_id, "bindings": bindings}, preview,
            )

        else:
            await query.edit_message_text(f"❓ Неизвестное действие: {action}")
        return

    logger.warning(f"Неизвестный admin_action callback: {data}")


async def pending_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ловит текст, когда админ вводит новый email или комментарий после кнопки."""
    pending = context.user_data.get("pending_input")
    if not pending:
        # Нет ожидающего ввода — игнорируем. Не отвечаем, чтобы не мешать другим хендлерам.
        return

    text = (update.message.text or "").strip()
    action = pending["action"]
    tg_id = pending["tg_id"]
    panel_name = pending["panel_name"]
    old_email = pending["email"]
    chat_id = update.effective_chat.id

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
        await context.bot.send_message(chat_id=chat_id, text=err)
        return

    if action == "rename":
        err = _validate_email(text)
        if err:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ {err}\nНачни заново через кнопку «✏️ Переименовать».")
            return
        new_email = text

        if get_binding_by_email(panel_name, new_email):
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Email `{new_email}` уже занят в БД на '{panel_name}'.",
                parse_mode='Markdown',
            )
            return

        result = None
        try:
            async with _get_panel_api(panel_name) as api:
                await api.login()
                existing = await api.get_client_object(new_email)
                if existing is not None:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"❌ Клиент с email `{new_email}` уже есть в панели.",
                        parse_mode='Markdown',
                    )
                    return
                result = await api.update_client(old_email, new_email=new_email)
        except Exception as e:
            logger.error(f"[admin={update.effective_user.id}] Ошибка rename '{panel_name}': {e}")

        if result is not True:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"❌ Не удалось переименовать.\n"
                    f"Возможно, клиента `{old_email}` нет в панели.\n"
                    f"Проверь через `/sync`."
                ),
                parse_mode='Markdown',
            )
            return

        db_ok = update_binding_email(tg_id, panel_name, old_email, new_email)
        if not db_ok:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Панель обновлена, но в БД ошибка. Запусти `/sync`.",
                parse_mode='Markdown',
            )
            return

        renamed = rename_traffic_email(panel_name, old_email, new_email)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ **Email изменён**\n\n"
                f"Было: `{old_email}`\n"
                f"Стало: `{new_email}`\n"
                f"Панель: `{panel_name}`\n"
                f"Записей трафика обновлено: {renamed}"
            ),
            parse_mode='Markdown',
        )

    elif action == "comment":
        new_comment = "" if text == "-" else text

        result = None
        try:
            async with _get_panel_api(panel_name) as api:
                await api.login()
                result = await api.update_client(old_email, comment=new_comment)
        except Exception as e:
            logger.error(f"[admin={update.effective_user.id}] Ошибка setcomment '{panel_name}/{old_email}': {e}")

        if result is not True:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"❌ Не удалось обновить комментарий.\n"
                    f"Возможно, клиента `{old_email}` нет в панели."
                ),
                parse_mode='Markdown',
            )
            return

        try:
            update_binding_comment(tg_id, panel_name, old_email, new_comment)
        except Exception as e:
            logger.error(f"Не удалось обновить комментарий в БД: {e}")

        shown = f"`{new_comment}`" if new_comment else "*(пустой)*"
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ Комментарий обновлён.\n\n"
                f"Панель: `{panel_name}`\n"
                f"Клиент: `{old_email}`\n"
                f"Комментарий: {shown}"
            ),
            parse_mode='Markdown',
        )


async def cancel_input_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отменяет ожидание ввода после кнопок переименования/комментария."""
    if context.user_data.pop("pending_input", None):
        await update.message.reply_text("Отменено.")
    else:
        await update.message.reply_text("Нечего отменять.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    policy_line = "`/policy` - 🔒 Gizlinlik syýasaty\n" if config.get_policy_url() else ""
    user_id = update.effective_user.id

    if config.is_admin(user_id):
        common = (
            "**✨ Команды администратора:**\n"
            "`/start` - 🚀 Начать работу с ботом\n"
            "`/help` - ℹ️ Показать эту справку\n"
            f"{policy_line}"
            "`/guideadmin` - 📚 Гайд для администратора\n"
            "`/listpanels` - 📋 Список панелей со статусом\n"
            "`/status <панель>` - 📊 Подробный статус панели\n"
            "`/inbounds <панель>` - 📡 Список инбаундов с ID\n"
            "`/addclient <tg_id> <email> <панель> <id1> [id2] ...` - 🆕 Создать клиента\n"
            "`/revoke <tg_id> [email]` - 🗑️ Удалить клиента\n"
            "`/pausesub <tg_id> [email]` - ⏸️ Приостановить подписку\n"
            "`/resumesub <tg_id> [email]` - ▶️ Возобновить подписку\n"
            "`/extendsub <tg_id> <+N | дата> [email]` - 📅 Продлить подписку\n"
            "`/listclients` - 📋 Список выданных клиентов\n"
            "`/getlink <tg_id> [email]` - 🔗 Получить sub-ссылку клиента\n"
            "`/setcomment <tg_id> <email> <текст>` - 💬 Изменить комментарий\n"
            "`/rename <tg_id> <старый> <новый>` - ✏️ Изменить email клиента\n"
            "`/report` - 📈 Отправить дневной отчёт сейчас"
        )

        if config.is_superadmin(user_id):
            common += (
                "\n\n**🔐 Только суперадмин:**\n"
                "`/guidesuper` - 📚 Гайд для суперадминистратора\n"
                "`/setting` - ⚙️ Добавить или обновить панель\n"
                "`/delpanel <имя>` - 🗑️ Удалить панель\n"
                "`/sync` - 🔄 Синхронизировать БД с панелью"
            )

        help_text = common
    else:
        help_text = (
            "**👋 Müşderi buýruklary:**\n"
            "`/start` - 🚀 Boty başlamak\n"
            "`/help` - ℹ️ Şu gollanmany görkezmek\n"
            f"{policy_line}"
            "`/guide` - 📚 Bot boýunça gollanma\n"
            "`/mylink` - 🔗 Abuna salgysyny almak"
        )

    await update.message.reply_text(help_text, parse_mode='Markdown')

    # Если пользователь — админ, дополнительно показываем клавиатуру
    if config.is_admin(user_id):
        await update.message.reply_text(
            "👇 Быстрый доступ:",
            reply_markup=_admin_reply_keyboard(),
        )


# --- Админские команды по панелям ---
@admin_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Подробный статус одной панели. Без аргумента — подсказка."""
    if not context.args:
        await update.message.reply_text(
            "Укажи панель: `/status <имя>`\n"
            "Обзор всех панелей: `/listpanels`",
            parse_mode='Markdown',
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
            f"**Статус панели {panel_name}**\n"
            f"- Версия Xray: `{xray_version}`\n"
            f"- Статус Xray: **{xray_status.capitalize()}**\n\n"
            f"**Состояние сервера**\n"
            f"- CPU: {cpu_percent:.2f}%\n"
            f"- Память: {_format_bytes(mem_current)} / {_format_bytes(mem_total)} ({mem_percent:.2f}%)\n"
            f"- Диск: {_format_bytes(disk_current)} / {_format_bytes(disk_total)} ({disk_percent:.2f}%)\n"
            f"- Время работы: {uptime_str}\n"
            f"**Сеть**\n"
            f"- Отдано: {_format_bytes(net_sent)}\n"
            f"- Принято: {_format_bytes(net_recv)}"
        )
        await update.message.reply_text(status_text, parse_mode='Markdown')
    else:
        await update.message.reply_text(f"Не удалось получить полный статус '{panel_name}'. Проверьте подключение или повторите позже.")


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

    lines = [f"**Инбаунды панели '{panel_name}':**\n"]
    for ib in inbounds:
        status = "✅" if ib.get("enable") else "⛔"
        lines.append(
            f"{status} ID `{ib['id']}` — {ib.get('remark', 'без имени')} "
            f"({ib.get('protocol', '?')}:{ib.get('port', '?')})"
        )
    lines.append("\nИспользуй ID в команде `/addclient`.")
    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')


@admin_only
async def listpanels_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Список всех панелей с их текущим статусом."""
    all_panels = config.get_all_panels()
    if not all_panels:
        await update.message.reply_text("Панели не настроены.")
        return

    await update.message.reply_text(f"Проверяю {len(all_panels)} панель(ей)...")

    lines = ["**Список панелей:**\n"]
    for name, pconf in all_panels.items():
        lines.append(f"\n**{name}** — `{pconf['url']}`")

        # Отключённые панели не дёргаем — сразу помечаем
        if pconf.get("disabled", False):
            lines.append("  ⛔ `отключена`")
            continue

        # Активные — опрашиваем статус Xray
        try:
            async with _get_panel_api(name) as api:
                status = await api.get_server_status()
            if status and 'xray' in status:
                xray_state = status['xray'].get('state', 'N/A')
                lines.append(f"  Xray: **{xray_state.capitalize()}**")
            else:
                lines.append("  Xray: `не удалось подключиться`")
        except Exception as e:
            logger.warning(f"Ошибка получения статуса '{name}': {e}")
            lines.append("  Xray: `ошибка запроса`")

    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')


@superadmin_only
async def delpanel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Формат: /delpanel <имя панели>")
        return
    panel_name = context.args[0]
    if config.delete_panel(panel_name):
        await update.message.reply_text(f"🗑️ Панель '{panel_name}' успешно удалена.")
    else:
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
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=keyboard)
    except Exception as e:
        # Telegram падает, если текст не изменился (нажали на ту же страницу)
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
            "📅 **До какого числа подписка?**\n\n"
            "Нажми кнопку, или введи дату в формате `ГГГГ-ММ-ДД`.\n"
            "`/skip` — бессрочная подписка."
        ),
        parse_mode='Markdown',
        reply_markup=_expiry_keyboard(),
    )


async def _ask_comment(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Задаёт вопрос о комментарии к клиенту."""
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "💬 **Комментарий к клиенту**\n\n"
            "Напиши что-нибудь для себя и других админов "
            "(например: «Иван с работы», «Друг Миши»).\n"
            "Клиент этот текст не увидит.\n\n"
            "Или `/skip`, чтобы пропустить."
        ),
        parse_mode='Markdown',
    )

# --- /addclient ---
AC_HWID = 1
AC_EXPIRY = 2
AC_COMMENT = 3


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
        f"- TG ID: `{tg_id}`\n"
        f"- Email: `{email}`\n"
        f"- Панель: `{panel_name}`\n"
        f"- Инбаунды: `{inbound_ids}`\n\n"
        f"Введи лимит HWID (0–10, где 0 = безлимит), или `/skip`:",
        parse_mode='Markdown',
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
    await update.message.reply_text(f"HWID лимит: `{hwid}`", parse_mode='Markdown')
    await _ask_expiry(update.effective_chat.id, context)
    return AC_EXPIRY


async def addclient_hwid_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка /skip на шаге HWID — лимит 0, переход к дате."""
    if "ac" not in context.user_data:
        await update.message.reply_text("Сессия потеряна. Начни заново /addclient.")
        return ConversationHandler.END
    context.user_data["ac"]["hwid"] = 0
    await update.message.reply_text("HWID лимит: `0` (безлимит)", parse_mode='Markdown')
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
        f"⏳ Срок: до *{expiry_date_str}* ({days} дн.)",
        parse_mode='Markdown',
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
        await update.message.reply_text(f"⏳ Срок: до *{expiry_date_str}*", parse_mode='Markdown')
    else:
        await update.message.reply_text("⏳ Срок: *бессрочно*", parse_mode='Markdown')

    await _ask_comment(update.effective_chat.id, context)
    return AC_COMMENT


async def addclient_expiry_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка /skip на шаге даты — бессрочно."""
    context.user_data["ac"]["expiry_ts"] = 0
    context.user_data["ac"]["expiry_date_str"] = None
    await update.message.reply_text("⏳ Срок: *бессрочно*", parse_mode='Markdown')
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

    expiry_line = f"до `{expiry_date_str}`" if expiry_date_str else "`бессрочно`"
    comment_line = f"\n- 💬 Комментарий: {comment}" if comment else ""

    preview = (
        f"**Создать клиента:**\n\n"
        f"- TG ID: `{tg_id}`\n"
        f"- Email: `{email}`\n"
        f"- Панель: `{panel_name}`\n"
        f"- Инбаунды: `{inbound_ids}`\n"
        f"- HWID лимит: `{hwid}`\n"
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

    preview_lines = [f"Поставить на паузу клиента `{tg_id}`:\n"]
    for b in bindings:
        status = "уже на паузе" if b.get("paused_at") else "будет приостановлена"
        preview_lines.append(f"Подписка `{b['email']}` — {status}")

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
    lines = ["**Ставлю на паузу:**\n"]
    any_paused = False

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
            lines.append(_render_binding_line(panel_name, email, "✅ приостановлена"))
            any_paused = True
        elif result is False:
            lines.append(_render_binding_line(panel_name, email, "❌ клиента нет в панели"))
        else:
            lines.append(_render_binding_line(panel_name, email, "❌ ошибка связи с панелью"))

    await _edit_query_safely(query, "\n".join(lines))

    if any_paused:
        await _send_client_notice(
            context, tg_id,
            "⏸️ **Abuna saklandy**\n\n"
            "Saklanyş günleri harç edilmeýär. "
            "Täzeden işledeniňde — möhlet awtomatik uzaldylar.\n\n"
            "Administrator bilen habarlaşmak: 🆘 Kömek gerek"
        )

# --- /resumesub ---


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

    preview_lines = [f"Возобновить подписку клиента `{tg_id}`:\n"]
    for b in bindings:
        if b.get("paused_at"):
            preview_lines.append(f"Подписка `{b['email']}` — будет возобновлена")
        else:
            preview_lines.append(f"Подписка `{b['email']}` — не на паузе")

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
    lines = ["**Снимаю с паузы:**\n"]
    resumed = False

    for b in bindings:
        panel_name = b["panel_name"]
        email = b["email"]
        paused_at = b.get("paused_at")

        if not paused_at:
            lines.append(_render_binding_line(panel_name, email, "не на паузе"))
            continue

        pause_days = _days_between(paused_at.split()[0], today_str)

        old_expiry = b.get("expiry_date")

        # Бессрочная подписка: срок не трогаем — пауза на него не влияет
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
            # Бессрочная — срок не меняем
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
            if new_expiry_str:
                update_binding_expiry(tg_id, panel_name, email, new_expiry_str)
            lines.append(_render_binding_line(panel_name, email, resumed_status, line_extra))
            resumed = True
        elif result is False:
            lines.append(_render_binding_line(panel_name, email, "❌ клиента нет в панели"))
        else:
            lines.append(_render_binding_line(panel_name, email, "❌ ошибка связи с панелью"))

    await _edit_query_safely(query, "\n".join(lines))

    if resumed:
        await _send_client_notice(
            context, tg_id,
            "▶️ **Abuna täzeden işledildi.** Möhlet saklanyş günlerine uzaldylar."
        )


# --- /extendsub ---
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

    preview_lines = [f"Продлить подписку клиента `{tg_id}`:\n"]
    if kind == "days":
        preview_lines.append(f"Аргумент: **+{value} дней**\n")
    else:
        preview_lines.append(f"Аргумент: **до {value}**\n")
    for b in bindings:
        old = b.get("expiry_date") or "бессрочно"
        preview_lines.append(f"Подписка `{b['email']}` (сейчас: {old})")

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
    lines = ["**Продлеваю подписку:**\n"]
    last_new_expiry_str = None

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
        elif result is False:
            lines.append(_render_binding_line(panel_name, email, "❌ клиента нет в панели"))
        else:
            lines.append(_render_binding_line(panel_name, email, "❌ ошибка связи с панелью"))

    await query.edit_message_text("\n".join(lines), parse_mode='Markdown')

    if last_new_expiry_str:
        try:
            await context.bot.send_message(
                chat_id=tg_id,
                text=f"🎉 **Abuna uzaldylar.** Täze möhlet: {last_new_expiry_str} çenli.",
                parse_mode='Markdown',
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить клиента {tg_id} о продлении: {e}")


# --- /revoke и /listclients ---
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

    preview_lines = [f"**Удалить клиента** `{tg_id}`:\n"]
    for b in bindings:
        preview_lines.append(f"Подписка `{b['email']}`")
    preview_lines.append("\n⚠️ Клиент будет **удалён из панели** и БД.")

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
    lines = ["**Удаляю клиентов:**\n"]

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

        mark_panel = "✅" if panel_ok else "❌"
        mark_db = "✅" if db_ok else "❌"
        lines.append(f"- `{panel_name}/{email}` — панель: {mark_panel}, БД: {mark_db}")

    await _edit_query_safely(query, "\n".join(lines))

# --- /getlink ---


@admin_only
async def getlink_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Выдаёт sub-ссылку клиента админу по запросу."""
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

    lines = [f"🔗 **Ссылки клиента** `{tg_id}`:\n"]
    for b in bindings:
        panel_name = b["panel_name"]
        email = b["email"]
        panel_config = config.get_panel_config(panel_name)
        sub_url = panel_config.get("sub_url", "").rstrip("/")
        status = "⏸️ приостановлена" if b.get("paused_at") else "▶️ активна"

        # Проверяем клиента в панели: если его там нет — предупреждаем админа
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
            lines.append(f"**{panel_name}** ({email}) — {status}:\n{link}{warn_line}\n")
        else:
            lines.append(
                f"**{panel_name}** ({email}) — {status}: sub_url не настроен\n"
            )

    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')


# ---------- /setcomment ----------
@admin_only
async def setcomment_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Меняет комментарий клиента — и в БД, и в панели."""
    if len(context.args) < 3:
        await update.message.reply_text(
            "Формат: /setcomment <tg_id> <email> <новый комментарий>\n\n"
            "Пустой комментарий — используй `-` вместо текста.",
            parse_mode='Markdown',
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

    bindings = _find_bindings_for_admin(tg_id, email)
    if not bindings:
        await update.message.reply_text(
            f"Связка `{email}` у клиента {tg_id} не найдена.",
            parse_mode='Markdown',
        )
        return

    b = bindings[0]
    panel_name = b["panel_name"]

    err = _check_panel_available(panel_name)
    if err:
        await update.message.reply_text(err)
        return

    result = None
    try:
        async with _get_panel_api(panel_name) as api:
            await api.login()
            result = await api.update_client(email, comment=new_comment)
    except Exception as e:
        logger.error(f"[admin={update.effective_user.id}] Ошибка setcomment '{panel_name}/{email}': {e}")

    if result is True:
        try:
            update_binding_comment(tg_id, panel_name, email, new_comment)
        except Exception as e:
            logger.error(f"Не удалось обновить комментарий в БД: {e}")
        shown = f"`{new_comment}`" if new_comment else "*(пустой)*"
        await update.message.reply_text(
            f"✅ Комментарий обновлён.\n\n"
            f"Панель: `{panel_name}`\n"
            f"Клиент: `{email}`\n"
            f"Комментарий: {shown}",
            parse_mode='Markdown',
        )
    elif result is False:
        await update.message.reply_text("❌ Клиента нет в панели.")
    else:
        await update.message.reply_text("❌ Ошибка связи с панелью.")


# ---------- /rename ----------
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

    err = _validate_email(new_email)
    if err:
        await update.message.reply_text(err)
        return

    bindings = _find_bindings_for_admin(tg_id, old_email)
    if not bindings:
        await update.message.reply_text(
            f"Связка `{old_email}` у клиента {tg_id} не найдена.",
            parse_mode='Markdown',
        )
        return

    b = bindings[0]
    panel_name = b["panel_name"]

    err = _check_panel_available(panel_name)
    if err:
        await update.message.reply_text(err)
        return

    # Проверка в БД
    if get_binding_by_email(panel_name, new_email):
        await update.message.reply_text(
            f"❌ Клиент с email `{new_email}` уже существует в БД на '{panel_name}'.",
            parse_mode='Markdown',
        )
        return

    # Проверка в панели + смена email
    result = None
    try:
        async with _get_panel_api(panel_name) as api:
            await api.login()
            existing = await api.get_client_object(new_email)
            if existing is not None:
                await update.message.reply_text(
                    f"❌ Клиент с email `{new_email}` уже есть в панели.",
                    parse_mode='Markdown',
                )
                return
            result = await api.update_client(old_email, new_email=new_email)
    except Exception as e:
        logger.error(f"[admin={update.effective_user.id}] Ошибка rename '{panel_name}': {e}")

    if result is not True:
        if result is False:
            await update.message.reply_text(
                f"❌ Клиента `{old_email}` нет в панели.",
                parse_mode='Markdown',
            )
        else:
            await update.message.reply_text("❌ Ошибка связи с панелью.")
        return

    # Обновляем БД
    db_ok = update_binding_email(tg_id, panel_name, old_email, new_email)
    if not db_ok:
        logger.error(f"БД: не удалось переименовать {old_email} -> {new_email}")
        await update.message.reply_text(
            "⚠️ Панель обновлена, но в БД ошибка.\nЗапусти `/sync` для восстановления.",
            parse_mode='Markdown',
        )
        return

    renamed = rename_traffic_email(panel_name, old_email, new_email)

    await update.message.reply_text(
        f"✅ **Email изменён**\n\n"
        f"Было: `{old_email}`\n"
        f"Стало: `{new_email}`\n"
        f"Панель: `{panel_name}`\n"
        f"Записей трафика обновлено: {renamed}",
        parse_mode='Markdown',
    )


# ---------- /sync ----------
@superadmin_only
async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Синхронизирует БД из панели: email, комментарий, HWID, срок действия."""
    await update.message.reply_text("🔄 Запускаю синхронизацию. Это займёт несколько секунд...")

    all_panels = config.get_all_panels()
    if not all_panels:
        await update.message.reply_text("Панели не настроены.")
        return

    total_updated = 0
    total_renamed = 0
    total_missing = 0
    lines = []

    for panel_name, pconf in all_panels.items():
        if pconf.get("disabled", False):
            lines.append(f"⏭️ **{panel_name}**: отключена")
            continue

        # 1. Все клиенты панели
        try:
            async with _get_panel_api(panel_name) as api:
                await api.login()
                inbounds_data = await api.get_inbounds()
        except Exception as e:
            logger.error(f"[sync] Ошибка '{panel_name}': {e}")
            lines.append(f"❌ **{panel_name}**: ошибка связи")
            continue

        if not inbounds_data or not inbounds_data.get("success"):
            lines.append(f"❌ **{panel_name}**: не удалось получить данные")
            continue

        # 2. Карта UUID → {email, comment, limitHwid, expiryTime}
        # В 3.8.x UUID клиента — это поле "id". Поле "uuid" не используется.
        panel_clients = {}
        for inbound in inbounds_data.get("obj", []):
            settings = _parse_settings(inbound.get("settings"))
            if not settings:
                continue
            for client in settings.get("clients", []) or []:
                c_uuid = client.get("id") or client.get("uuid") or ""
                if c_uuid:
                    panel_clients[c_uuid] = {
                        "email": client.get("email") or "",
                        "comment": client.get("comment") or "",
                        "limitHwid": int(client.get("limitHwid") or 0),
                        "expiryTime": int(client.get("expiryTime") or 0),
                    }

        # 3. Обходим связки панели
        panel_bindings = [b for b in list_all_bindings() if b["panel_name"] == panel_name]

        panel_upd = 0
        panel_ren = 0
        panel_miss = 0

        for b in panel_bindings:
            b_uuid = b.get("uuid") or ""
            if not b_uuid or b_uuid not in panel_clients:
                panel_miss += 1
                logger.warning(
                    f"[sync] {panel_name}: потеряна связка "
                    f"tg_id={b['tg_id']}, email={b['email']}, uuid={b_uuid or '(пусто)'}"
                )
                continue

            pc = panel_clients[b_uuid]
            old_email = b["email"]
            new_email = pc["email"]

            # 3.1. Смена email
            if new_email and new_email != old_email:
                db_ok = update_binding_email(b["tg_id"], panel_name, old_email, new_email)
                if db_ok:
                    renamed = rename_traffic_email(panel_name, old_email, new_email)
                    logger.info(
                        f"[sync] {panel_name}: '{old_email}' -> '{new_email}' "
                        f"({renamed} записей трафика)"
                    )
                    panel_ren += 1
                    total_renamed += 1
                else:
                    logger.warning(f"[sync] Не удалось переименовать {old_email} -> {new_email}")

            # Для дальнейших проверок используем актуальный email
            current_email = new_email or old_email

            # 3.2. Комментарий
            if pc["comment"] != (b.get("comment") or ""):
                try:
                    update_binding_comment(b["tg_id"], panel_name, current_email, pc["comment"])
                    panel_upd += 1
                except Exception as e:
                    logger.error(f"[sync] Ошибка обновления комментария: {e}")

            # 3.3. HWID
            if pc["limitHwid"] != (b.get("limit_hwid") or 0):
                try:
                    update_binding_limit_hwid(b["tg_id"], panel_name, current_email, pc["limitHwid"])
                    panel_upd += 1
                except Exception as e:
                    logger.error(f"[sync] Ошибка обновления HWID: {e}")

            # 3.4. Срок
            new_expiry = None
            if pc["expiryTime"] > 0:
                try:
                    new_expiry = datetime.fromtimestamp(
                        pc["expiryTime"] / 1000, _tz()
                    ).strftime("%Y-%m-%d")
                except (ValueError, OSError):
                    new_expiry = None
            if (new_expiry or "") != (b.get("expiry_date") or ""):
                try:
                    update_binding_expiry(b["tg_id"], panel_name, current_email, new_expiry)
                    panel_upd += 1
                except Exception as e:
                    logger.error(f"[sync] Ошибка обновления expiry: {e}")

        total_updated += panel_upd
        total_missing += panel_miss

        lines.append(
            f"✅ **{panel_name}**: обновлено {panel_upd}, "
            f"переименовано {panel_ren}, потеряно {panel_miss}"
        )

    lines.append(
        f"\n**Итого:** обновлено {total_updated}, "
        f"переименовано {total_renamed}, не найдено в панели {total_missing}"
    )
    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')


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

    client_uuid = str(uuid_module.uuid4())
    sub_id = _make_sub_id()

    async with _get_panel_api(panel_name) as api:
        ok = await api.login()
        if not ok:
            await query.edit_message_text("❌ Не удалось подключиться к панели.")
            logger.error(f"[admin={update.effective_user.id}] Не удалось залогиниться в панель '{panel_name}'")
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
        await query.edit_message_text("❌ Не удалось создать клиента. Проверь логи бота.")
        logger.error(f"[admin={update.effective_user.id}] Не удалось создать клиента '{email}' на '{panel_name}'")
        return

    # Подстраховка: sub_url мог быть снят между шагами диалога
    if not sub_link:
        await query.edit_message_text(
            "⚠️ Клиент создан в панели, но sub_url у панели не настроен.\n"
            "Настрой sub_url через /setting и выдай ссылку клиенту вручную через /getlink."
        )
        return

    # Сохранение в БД
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

    # Уведомление клиенту
    # Уведомление клиенту
    expiry_line_client = (
        f"Möhlet: **{expiry_date_str}** çenli"
        if expiry_date_str else "Möhlet: **möhletsiz**"
    )
    delivered = True
    try:
        await context.bot.send_message(
            chat_id=tg_id,
            text=(
                f"🎉 **Seniň abunaň taýýar!**\n\n"
                f"Panel: **{panel_name}**\n"
                f"Login: `{email}`\n"
                f"{expiry_line_client}\n\n"
                f"**Abuna salgysy:**\n{sub_link}\n\n"
                f"Salgyny bas — brauzer açylar. "
                f"Göçürmek üçin, barmagyňy salgynyň üstünde sakla."
            ),
            parse_mode='Markdown',
            reply_markup=keyboard,
        )
    except Exception as e:
        delivered = False
        logger.error(f"Не удалось отправить клиенту {tg_id}: {e}")

    # Отчёт админу
    comment_line = f"💬 Комментарий: `{comment}`\n" if comment else ""
    expiry_line_admin = f"до {expiry_date_str}" if expiry_date_str else "бессрочно"

    admin_msg = (
        f"✅ **Клиент создан**\n\n"
        f"TG ID: `{tg_id}`\n"
        f"Email: `{email}`\n"
        f"Панель: `{panel_name}`\n"
        f"Инбаунды: `{inbound_ids}`\n"
        f"HWID лимит: `{hwid}`\n"
        f"UUID: `{client_uuid}`\n"
        f"Sub ID: `{sub_id}`\n"
        f"Срок: {expiry_line_admin}\n"
        f"{comment_line}"
    )
    if not delivered:
        admin_msg += "\n\n⚠️ Не удалось доставить клиенту — возможно, он не запускал бота."

    await query.edit_message_text(admin_msg, parse_mode='Markdown')

CLIENTS_PAGE_SIZE = 5


def _format_client_binding(b: dict) -> str:
    """Форматирует одну связку клиента для /listclients."""
    username = b.get("bot_username")
    first_name = b.get("bot_first_name")
    if username:
        user_line = f"👤 `@{username}`"
    elif first_name:
        user_line = f"👤 {first_name}"
    else:
        user_line = "👤 `без username`"

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
    else:
        status_icon = "▶️"

    lines = [
        user_line,
        f"✏️ `{email}`",
        f"🎛️ `{panel_name}`",
        f"🆔 `{tg_id}`",
        f"📳 HWID `{hwid_str}`",
        f"{status_icon} до `{expiry}`",
    ]
    if b.get("comment"):
        lines.append(f"💬 _{b['comment']}_")
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

    lines = [f"👥 **Список клиентов** (страница {page + 1}/{total_pages}):\n"]
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
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=keyboard)

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
            message, parse_mode='Markdown', reply_markup=keyboard,
        )
    except Exception as e:
        logger.warning(f"Markdown для /policy не сработал: {e}. Отправляю как plain text.")
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
            message, parse_mode='Markdown', reply_markup=keyboard,
        )
    except Exception as e:
        logger.warning(f"Markdown для гайда '{role}' не сработал: {e}. Отправляю как plain text.")
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

    lines = ["🔗 **Seniň abuna salgylaryň:**\n"]
    for b in active:
        panel_config = config.get_panel_config(b["panel_name"])
        sub_url = panel_config.get("sub_url", "").rstrip("/")
        if sub_url:
            link = f"{sub_url}/{b['sub_id']}"
            lines.append(f"**{b['panel_name']}** ({b['email']}):\n{link}\n")
        else:
            lines.append(f"**{b['panel_name']}** ({b['email']}): sub_url sazlanmady\n")

    if paused:
        lines.append("⏸️ *Käbir abunalar saklandy. Täzeden işletmek üçin 🆘 Kömek gerek düwmesine bas.*")

    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')


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
            message, parse_mode='Markdown', reply_markup=keyboard,
        )
    except Exception as e:
        logger.warning(f"Markdown для тарифов не сработал: {e}. Отправляю как plain text.")
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
        f"🆘 **Клиент просит помощи**\n\n"
        f"- Имя: {full_name}\n"
        f"- Username: {username}\n"
        f"- TG ID: `{user.id}`"
    )
    for uid in config.get_admin_users():
        try:
            await context.bot.send_message(
                chat_id=uid, text=notif, parse_mode='Markdown',
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить админа {uid}: {e}")


# --- Диалог настройки панели ---
SET_NAME, SET_URL, SET_USERNAME, SET_PASSWORD, SET_SUB_URL = range(5)


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


@admin_only
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    report_text = await _generate_daily_report_text()
    await update.message.reply_text(report_text, parse_mode='Markdown')


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Логирует исключения в хендлерах и говорит пользователю, что что-то сломалось."""
    logger.error("Исключение при обработке апдейта:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Внутренняя ошибка. Администратор уже видит её в логах."
            )
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
        BotCommand("start", "🚀 Начать работу с ботом"),
        BotCommand("help", "ℹ️ Справка"),
    ]
    if config.get_policy_url():
        commands.append(BotCommand("policy", "🔒 Политика конфиденциальности"))
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
        job_queue.run_daily(expiry_notification_job, time=_scheduled_time(9, 0))
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
    application.add_handler(CommandHandler("cancel_input", cancel_input_command))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, pending_input_handler
    ))


if __name__ == "__main__":
    main()
