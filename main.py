import logging
import subprocess
import sys
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
from xui_api import XUIApi
from database import (
    init_db, batch_record_traffic, cleanup_old_traffic,
    get_daily_stats, get_panel_daily_stats, get_top_users, has_daily_traffic_snapshot,
    record_query_log,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

SCHEDULE_TIMEZONE = ZoneInfo("Asia/Hong_Kong")


def _scheduled_time(hour: int, minute: int = 0) -> time:
    """Создаёт время ежедневной задачи в часовом поясе сервиса."""
    return time(hour=hour, minute=minute, tzinfo=SCHEDULE_TIMEZONE)


# Инициализируем БД при старте
init_db()

# --- Ограничение частоты запросов ---
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


# --- Обработчики бота ---
@authorized
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_html(
        rf"Привет, {user.mention_html()}! "
        f"Добро пожаловать в бот управления панелями 3x-ui. Используйте /help для списка команд.",
    )


@authorized
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if config.is_admin(update.effective_user.id):
        help_text = (
            "**✨ Команды администратора:**\n"
            "/start - 🚀 Начать работу с ботом\n"
            "/help - ℹ️ Показать эту справку\n"
            "/setting - ⚙️ Добавить или обновить панель\n"
            "/delpanel <имя> - 🗑️ Удалить панель по имени\n"
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
            "/query <панель> <имя> - 🔍 Информация о ноде"
        )
    await update.message.reply_text(help_text, parse_mode='Markdown')


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
            async with XUIApi(panel_config["url"], panel_config["username"], panel_config["password"]) as api:
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

    async with XUIApi(panel_config["url"], panel_config["username"], panel_config["password"]) as api:
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


from query_logic import query_user_data


@authorized
async def query_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if user_id in blocked_users:
        unblock_time = blocked_users[user_id]
        if datetime.now() < unblock_time:
            remaining_time = unblock_time - datetime.now()
            await update.message.reply_text(
                f"Слишком частые запросы. Временная блокировка. Повторите через {int(remaining_time.total_seconds() / 60)} мин.")
            return
        else:
            del blocked_users[user_id]
            if user_id in failed_query_attempts:
                del failed_query_attempts[user_id]

    if len(context.args) < 2:
        await update.message.reply_text("Укажите имя панели и имя пользователя. Формат: /query <панель> <имя>")
        return

    panel_name, query_user = context.args[0], context.args[1]
    await update.message.reply_text(f"Ищу на '{panel_name}', подождите...")

    success, result = await query_user_data(panel_name, query_user)
    try:
        record_query_log("telegram", update.effective_user.id, panel_name, query_user, success)
    except Exception:
        pass

    if success:
        if user_id in failed_query_attempts:
            del failed_query_attempts[user_id]
        accounting_mode = config.get_accounting_mode()
        used_gb = result['used_gb']
        total_gb = result['total_gb']
        if accounting_mode == "bidirectional":
            try:
                used_gb = float(used_gb) * 2
                total_gb = float(total_gb) * 2
            except (ValueError, TypeError):
                pass
        try:
            used_gb_formatted = f"{float(used_gb):.2f}"
            total_gb_formatted = f"{float(total_gb):.2f}"
        except (ValueError, TypeError):
            used_gb_formatted = used_gb
            total_gb_formatted = total_gb

        reply_text = (
            f"**Информация о ноде пользователя {result['email']} на панели '{result['panel_name']}':**\n"
            f"- Трафик: {used_gb_formatted} GB / {total_gb_formatted} GB\n"
            f"- Срок действия: {result['expiry_date']}"
        )
        await update.message.reply_text(reply_text, parse_mode='Markdown')
    else:
        await update.message.reply_text(result)
        now = datetime.now()
        if user_id not in failed_query_attempts:
            failed_query_attempts[user_id] = []
        failed_query_attempts[user_id].append(now)
        five_minutes_ago = now - timedelta(minutes=5)
        failed_query_attempts[user_id] = [
            t for t in failed_query_attempts[user_id] if t > five_minutes_ago
        ]
        if len(failed_query_attempts[user_id]) >= 5:
            block_duration = timedelta(hours=2)
            blocked_users[user_id] = now + block_duration
            await update.message.reply_text("Слишком частые запросы несуществующих пользователей. Блокировка на 2 часа.")
            del failed_query_attempts[user_id]


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
async def setresetday_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Устанавливает день сброса трафика для конкретной панели."""
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
        panel_config["password"], reset_day=day
    )
    await update.message.reply_text(f"✅ День сброса трафика панели '{panel_name}' установлен на {day}-е число.")


@admin_only
async def resetpanel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сбрасывает трафик для конкретной панели вручную."""
    if not context.args:
        await update.message.reply_text("Формат: /resetpanel <имя панели>")
        return
    panel_name = context.args[0]
    panel_config = config.get_panel_config(panel_name)
    if not panel_config:
        await update.message.reply_text(f"Панель '{panel_name}' не найдена.")
        return
    await update.message.reply_text(f"Сбрасываю трафик панели '{panel_name}'...")
    initiator = update.effective_user
    init_name = initiator.full_name or initiator.username or str(initiator.id)
    async with XUIApi(panel_config["url"], panel_config["username"], panel_config["password"]) as api:
        success = await api.reset_all_client_traffic()
    if success:
        await update.message.reply_text(f"✅ Трафик панели '{panel_name}' успешно сброшен!")
        msg = f"✅ **{panel_name}**: трафик сброшен! (вручную, инициатор: {init_name})"
    else:
        await update.message.reply_text(f"❌ Не удалось сбросить трафик панели '{panel_name}'!")
        msg = f"❌ **{panel_name}**: не удалось сбросить трафик! (вручную, инициатор: {init_name})"
    # Рассылаем результат остальным админам (инициатор уже получил ответ выше)
    for uid in config.get_admin_users():
        if uid == update.effective_user.id:
            continue
        try:
            await context.bot.send_message(chat_id=uid, text=msg, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Не удалось уведомить администратора {uid}: {e}")


# --- Диалог настройки ---
SET_NAME, SET_URL, SET_USERNAME, SET_PASSWORD = range(4)


@admin_only
async def setting_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Введите имя панели для добавления или обновления:")
    return SET_NAME


async def set_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['panel_name'] = update.message.text.strip()
    await update.message.reply_text("Введите URL панели 3x-ui:")
    return SET_URL


async def set_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['panel_url'] = update.message.text.strip()
    await update.message.reply_text("Введите логин панели:")
    return SET_USERNAME


async def set_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['panel_username'] = update.message.text.strip()
    await update.message.reply_text("Введите пароль панели:")
    return SET_PASSWORD


async def set_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['panel_password'] = update.message.text.strip()
    name = context.user_data['panel_name']
    url = context.user_data['panel_url']
    username = context.user_data['panel_username']
    password = context.user_data['panel_password']
    await update.message.reply_text("Пытаюсь подключиться к панели...")

    async with XUIApi(url, username, password) as api:
        connected = await api.login()
    if connected:
        config.add_or_update_panel(name, url, username, password)
        await update.message.reply_text(f"✅ Панель '{name}' подключена! Конфигурация сохранена.")
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
        async with XUIApi(pconf["url"], pconf["username"], pconf["password"]) as api:
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

    # Для кумулятивного счётчика нужны снимки конца дня за оба дня.
    if not has_daily_traffic_snapshot(yesterday) or not has_daily_traffic_snapshot(baseline_day):
        return _format_daily_report_text(yesterday, [], [], {})

    # Используем данные за вчера (полный день).
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
    admin_users = config.get_admin_users()
    for uid in admin_users:
        try:
            await context.bot.send_message(chat_id=uid, text=report_text, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Не удалось отправить отчёт {uid}: {e}")


@admin_only
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ручной запуск дневного отчёта."""
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
        async with XUIApi(pconf["url"], pconf["username"], pconf["password"]) as api:
            if not await api.login():
                logger.error(f"Панель '{name}': не удалось подключиться. Отправляем алерт.")
                for uid in admin_users:
                    await context.bot.send_message(chat_id=uid, text=f"🚨 **Панель '{name}' недоступна**", parse_mode='Markdown')
                continue
            inbounds_data = await api.get_inbounds()
        if inbounds_data and inbounds_data.get("success"):
            three_days_later = (datetime.now(SCHEDULE_TIMEZONE) + timedelta(days=3)).timestamp() * 1000
            for inbound in inbounds_data.get("obj", []):
                expiry_ts = inbound.get("expiryTime", 0)
                if 0 < expiry_ts < three_days_later:
                    expiry_date = datetime.fromtimestamp(expiry_ts / 1000, SCHEDULE_TIMEZONE).strftime('%Y-%m-%d')
                    message = f"🔔 **Напоминание об истечении ({name})** 🔔\n- Описание: {inbound.get('remark', 'N/A')}\n- Истекает: {expiry_date}"
                    for uid in admin_users:
                        await context.bot.send_message(chat_id=uid, text=message, parse_mode='Markdown')


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
            # Проверка глобального ежемесячного сброса
            if not config.is_monthly_reset_enabled():
                continue
            reset_day = 1
        if today.day != reset_day:
            continue
        logger.info(f"Сбрасываю трафик панели '{name}' в день {reset_day}.")
        async with XUIApi(pconf["url"], pconf["username"], pconf["password"]) as api:
            reset_success = await api.reset_all_client_traffic()
        if reset_success:
            msg = f"✅ **{name}**: трафик сброшен! (день сброса: {reset_day})"
            logger.info(f"Успешный сброс трафика панели: {name}")
        else:
            msg = f"❌ **{name}**: не удалось сбросить трафик!"
            logger.error(f"Ошибка сброса трафика панели: {name}")
        for uid in admin_users:
            await context.bot.send_message(chat_id=uid, text=msg, parse_mode='Markdown')


async def post_init(application: Application) -> None:
    commands = [
        BotCommand("start", "🚀 Начать работу с ботом"),
        BotCommand("help", "ℹ️ Справка"),
        BotCommand("query", "🔍 Информация о ноде (пользователи)"),
        BotCommand("setting", "⚙️ Добавить/обновить панель (админ)"),
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


def run_web_app():
    logger.info("Запуск веб-приложения через Gunicorn...")
    command = ["gunicorn", "--workers", "2", "--timeout", "120", "--bind", "0.0.0.0:5000", "webapp:app"]
    try:
        subprocess.Popen(command)
        logger.info("Веб-приложение успешно запущено.")
    except FileNotFoundError:
        logger.error("Gunicorn не найден.")
        sys.exit(1)


def main() -> None:
    bot_token = config.get_bot_token()
    if not bot_token or bot_token == "YOUR_TELEGRAM_BOT_TOKEN":
        logger.error("Токен бота не настроен в config.yml.")
        return

    application = Application.builder().token(bot_token).build()
    application.post_init = post_init

    run_web_app()

    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(check_inbounds_job, interval=timedelta(hours=6), first=timedelta(seconds=10))
        job_queue.run_daily(record_traffic_job, time=_scheduled_time(23, 50))
        report_hour = config.get_daily_report_hour()
        job_queue.run_daily(daily_report_job, time=_scheduled_time(report_hour))
        job_queue.run_daily(traffic_reset_job, time=_scheduled_time(0, 5))
    else:
        logger.warning("JobQueue не инициализирован.")

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("setting", setting_start)],
        states={
            SET_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_name)],
            SET_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_url)],
            SET_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_username)],
            SET_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel_setting)],
    )
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("query", query_command))
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