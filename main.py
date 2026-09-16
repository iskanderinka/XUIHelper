import logging
import uuid as uuid_module
import secrets
import string
import json
from functools import wraps
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    ConversationHandler,
)

import config
import database
from xui_api import XUIApi
from query_logic import query_user_data
from database import (
    init_db, batch_record_traffic, cleanup_old_traffic,
    get_daily_stats, get_panel_daily_stats, get_top_users, has_daily_traffic_snapshot,
    upsert_bot_user, is_bot_user,
    save_binding, get_user_bindings, get_binding_by_email,
    delete_binding, list_all_bindings,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# Эти библиотеки слишком болтливы — INFO у них на каждый HTTP-запрос
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("xui_api").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

SCHEDULE_TIMEZONE = ZoneInfo("Asia/Hong_Kong")


def _scheduled_time(hour: int, minute: int = 0) -> time:
    """Создаёт время ежедневной задачи в часовом поясе сервиса."""
    return time(hour=hour, minute=minute, tzinfo=SCHEDULE_TIMEZONE)


def _make_sub_id(length: int = 16) -> str:
    """Генерирует subId: строчные буквы и цифры."""
    alphabet = string.ascii_lowercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


# Инициализируем БД при старте
init_db()

# --- Ограничение частоты запросов (для /mystatus) ---
failed_query_attempts = {}
blocked_users = {}


# --- Вспомогательные функции ---
def _format_bytes(size: int) -> str:
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
    return round(size / (1024 ** 3), 2)


def _get_panel_api(panel_name: str) -> XUIApi:
    """Создаёт клиент XUIApi по имени панели."""
    panel_config = config.get_panel_config(panel_name)
    return XUIApi(
        url=panel_config.get("url", ""),
        username=panel_config.get("username", ""),
        password=panel_config.get("password", ""),
        sub_url=panel_config.get("sub_url", ""),
    )


# --- Декораторы ---
def authorized(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not config.is_authorized(update.effective_user.id):
            await update.message.reply_text("Извините, у вас нет прав на использование этого бота.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not config.is_admin(update.effective_user.id):
            await update.message.reply_text("Извините, эта команда доступна только администраторам.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


# --- Базовые команды ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    # Регистрируем пользователя в bot_users
    try:
        upsert_bot_user(user.id, user.username or "", user.first_name or "")
    except Exception as e:
        logger.error(f"Не удалось записать bot_user {user.id}: {e}")

    await update.message.reply_html(
        rf"Привет, {user.mention_html()}! "
        f"Твой Telegram ID: <code>{user.id}</code>\n\n"
        f"Если ты клиент — админ выдаст тебе подписку, и она придёт в этот чат автоматически.\n"
        f"Используй /help для списка команд.",
    )


@authorized
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if config.is_admin(update.effective_user.id):
        help_text = (
            "**✨ Команды администратора:**\n"
            "/start - 🚀 Начать работу с ботом\n"
            "/help - ℹ️ Показать эту справку\n"
            "/setting - ⚙️ Добавить или обновить панель\n"
            "/inbounds <панель> - 📡 Список инбаундов с ID\n"
            "/addclient <tg_id> <email> <панель> <id1> [id2] ... - 🆕 Создать клиента\n"
            "/revoke <tg_id> [email] - 🗑️ Удалить клиента\n"
            "/listclients - 📋 Список выданных клиентов\n"
            "/delpanel <имя> - 🗑️ Удалить панель\n"
            "/listpanels - 📋 Список всех панелей\n"
            "/status <имя> - 📊 Статус панели (без имени — все)\n"
            "/adduser <ID> - ✅ Добавить обычного пользователя\n"
            "/deluser <ID> - ❌ Удалить обычного пользователя\n"
            "/listusers - 👥 Список авторизованных пользователей\n"
            "/setresetday <панель> <день> - 🔧 День сброса трафика (1-28)\n"
            "/report - 📈 Отправить дневной отчёт сейчас\n"
            "/resetpanel <панель> - ⚡️ Немедленно сбросить трафик панели"
        )
    else:
        help_text = (
            "**👋 Команды пользователя:**\n"
            "/start - 🚀 Начать работу с ботом\n"
            "/help - ℹ️ Показать эту справку\n"
            "/mylink - 🔗 Получить ссылку подписки\n"
            "/mystatus - 📊 Мой трафик и срок действия"
        )
    await update.message.reply_text(help_text, parse_mode='Markdown')


# --- Админские команды по панелям ---
@admin_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    panel_name = context.args[0] if context.args else None

    if not panel_name:
        all_panels = config.get_all_panels()
        if not all_panels:
            await update.message.reply_text("Панели не настроены. Используйте /setting для настройки.")
            return

        status_messages = ["**Обзор статуса всех панелей:**"]
        for name, panel_config in all_panels.items():
            if panel_config.get("disabled", False):
                status_messages.append(f"- **{name}**: `отключена`")
                continue
            async with _get_panel_api(name) as api:
                status = await api.get_server_status()
            if status and 'xray' in status:
                xray_status = status['xray'].get('state', 'N/A')
                status_messages.append(f"- **{name}**: {xray_status.capitalize()}")
            else:
                status_messages.append(f"- **{name}**: `не удалось подключиться`")

        await update.message.reply_text("\n".join(status_messages), parse_mode='Markdown')
        return

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

        reset_day = config.get_panel_reset_day(panel_name)
        reset_info = f"- День сброса трафика: {reset_day}-е число" if reset_day else "- День сброса трафика: 1-е число (по умолчанию)"

        status_text = (
            f"**Статус панели {panel_name}**\n"
            f"- Версия Xray: `{xray_version}`\n"
            f"- Статус Xray: **{xray_status.capitalize()}**\n\n"
            f"**Состояние сервера**\n"
            f"- CPU: {cpu_percent:.2f}%\n"
            f"- Память: {_format_bytes(mem_current)} / {_format_bytes(mem_total)} ({mem_percent:.2f}%)\n"
            f"- Диск: {_format_bytes(disk_current)} / {_format_bytes(disk_total)} ({disk_percent:.2f}%)\n"
            f"- Время работы: {uptime_str}\n"
            f"{reset_info}\n\n"
            f"**Сеть**\n"
            f"- Отдано: {_format_bytes(net_sent)}\n"
            f"- Принято: {_format_bytes(net_recv)}"
        )
        await update.message.reply_text(status_text, parse_mode='Markdown')
    else:
        await update.message.reply_text(f"Не удалось получить полный статус '{panel_name}'. Проверьте подключение или повторите позже.")


@admin_only
async def inbounds_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает список инбаундов панели с их ID (чтобы админ знал, что вводить)."""
    if not context.args:
        await update.message.reply_text("Формат: /inbounds <имя панели>")
        return
    panel_name = context.args[0]
    if not config.get_panel_config(panel_name):
        await update.message.reply_text(f"Панель '{panel_name}' не найдена.")
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
    all_panels = config.get_all_panels()
    if not all_panels:
        await update.message.reply_text("Панели не настроены.")
        return
    message = "**Список настроенных панелей:**\n\n"
    for name, pconf in all_panels.items():
        reset_day = pconf.get("reset_day", "1 (по умолчанию)")
        disabled_tag = " ⛔отключена" if pconf.get("disabled", False) else ""
        message += f"- **{name}**{disabled_tag}: `{pconf['url']}`\n  День сброса: {reset_day}-е число\n"
    await update.message.reply_text(message, parse_mode='Markdown')


@admin_only
async def delpanel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Формат: /delpanel <имя панели>")
        return
    panel_name = context.args[0]
    if config.delete_panel(panel_name):
        await update.message.reply_text(f"🗑️ Панель '{panel_name}' успешно удалена.")
    else:
        await update.message.reply_text(f"Панель '{panel_name}' не найдена.")


@admin_only
async def setresetday_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await update.message.reply_text("Формат: /setresetday <имя панели> <день (1-28)>")
        return
    panel_name = context.args[0]
    try:
        day = int(context.args[1])
    except ValueError:
        await update.message.reply_text("День должен быть целым числом от 1 до 28.")
        return
    if day < 1 or day > 28:
        await update.message.reply_text("День должен быть в диапазоне 1–28.")
        return
    panel_config = config.get_panel_config(panel_name)
    if not panel_config:
        await update.message.reply_text(f"Панель '{panel_name}' не найдена.")
        return
    config.add_or_update_panel(
        panel_name, panel_config["url"], panel_config["username"],
        panel_config["password"], reset_day=day,
        sub_url=panel_config.get("sub_url", ""),
    )
    await update.message.reply_text(f"✅ День сброса трафика панели '{panel_name}' установлен на {day}-е число.")


@admin_only
async def resetpanel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Формат: /resetpanel <имя панели>")
        return
    panel_name = context.args[0]
    if not config.get_panel_config(panel_name):
        await update.message.reply_text(f"Панель '{panel_name}' не найдена.")
        return
    await update.message.reply_text(f"Сбрасываю трафик панели '{panel_name}'...")
    initiator = update.effective_user
    init_name = initiator.full_name or initiator.username or str(initiator.id)
    async with _get_panel_api(panel_name) as api:
        success = await api.reset_all_client_traffic()
    if success:
        await update.message.reply_text(f"✅ Трафик панели '{panel_name}' успешно сброшен!")
        msg = f"✅ **{panel_name}**: трафик сброшен! (вручную, инициатор: {init_name})"
    else:
        await update.message.reply_text(f"❌ Не удалось сбросить трафик панели '{panel_name}'!")
        msg = f"❌ **{panel_name}**: не удалось сбросить трафик! (вручную, инициатор: {init_name})"
    for uid in config.get_admin_users():
        if uid == update.effective_user.id:
            continue
        try:
            await context.bot.send_message(chat_id=uid, text=msg, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Не удалось уведомить администратора {uid}: {e}")


# --- Управление пользователями бота ---
@admin_only
async def adduser_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Формат: /adduser <ID пользователя>")
        return
    user_id_to_add = int(context.args[0])
    if config.add_normal_user(user_id_to_add):
        await update.message.reply_text(f"✅ Обычный пользователь {user_id_to_add} добавлен!")
    else:
        await update.message.reply_text(f"Пользователь {user_id_to_add} уже существует.")


@admin_only
async def deluser_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Формат: /deluser <ID пользователя>")
        return
    user_id_to_del = int(context.args[0])
    if config.del_normal_user(user_id_to_del):
        await update.message.reply_text(f"🗑️ Обычный пользователь {user_id_to_del} удалён.")
    else:
        await update.message.reply_text(f"Пользователь {user_id_to_del} не найден.")


@admin_only
async def listusers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin_users = config.get_admin_users()
    normal_users = config.get_normal_users()
    message = "**Список авторизованных пользователей**\n\n**Администраторы:**\n"
    for uid in admin_users:
        message += f"- `{uid}`\n"
    message += "\n**Обычные пользователи:**\n"
    if not normal_users:
        message += "нет"
    else:
        for uid in normal_users:
            message += f"- `{uid}`\n"
    await update.message.reply_text(message, parse_mode='Markdown')


# --- /addclient ---
AC_HWID = 1  # состояние ConversationHandler


@admin_only
async def addclient_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
    except ValueError:
        await update.message.reply_text("TG ID должен быть числом.")
        return ConversationHandler.END

    email = args[1].strip()
    panel_name = args[2].strip()

    # Парсим ID инбаундов
    try:
        inbound_ids = [int(x) for x in args[3:]]
    except ValueError:
        await update.message.reply_text("ID инбаундов должны быть числами.")
        return ConversationHandler.END

    # Проверки
    if not is_bot_user(tg_id):
        await update.message.reply_text(
            f"❌ Пользователь {tg_id} не запускал бота.\n"
            f"Попроси его нажать /start."
        )
        return ConversationHandler.END

    panel_config = config.get_panel_config(panel_name)
    if not panel_config:
        await update.message.reply_text(f"❌ Панель '{panel_name}' не найдена.")
        return ConversationHandler.END

    if get_binding_by_email(panel_name, email):
        await update.message.reply_text(
            f"❌ Клиент с email '{email}' уже существует в базе на панели '{panel_name}'."
        )
        return ConversationHandler.END

    # Проверка, что инбаунды существуют (через API)
    async with _get_panel_api(panel_name) as api:
        ok = await api.login()
        if not ok:
            await update.message.reply_text("❌ Не удалось подключиться к панели.")
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
        f"Введи лимит HWID (целое число >= 0), или `/skip` для безлимита:",
        parse_mode='Markdown',
    )
    return AC_HWID


async def addclient_hwid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text.lower() in ("/skip", "skip", "пропустить"):
        hwid = 0
    else:
        try:
            hwid = int(text)
            if hwid < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Нужно целое число >= 0, или /skip.")
            return AC_HWID

    data = context.user_data.pop("ac", None)
    if not data:
        await update.message.reply_text("Сессия потеряна. Начни заново.")
        return ConversationHandler.END

    panel_name = data["panel_name"]
    tg_id = data["tg_id"]
    email = data["email"]
    inbound_ids = data["inbound_ids"]

    client_uuid = str(uuid_module.uuid4())
    sub_id = _make_sub_id()

    async with _get_panel_api(panel_name) as api:
        ok = await api.login()
        if not ok:
            await update.message.reply_text("❌ Не удалось подключиться к панели.")
            return ConversationHandler.END

        created = await api.create_client(
            email=email,
            client_uuid=client_uuid,
            sub_id=sub_id,
            inbound_ids=inbound_ids,
            limit_hwid=hwid,
            tg_id=tg_id,
        )
        sub_link = api.get_client_sub_link(sub_id)

    if not created:
        await update.message.reply_text("❌ Не удалось создать клиента. Проверь логи бота.")
        return ConversationHandler.END

    # Сохраняем в БД
    try:
        save_binding(
            tg_id=tg_id,
            panel_name=panel_name,
            email=email,
            inbound_ids=inbound_ids,
            sub_id=sub_id,
            uuid=client_uuid,
            limit_hwid=hwid,
        )
    except Exception as e:
        logger.error(f"Не удалось сохранить связку: {e}")

    # Отправляем клиенту
    delivered = True
    try:
        await context.bot.send_message(
            chat_id=tg_id,
            text=(
                f"🎉 **Твоя подписка готова!**\n\n"
                f"Панель: **{panel_name}**\n"
                f"Логин: `{email}`\n\n"
                f"**Ссылка подписки:**\n{sub_link}\n\n"
                f"Кликни по ссылке — откроется браузер. "
                f"Чтобы скопировать, удерживай палец на ссылке."
            ),
            parse_mode='Markdown',
        )
    except Exception as e:
        delivered = False
        logger.error(f"Не удалось отправить клиенту {tg_id}: {e}")

    # Ответ админу
    admin_msg = (
        f"✅ **Клиент создан**\n\n"
        f"TG ID: `{tg_id}`\n"
        f"Email: `{email}`\n"
        f"Панель: `{panel_name}`\n"
        f"Инбаунды: `{inbound_ids}`\n"
        f"HWID лимит: `{hwid}`\n"
        f"UUID: `{client_uuid}`\n\n"
        f"**Sub link:**\n`{sub_link}`"
    )
    if not delivered:
        admin_msg += "\n\n⚠️ Не удалось доставить клиенту — возможно, он не запускал бота."

    await update.message.reply_text(admin_msg, parse_mode='Markdown')
    return ConversationHandler.END


async def addclient_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("ac", None)
    await update.message.reply_text("Создание клиента отменено.")
    return ConversationHandler.END


# --- /revoke и /listclients ---
@admin_only
async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Формат: /revoke <tg_id> [email]")
        return

    try:
        tg_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("TG ID должен быть числом.")
        return

    email_filter = context.args[1].strip() if len(context.args) > 1 else None

    bindings = get_user_bindings(tg_id)
    if email_filter:
        bindings = [b for b in bindings if b["email"] == email_filter]

    if not bindings:
        await update.message.reply_text("Не найдено связок для удаления.")
        return

    lines = ["**Удаляю клиентов:**\n"]
    for b in bindings:
        panel_name = b["panel_name"]
        email = b["email"]

        # Удаляем из панели
        panel_ok = False
        try:
            async with _get_panel_api(panel_name) as api:
                ok = await api.login()
                if ok:
                    panel_ok = await api.delete_client(email)
        except Exception as e:
            logger.error(f"Ошибка удаления из панели '{panel_name}/{email}': {e}")

        # Удаляем из БД
        db_ok = delete_binding(tg_id, panel_name, email)

        mark_panel = "✅" if panel_ok else "❌"
        mark_db = "✅" if db_ok else "❌"
        lines.append(f"- `{panel_name}/{email}` — панель: {mark_panel}, БД: {mark_db}")

    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')


@admin_only
async def listclients_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bindings = list_all_bindings()
    if not bindings:
        await update.message.reply_text("Пока нет ни одного выданного клиента.")
        return

    lines = ["**Выданные клиенты:**\n"]
    for b in bindings:
        lines.append(
            f"- `{b['panel_name']}/{b['email']}` — TG `{b['tg_id']}`, "
            f"HWID `{b['limit_hwid']}`, инбаунды `{b['inbound_ids']}`"
        )
    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')


# --- Клиентские команды ---
@authorized
async def mylink_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет клиенту его sub-ссылку(-и)."""
    tg_id = update.effective_user.id
    bindings = get_user_bindings(tg_id)
    if not bindings:
        await update.message.reply_text(
            "У тебя пока нет выданных подписок. Обратись к администратору."
        )
        return

    lines = ["🔗 **Твои ссылки подписки:**\n"]
    for b in bindings:
        panel_config = config.get_panel_config(b["panel_name"])
        sub_url = panel_config.get("sub_url", "").rstrip("/")
        if sub_url:
            link = f"{sub_url}/{b['sub_id']}"
            lines.append(f"**{b['panel_name']}** ({b['email']}):\n`{link}`\n")
        else:
            lines.append(f"**{b['panel_name']}** ({b['email']}): sub_url не настроен\n")

    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')


@authorized
async def mystatus_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает клиенту его трафик по каждой связке."""
    tg_id = update.effective_user.id
    bindings = get_user_bindings(tg_id)
    if not bindings:
        await update.message.reply_text(
            "У тебя пока нет выданных подписок. Обратись к администратору."
        )
        return

    accounting_mode = config.get_accounting_mode()
    lines = ["📊 **Твой трафик:**\n"]

    for b in bindings:
        success, result = await query_user_data(b["panel_name"], b["email"])
        if not success:
            lines.append(f"**{b['panel_name']}** ({b['email']}): ⚠️ {result}\n")
            continue

        used_gb = result["used_gb"]
        total_gb = result["total_gb"]
        if accounting_mode == "bidirectional":
            try:
                used_gb = f"{float(used_gb) * 2:.2f}"
                total_gb = f"{float(total_gb) * 2:.2f}"
            except (ValueError, TypeError):
                pass

        lines.append(
            f"**{b['panel_name']}** ({b['email']}):\n"
            f"- Трафик: {used_gb} GB / {total_gb} GB\n"
            f"- Срок действия: {result['expiry_date']}\n"
        )

    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')


# --- Диалог настройки панели ---
SET_NAME, SET_URL, SET_USERNAME, SET_PASSWORD, SET_SUB_URL = range(5)


@admin_only
async def setting_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
    # Сохраняем пароль в память диалога ДО удаления сообщения
    context.user_data['panel_password'] = update.message.text.strip()

    # Удаляем сообщение с паролем
    try:
        await update.message.delete()
    except Exception as e:
        logger.warning(f"Не удалось удалить сообщение с паролем: {e}")

    # Удаляем сообщение-приглашение "Введите пароль панели"
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


# --- Задачи по расписанию ---
async def record_traffic_job(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневная задача: снимок трафика клиентов всех панелей в БД."""
    logger.info("Запуск задачи: record_traffic_job")
    all_panels = config.get_all_panels()
    if not all_panels:
        logger.warning("record_traffic_job пропущено: панели не настроены.")
        return
    today = datetime.now(SCHEDULE_TIMEZONE).strftime("%Y-%m-%d")
    records = []
    for name, pconf in all_panels.items():
        if pconf.get("disabled", False):
            continue
        async with _get_panel_api(name) as api:
            clients = await api.get_all_clients()
        for c in clients:
            if not c["email"]:
                continue
            records.append((name, c["email"], c["up"], c["down"],
                            c["total"], c["expiryTime"], today))
    batch_record_traffic(records)
    cleanup_old_traffic()
    logger.info(f"Записано {len(records)} записей трафика за {today}.")


def _format_daily_report_text(report_date: str, stats: list,
                              panel_stats: list, top_users_by_panel: dict) -> str:
    """Форматирует дневной отчёт: список пользователей отдельно по каждой панели."""
    if not stats and not panel_stats:
        return (
            f"📊 **Дневной отчёт по трафику ({report_date})**\n\n"
            "**Данные за день недоступны**\n"
            "- Причина: отсутствует снимок трафика за предыдущий день, точный расход посчитать нельзя."
        )

    total_upload = sum(s["total_upload"] for s in stats)
    total_download = sum(s["total_download"] for s in stats)
    total_traffic = total_upload + total_download

    lines = [
        f"📊 **Дневной отчёт по трафику ({report_date})**\n",
        f"**Общий расход**: {_bytes_to_gb(total_traffic)} GB",
        f"  - Отдано: {_bytes_to_gb(total_upload)} GB",
        f"  - Принято: {_bytes_to_gb(total_download)} GB\n",
    ]

    if panel_stats:
        lines.append("**Расход по панелям:**")
        for ps in panel_stats:
            lines.append(f"  - {ps['panel_name']}: {_bytes_to_gb(ps['daily_total'])} GB")
        lines.append("")

    for panel_name in (ps["panel_name"] for ps in panel_stats):
        panel_users = top_users_by_panel.get(panel_name, [])
        if not panel_users:
            continue
        lines.append(f"**{panel_name}: Топ 10 пользователей по расходу:**")
        for i, user in enumerate(panel_users[:10], 1):
            lines.append(
                f"  {i}. {user['email']} ({panel_name}): "
                f"{_bytes_to_gb(user['total_usage'])} GB"
            )
        lines.append("")

    return "\n".join(lines).rstrip()


async def _generate_daily_report_text() -> str:
    """Собирает дневной отчёт из базы данных."""
    report_day = datetime.now(SCHEDULE_TIMEZONE).date() - timedelta(days=1)
    yesterday = report_day.strftime("%Y-%m-%d")
    baseline_day = (report_day - timedelta(days=1)).strftime("%Y-%m-%d")

    if not has_daily_traffic_snapshot(yesterday) or not has_daily_traffic_snapshot(baseline_day):
        return _format_daily_report_text(yesterday, [], [], {})

    stats = get_daily_stats(yesterday, yesterday)
    panel_stats = get_panel_daily_stats(yesterday, yesterday)
    top_users_by_panel = {
        ps["panel_name"]: get_top_users(
            yesterday, yesterday, panel_name=ps["panel_name"], limit=10
        )
        for ps in panel_stats
    }

    return _format_daily_report_text(yesterday, stats, panel_stats, top_users_by_panel)


async def daily_report_job(context: ContextTypes.DEFAULT_TYPE):
    """Отправляет дневной отчёт всем админам."""
    logger.info("Запуск задачи: daily_report_job")
    if not config.is_daily_report_enabled():
        logger.info("Дневной отчёт отключён, пропускаем.")
        return

    report_text = await _generate_daily_report_text()
    for uid in config.get_admin_users():
        try:
            await context.bot.send_message(chat_id=uid, text=report_text, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Не удалось отправить отчёт {uid}: {e}")


@admin_only
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    report_text = await _generate_daily_report_text()
    await update.message.reply_text(report_text, parse_mode='Markdown')


async def check_inbounds_job(context: ContextTypes.DEFAULT_TYPE):
    """Проверяет истекающие инбаунды и статус панелей."""
    logger.info("Запуск задачи: check_inbounds_job")
    all_panels = config.get_all_panels()
    if not all_panels:
        logger.warning("Задача пропущена: панели не настроены.")
        return
    admin_users = config.get_admin_users()
    for name, pconf in all_panels.items():
        if pconf.get("disabled", False):
            continue
        async with _get_panel_api(name) as api:
            if not await api.login():
                logger.error(f"Панель '{name}': не удалось подключиться. Отправляем алерт.")
                for uid in admin_users:
                    try:
                        await context.bot.send_message(
                            chat_id=uid, text=f"🚨 **Панель '{name}' недоступна**",
                            parse_mode='Markdown'
                        )
                    except Exception:
                        pass
                continue
            inbounds_data = await api.get_inbounds()
        if inbounds_data and inbounds_data.get("success"):
            three_days_later = (datetime.now(SCHEDULE_TIMEZONE) + timedelta(days=3)).timestamp() * 1000
            for inbound in inbounds_data.get("obj", []):
                expiry_ts = inbound.get("expiryTime", 0)
                if 0 < expiry_ts < three_days_later:
                    expiry_date = datetime.fromtimestamp(
                        expiry_ts / 1000, SCHEDULE_TIMEZONE
                    ).strftime('%Y-%m-%d')
                    message = (
                        f"🔔 **Напоминание об истечении ({name})** 🔔\n"
                        f"- Описание: {inbound.get('remark', 'N/A')}\n"
                        f"- Истекает: {expiry_date}"
                    )
                    for uid in admin_users:
                        try:
                            await context.bot.send_message(
                                chat_id=uid, text=message, parse_mode='Markdown'
                            )
                        except Exception:
                            pass


async def traffic_reset_job(context: ContextTypes.DEFAULT_TYPE):
    """Проверяет каждую панель на её день сброса и сбрасывает при необходимости."""
    logger.info("Запуск задачи: traffic_reset_job")
    all_panels = config.get_all_panels()
    if not all_panels:
        return
    admin_users = config.get_admin_users()
    today = datetime.now(SCHEDULE_TIMEZONE)
    for name, pconf in all_panels.items():
        if pconf.get("disabled", False):
            continue
        reset_day = config.get_panel_reset_day(name)
        if reset_day is None:
            if not config.is_monthly_reset_enabled():
                continue
            reset_day = 1
        if today.day != reset_day:
            continue
        logger.info(f"Сбрасываю трафик панели '{name}' в день {reset_day}.")
        async with _get_panel_api(name) as api:
            reset_success = await api.reset_all_client_traffic()
        if reset_success:
            msg = f"✅ **{name}**: трафик сброшен! (день сброса: {reset_day})"
            logger.info(f"Успешный сброс трафика панели: {name}")
        else:
            msg = f"❌ **{name}**: не удалось сбросить трафик!"
            logger.error(f"Ошибка сброса трафика панели: {name}")
        for uid in admin_users:
            try:
                await context.bot.send_message(chat_id=uid, text=msg, parse_mode='Markdown')
            except Exception:
                pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Логирует любые исключения в хендлерах и говорит пользователю, что что-то сломалось."""
    logger.error("Исключение при обработке апдейта:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Внутренняя ошибка. Администратор уже видит её в логах."
            )
        except Exception:
            pass


async def post_init(application: Application) -> None:
    commands = [
        BotCommand("start", "🚀 Начать работу с ботом"),
        BotCommand("help", "ℹ️ Справка"),
        BotCommand("mylink", "🔗 Ссылка подписки (клиенты)"),
        BotCommand("mystatus", "📊 Мой трафик (клиенты)"),
        BotCommand("setting", "⚙️ Добавить/обновить панель (админ)"),
        BotCommand("inbounds", "📡 Список инбаундов панели (админ)"),
        BotCommand("addclient", "🆕 Создать клиента (админ)"),
        BotCommand("revoke", "🗑️ Удалить клиента (админ)"),
        BotCommand("listclients", "📋 Список выданных клиентов (админ)"),
        BotCommand("status", "📊 Статус панели (админ)"),
        BotCommand("listpanels", "📋 Список панелей (админ)"),
        BotCommand("delpanel", "🗑️ Удалить панель (админ)"),
        BotCommand("adduser", "✅ Добавить пользователя (админ)"),
        BotCommand("deluser", "❌ Удалить пользователя (админ)"),
        BotCommand("listusers", "👥 Список пользователей (админ)"),
        BotCommand("setresetday", "🔧 День сброса панели (админ)"),
        BotCommand("resetpanel", "⚡️ Сбросить трафик панели (админ)"),
        BotCommand("report", "📈 Отправить дневной отчёт (админ)"),
    ]
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
        job_queue.run_daily(traffic_reset_job, time=_scheduled_time(0, 5))
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
    )

    # Диалог /addclient
    conv_addclient = ConversationHandler(
        entry_points=[CommandHandler("addclient", addclient_start)],
        states={
            AC_HWID: [MessageHandler(filters.TEXT & ~filters.COMMAND, addclient_hwid)],
        },
        fallbacks=[CommandHandler("cancel", addclient_cancel)],
    )

    application.add_handler(conv_setting)
    application.add_handler(conv_addclient)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("inbounds", inbounds_command))
    application.add_handler(CommandHandler("mylink", mylink_command))
    application.add_handler(CommandHandler("mystatus", mystatus_command))
    application.add_handler(CommandHandler("revoke", revoke_command))
    application.add_handler(CommandHandler("listclients", listclients_command))
    application.add_handler(CommandHandler("adduser", adduser_command))
    application.add_handler(CommandHandler("deluser", deluser_command))
    application.add_handler(CommandHandler("listusers", listusers_command))
    application.add_handler(CommandHandler("delpanel", delpanel_command))
    application.add_handler(CommandHandler("listpanels", listpanels_command))
    application.add_handler(CommandHandler("setresetday", setresetday_command))
    application.add_handler(CommandHandler("resetpanel", resetpanel_command))
    application.add_handler(CommandHandler("report", report_command))

    logger.info("Бот запущен...")
    application.run_polling()


if __name__ == "__main__":
    main()