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


def get_config() -> Dict[str, Any]:
    """Загружает конфигурацию из config.yml."""
    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)
        print(f"Файл '{CONFIG_FILE}' не найден. Создан файл конфигурации по умолчанию.")
        print("Отредактируйте его: укажите токен бота и ID пользователей.")
        # Выход, так как токен обязателен
        exit()

    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def save_config(config_data: Dict[str, Any]):
    """Сохраняет конфигурацию и сразу перезагружает её в память."""
    global config
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        yaml.dump(config_data, f, allow_unicode=True, sort_keys=False)
    config = config_data  # Немедленно обновляем конфигурацию в памяти


# Загружаем конфигурацию при импорте
config = get_config()

# --- Вспомогательные функции для доступа к значениям конфигурации ---

def get_bot_token() -> str:
    return config.get("bot_token", "")


def get_admin_users() -> List[int]:
    return config.get("users", {}).get("admin_users", [])


def get_normal_users() -> List[int]:
    return config.get("users", {}).get("normal_users", [])


def get_panel_config(name: str) -> Dict[str, str]:
    """Возвращает конфигурацию конкретной панели по имени."""
    return config.get("panels", {}).get(name, {})


def get_all_panels() -> Dict[str, Any]:
    """Возвращает все настроенные панели."""
    return config.get("panels", {})


def delete_panel(name: str) -> bool:
    """Удаляет конфигурацию панели по имени."""
    current_config = get_config()
    if "panels" in current_config and name in current_config["panels"]:
        del current_config["panels"][name]
        save_config(current_config)
        return True
    return False


def is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором."""
    return user_id in get_admin_users()


def is_authorized(user_id: int) -> bool:
    """Проверяет, авторизован ли пользователь (админ или обычный)."""
    return is_admin(user_id) or user_id in get_normal_users()


def is_monthly_reset_enabled() -> bool:
    """Проверяет, включён ли ежемесячный автосброс трафика."""
    return config.get("monthly_reset", {}).get("enable", False)