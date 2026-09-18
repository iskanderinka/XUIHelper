# config.py
import yaml
import os
from typing import Dict, Any, List

CONFIG_FILE = "config.yml"
DEFAULT_CONFIG = {
    "bot_token": "YOUR_TELEGRAM_BOT_TOKEN",
    "users": {
        "admin_users": [123456789],
        "normal_users": []
    },
    "panels": {}
}

from urllib.parse import urlparse


def _validate_panel_url(url: str) -> tuple:
    """
    Проверяет, что URL панели безопасен и валиден.

    Возвращает (True, "") при успехе,
              (False, "сообщение об ошибке") при провале.
    """
    if not url or not isinstance(url, str):
        return False, "URL не может быть пустым."

    url = url.strip()

    if len(url) > 500:
        return False, "URL слишком длинный (максимум 500 символов)."

    if any(c.isspace() for c in url):
        return False, "URL не должен содержать пробелы."

    # Управляющие символы ASCII (0x00-0x1F)
    if any(ord(c) < 32 for c in url):
        return False, "URL содержит недопустимые управляющие символы."

    try:
        parsed = urlparse(url)
    except ValueError as e:
        return False, f"Не удалось разобрать URL: {e}"

    if parsed.scheme not in ("http", "https"):
        return False, "Разрешены только схемы http:// и https://."

    if not parsed.hostname:
        return False, "В URL не указан хост."

    if parsed.username or parsed.password:
        return False, "URL не должен содержать логин и пароль (user:pass@host)."

    # parsed.port — ленивое свойство, может бросить ValueError
    try:
        port = parsed.port
    except ValueError:
        return False, "Порт должен быть в диапазоне 1–65535."

    if port is not None and not (1 <= port <= 65535):
        return False, "Порт должен быть в диапазоне 1–65535."

    return True, ""


def get_config() -> Dict[str, Any]:
    """Загружает конфигурацию из config.yml."""
    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)
        print(f"Файл '{CONFIG_FILE}' не найден. Создан файл конфигурации по умолчанию.")
        print("Отредактируйте его: укажите токен бота и ID пользователей.")
        exit()

    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def save_config(config_data: Dict[str, Any]):
    """Сохраняет конфигурацию и сразу перезагружает её в память."""
    global config
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        yaml.dump(config_data, f, allow_unicode=True, sort_keys=False)
    config = config_data


# Загружаем конфигурацию при импорте
config = get_config()


# ---------- Доступ к общим полям ----------

def get_bot_token() -> str:
    return config.get("bot_token", "")


def get_admin_users() -> List[int]:
    users = config.get("users", {}).get("admin_users") or []
    if not isinstance(users, list):
        users = [users] if users else []
    result = []
    for u in users:
        try:
            result.append(int(u))
        except (ValueError, TypeError):
            continue
    return result

def is_admin(user_id: int) -> bool:
    return user_id in get_admin_users()

# ---------- Панели ----------

def get_panel_config(name: str) -> Dict[str, str]:
    """Возвращает конфигурацию панели по имени."""
    return config.get("panels", {}).get(name, {})


def get_all_panels() -> Dict[str, Any]:
    """Возвращает все панели."""
    return config.get("panels", {})


def add_or_update_panel(name: str, url: str, username: str, password: str,
                        reset_day: int = None, sub_url: str = None) -> None:
    """
    Создаёт или обновляет панель. Бросает ValueError при невалидном URL.
    """
    ok, err = _validate_panel_url(url)
    if not ok:
        raise ValueError(err)

    if sub_url:
        ok, err = _validate_panel_url(sub_url)
        if not ok:
            raise ValueError(f"sub_url: {err}")

    current = get_config()
    panels = current.setdefault("panels", {})
    existing = panels.get(name, {})

    panel = dict(existing)
    panel["url"] = url
    panel["username"] = username
    panel["password"] = password
    if reset_day is not None:
        panel["reset_day"] = int(reset_day)
    if sub_url is not None:
        panel["sub_url"] = sub_url

    panels[name] = panel
    save_config(current)


def delete_panel(name: str) -> bool:
    """Удаляет панель по имени."""
    current = get_config()
    if "panels" in current and name in current["panels"]:
        del current["panels"][name]
        save_config(current)
        return True
    return False


# ---------- Автоматизация ----------

def is_daily_report_enabled() -> bool:
    return config.get("daily_report", {}).get("enable", False)


def get_daily_report_hour() -> int:
    """Час отправки дневного отчёта (0-23). По умолчанию 8."""
    hour = config.get("daily_report", {}).get("hour", 8)
    try:
        hour = int(hour)
    except (ValueError, TypeError):
        hour = 8
    if hour < 0 or hour > 23:
        hour = 8
    return hour


# ---------- Учёт трафика ----------

def get_accounting_mode() -> str:
    """'unidirectional' или 'bidirectional'. По умолчанию — первый."""
    mode = config.get("traffic", {}).get("accounting_mode", "unidirectional")
    if mode not in ("unidirectional", "bidirectional"):
        return "unidirectional"
    return mode


# ---------- Политика конфиденциальности ----------
DEFAULT_POLICY_MESSAGE = (
    "🔒 **Политика конфиденциальности**\n\n"
    "Мы собираем минимально необходимые данные для предоставления услуг:\n\n"
    "• Telegram ID и имя пользователя\n"
    "• Даты подписки\n"
    "• История обращений\n\n"
    "Данные используются только для работы сервиса и не передаются третьим лицам."
)


def get_policy_url() -> str:
    """URL страницы политики. Пусто, если не задан в config.yml."""
    return (config.get("policy", {}).get("url") or "").strip()


def get_policy_message() -> str:
    """Краткий текст перед кнопкой. Если не задан — стандартный."""
    message = (config.get("policy", {}).get("message") or "").strip()
    return message if message else DEFAULT_POLICY_MESSAGE


# ---------- Тарифы ----------

DEFAULT_TARIFFS_MESSAGE = (
    "📊 **Наши тарифы**\n\n"
    "Актуальные тарифы и цены — по кнопке ниже."
)


def get_tariffs_url() -> str:
    """URL страницы с тарифами. Пусто, если не задан в config.yml."""
    return (config.get("tariffs", {}).get("url") or "").strip()


def get_tariffs_message() -> str:
    """Текст перед кнопкой тарифов. Если не задан — стандартный."""
    msg = (config.get("tariffs", {}).get("message") or "").strip()
    return msg if msg else DEFAULT_TARIFFS_MESSAGE

# ---------- Суперадмин ----------

def get_superadmin_id() -> int | None:
    """
    Возвращает ID суперадмина — первого в списке admin_users.
    None, если список пуст.
    """
    admins = get_admin_users()
    return admins[0] if admins else None


def is_superadmin(user_id: int) -> bool:
    """Проверяет, является ли пользователь суперадмином."""
    superadmin = get_superadmin_id()
    return superadmin is not None and int(user_id) == superadmin


def has_superadmin() -> bool:
    """Есть ли вообще суперадмин в конфиге."""
    return get_superadmin_id() is not None