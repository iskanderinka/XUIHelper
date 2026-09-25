# =============================================================================
# jobs.py — задачи по расписанию
# =============================================================================
#
# Содержит:
#   1. record_traffic_job        — ежедневный снимок трафика
#   2. daily_report_job          — ежедневный отчёт админам
#   3. check_inbounds_job        — проверка связи с панелями и истечений
#   4. expiry_notification_job   — напоминания клиентам (7/3 дня) и алерт админам
#
# Также содержит хелперы, используемые только этими задачами:
#   - _format_daily_report_text
#   - _generate_daily_report_text
#   - _send_client_expiry_notice
#   - _notify_admins_expired
#
# =============================================================================

import logging
from datetime import datetime, timedelta

from telegram.ext import ContextTypes

import config
from database import (
    batch_record_traffic, cleanup_old_traffic,
    get_daily_stats, get_panel_daily_stats, get_top_users, has_daily_traffic_snapshot,
    list_all_bindings,
    has_recent_notification, log_notification,
    update_binding_email, update_binding_comment,
    update_binding_limit_hwid, update_binding_expiry,
    update_binding_enabled, rename_traffic_email,
)
from helpers import _bytes_to_gb, _tz, _get_panel_api
from xui_api import _parse_settings

logger = logging.getLogger(__name__)


# ---------- Ежедневный снимок трафика ----------

async def record_traffic_job(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневная задача: снимок трафика клиентов всех панелей в БД."""
    logger.info("Запуск задачи: record_traffic_job")
    all_panels = config.get_all_panels()
    if not all_panels:
        logger.warning("record_traffic_job пропущено: панели не настроены.")
        return

    today = datetime.now(_tz()).strftime("%Y-%m-%d")
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


# ---------- Дневной отчёт ----------

def _format_daily_report_text(report_date: str, stats: list,
                              panel_stats: list, top_users_by_panel: dict) -> str:
    """Форматирует дневной отчёт: список пользователей отдельно по каждой панели."""
    if not stats and not panel_stats:
        return (
            f"📊 **Дневной отчёт по трафику ({report_date})**\n\n"
            "**Данные за день недоступны**\n"
            "- Причина: отсутствует снимок трафика за предыдущий день, "
            "точный расход посчитать нельзя."
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
    report_day = datetime.now(_tz()).date() - timedelta(days=1)
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
    """Отправляет дневной отчёт всем админам (если включён в config.yml)."""
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


# ---------- Проверка связи с панелями и истечений инбаундов ----------

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
                            chat_id=uid,
                            text=f"🚨 **Панель '{name}' недоступна**",
                            parse_mode='Markdown'
                        )
                    except Exception:
                        pass
                continue
            inbounds_data = await api.get_inbounds()

        if inbounds_data and inbounds_data.get("success"):
            three_days_later = (
                datetime.now(_tz()) + timedelta(days=3)
            ).timestamp() * 1000

            for inbound in inbounds_data.get("obj", []):
                expiry_ts = inbound.get("expiryTime", 0)
                if 0 < expiry_ts < three_days_later:
                    expiry_date = datetime.fromtimestamp(
                        expiry_ts / 1000, _tz()
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


# ---------- Напоминания клиентам об истечении подписки ----------

async def _send_client_expiry_notice(
    context: ContextTypes.DEFAULT_TYPE,
    tg_id: int,
    panel_name: str,
    email: str,
    expiry_str: str,
    days_left: int,
) -> None:
    """Отправляет клиенту напоминание о скором истечении подписки."""
    text = (
        f"⏰ **Abuna barada ýatlatma**\n\n"
        f"Seniň abunaň **{days_left} gün** içinde gutarýar — "
        f"**{expiry_str}** çenli.\n\n"
        f"Möhleti wagtynda uzaltmak barada alada et."
    )
    try:
        await context.bot.send_message(chat_id=tg_id, text=text, parse_mode='Markdown')
    except Exception as e:
        logger.warning(f"Не удалось отправить напоминание клиенту {tg_id}: {e}")


async def _notify_admins_expired(
    context: ContextTypes.DEFAULT_TYPE,
    admin_users: list,
    tg_id: int,
    panel_name: str,
    email: str,
    expiry_str: str,
) -> None:
    """Отправляет всем админам уведомление об истёкшей подписке."""
    text = (
        f"❌ **Подписка истекла**\n\n"
        f"Клиент: `{tg_id}`\n"
        f"Панель: `{panel_name}`\n"
        f"Email: `{email}`\n"
        f"Истекла: `{expiry_str}`\n\n"
        f"Отозвать: `/revoke {tg_id} {email}`"
    )
    for uid in admin_users:
        try:
            await context.bot.send_message(chat_id=uid, text=text, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Не удалось уведомить админа {uid}: {e}")


async def expiry_notification_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Ежедневная задача:
      - за 7 и 3 дня до окончания — напоминание клиенту;
      - на следующий день после окончания — алерт всем админам.
    """
    logger.info("Запуск задачи: expiry_notification_job")
    bindings = list_all_bindings()
    if not bindings:
        logger.info("Нет выданных клиентов — задача пропущена.")
        return

    today = datetime.now(_tz()).date()
    admin_users = config.get_admin_users()
    sent_count = 0

    for b in bindings:
        expiry_str = b.get("expiry_date")
        if not expiry_str:
            continue  # бессрочно

        # Подписка на паузе — не тревожим клиента
        if b.get("paused_at"):
            continue

        try:
            expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
        except ValueError:
            logger.warning(f"Некорректная дата '{expiry_str}' у {b['panel_name']}/{b['email']}")
            continue

        days_left = (expiry_date - today).days
        tg_id = b["tg_id"]
        panel_name = b["panel_name"]
        email = b["email"]

        # За 7 дней
        if days_left == 7:
            if not has_recent_notification(tg_id, panel_name, email, "7d", expiry_str):
                await _send_client_expiry_notice(
                    context, tg_id, panel_name, email, expiry_str, 7
                )
                log_notification(tg_id, panel_name, email, "7d", expiry_str)
                sent_count += 1

        # За 3 дня
        elif days_left == 3:
            if not has_recent_notification(tg_id, panel_name, email, "3d", expiry_str):
                await _send_client_expiry_notice(
                    context, tg_id, panel_name, email, expiry_str, 3
                )
                log_notification(tg_id, panel_name, email, "3d", expiry_str)
                sent_count += 1

        # На следующий день после окончания
        elif days_left == -1:
            if not has_recent_notification(tg_id, panel_name, email, "expired", expiry_str):
                await _notify_admins_expired(
                    context, admin_users, tg_id, panel_name, email, expiry_str
                )
                log_notification(tg_id, panel_name, email, "expired", expiry_str)
                sent_count += 1

    logger.info(f"Задача завершена. Отправлено уведомлений: {sent_count}.")


async def sync_all_panels() -> dict:
    """
    Синхронизирует БД со всеми панелями.

    В 3.8.0 данные клиента разделены:
      - /inbounds/list       — базовое (email, uuid, enable, expiryTime)
      - /clients/get/<email> — расширенное (limitHwid, comment, tgId, ...)

    Поэтому для каждой связки делаем ОТДЕЛЬНЫЙ запрос на полный объект.
    Быстро только по одному запросу на панель: строим карту uuid → email
    из inbounds/list, потом идём точечно в clients/get.
    """
    all_panels = config.get_all_panels()
    if not all_panels:
        return {"total_updated": 0, "total_renamed": 0, "total_missing": 0, "details": []}

    total_updated = 0
    total_renamed = 0
    total_missing = 0
    details = []

    for panel_name, pconf in all_panels.items():
        if pconf.get("disabled", False):
            details.append({"panel_name": panel_name, "status": "disabled"})
            continue

        try:
            async with _get_panel_api(panel_name) as api:
                await api.login()

                # 1. Карта uuid → email из общего списка (один запрос)
                inbounds_data = await api.get_inbounds()
                if not inbounds_data or not inbounds_data.get("success"):
                    details.append({"panel_name": panel_name, "status": "error"})
                    continue

                uuid_to_email = {}
                for inbound in inbounds_data.get("obj", []):
                    settings = _parse_settings(inbound.get("settings"))
                    if not settings:
                        continue
                    for client in settings.get("clients", []) or []:
                        c_uuid = client.get("uuid") or client.get("id") or ""
                        c_email = client.get("email") or ""
                        if c_uuid and c_email:
                            uuid_to_email[c_uuid] = c_email

                # 2. Обход связок панели
                panel_bindings = [b for b in list_all_bindings() if b["panel_name"] == panel_name]
                panel_upd = 0
                panel_ren = 0
                panel_miss = 0

                for b in panel_bindings:
                    b_uuid = b.get("uuid") or ""
                    if not b_uuid or b_uuid not in uuid_to_email:
                        panel_miss += 1
                        logger.warning(
                            f"[sync] {panel_name}: потеряна связка "
                            f"tg_id={b['tg_id']}, email={b['email']}, uuid={b_uuid or '(пусто)'}"
                        )
                        continue

                    email_in_panel = uuid_to_email[b_uuid]

                    # 3. Полный объект клиента (расширенное поле limitHwid там)
                    full = await api.get_client_object(email_in_panel)
                    if not full:
                        panel_miss += 1
                        logger.warning(
                            f"[sync] {panel_name}: get_client_object вернул None для {email_in_panel}"
                        )
                        continue

                    old_email = b["email"]
                    new_email = full.get("email") or email_in_panel

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

                    current_email = new_email or old_email

                    # 3.2. Комментарий
                    new_comment = full.get("comment") or ""
                    if new_comment != (b.get("comment") or ""):
                        try:
                            update_binding_comment(b["tg_id"], panel_name, current_email, new_comment)
                            panel_upd += 1
                        except Exception as e:
                            logger.error(f"[sync] Ошибка обновления комментария: {e}")

                    # 3.3. HWID
                    new_hwid = int(full.get("limitHwid") or 0)
                    if new_hwid != (b.get("limit_hwid") or 0):
                        try:
                            update_binding_limit_hwid(b["tg_id"], panel_name, current_email, new_hwid)
                            panel_upd += 1
                        except Exception as e:
                            logger.error(f"[sync] Ошибка обновления HWID: {e}")

                    # 3.4. Срок
                    new_expiry = None
                    expiry_ts = int(full.get("expiryTime") or 0)
                    if expiry_ts > 0:
                        try:
                            new_expiry = datetime.fromtimestamp(expiry_ts / 1000, _tz()).strftime("%Y-%m-%d")
                        except (ValueError, OSError):
                            new_expiry = None
                    if (new_expiry or "") != (b.get("expiry_date") or ""):
                        try:
                            update_binding_expiry(b["tg_id"], panel_name, current_email, new_expiry)
                            panel_upd += 1
                        except Exception as e:
                            logger.error(f"[sync] Ошибка обновления expiry: {e}")

                    # 3.5. Статус enabled
                    db_enabled = bool(b.get("enabled", 1))
                    panel_enabled = bool(full.get("enable", True))
                    if panel_enabled != db_enabled:
                        try:
                            update_binding_enabled(b["tg_id"], panel_name, current_email, panel_enabled)
                            panel_upd += 1
                        except Exception as e:
                            logger.error(f"[sync] Ошибка обновления enabled: {e}")

        except Exception as e:
            logger.error(f"[sync] Ошибка '{panel_name}': {e}")
            details.append({"panel_name": panel_name, "status": "error", "error": str(e)})
            continue

        total_updated += panel_upd
        total_missing += panel_miss
        details.append({
            "panel_name": panel_name,
            "status": "ok",
            "updated": panel_upd,
            "renamed": panel_ren,
            "missing": panel_miss,
        })

    return {
        "total_updated": total_updated,
        "total_renamed": total_renamed,
        "total_missing": total_missing,
        "details": details,
    }



async def auto_sync_job(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневная автосинхронизация БД с панелями (00:01 Asia/Ashgabat)."""
    logger.info("Запуск задачи: auto_sync_job")

    result = await sync_all_panels()
    logger.info(
        f"[auto_sync] обновлено {result['total_updated']}, "
        f"переименовано {result['total_renamed']}, "
        f"потеряно {result['total_missing']}"
    )

    # Уведомляем админов только если были реальные изменения
    if not (result["total_updated"] or result["total_renamed"] or result["total_missing"]):
        return

    lines = ["🔄 **Автосинхронизация БД с панелями**\n"]
    for d in result["details"]:
        if d["status"] == "ok":
            lines.append(
                f"**{d['panel_name']}**: обновлено {d['updated']}, "
                f"переименовано {d['renamed']}, потеряно {d['missing']}"
            )
        elif d["status"] == "disabled":
            lines.append(f"⏭️ **{d['panel_name']}**: отключена")
        elif d["status"] == "error":
            lines.append(f"❌ **{d['panel_name']}**: ошибка связи")

    text = "\n".join(lines)
    for uid in config.get_admin_users():
        try:
            await context.bot.send_message(chat_id=uid, text=text, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"[auto_sync] Не удалось уведомить {uid}: {e}")


