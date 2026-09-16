# query_logic.py
from datetime import datetime
import config
from xui_api import XUIApi


async def query_user_data(panel_name: str, email: str) -> (bool, dict or str):
    """
    Основная логика запроса данных пользователя.

    :param panel_name: имя панели
    :param email: email пользователя
    :return: кортеж (success, result).
             При успехе result — словарь с данными.
             При ошибке result — строка с сообщением.
    """
    panel_config = config.get_panel_config(panel_name)
    if not panel_config:
        return False, f"Панель '{panel_name}' не найдена."
    if panel_config.get("disabled", False):
        return False, f"Панель '{panel_name}' отключена, запрос невозможен."

    async with XUIApi(panel_config["url"], panel_config["username"], panel_config["password"]) as api:
        inbounds_data = await api.get_inbounds()
    if not inbounds_data or not inbounds_data.get("success"):
        return False, "Не удалось получить данные с панели. Повторите позже или обратитесь к администратору."

    found_inbound = None
    for inbound in inbounds_data.get("obj", []):
        clients = inbound.get("clientStats", [])
        for client in clients:
            if client.get("email") == email:
                found_inbound = client
                found_inbound.update({
                    'total': client.get('total', inbound.get('total', 0)),
                    'expiryTime': client.get('expiryTime', inbound.get('expiryTime', 0))
                })
                break
        if found_inbound:
            break

    if found_inbound:
        used_bytes = found_inbound.get("up", 0) + found_inbound.get("down", 0)
        total_bytes = found_inbound.get("total", 0)

        used_gb = used_bytes / (1024**3)
        total_gb = total_bytes / (1024**3)

        expiry_ts = found_inbound.get("expiryTime", 0)
        expiry_date = datetime.fromtimestamp(expiry_ts / 1000).strftime('%Y-%m-%d') if expiry_ts > 0 else "бессрочно"

        return True, {
            "email": email,
            "panel_name": panel_name,
            "used_gb": f"{used_gb:.2f}",
            "total_gb": f"{total_gb:.2f}",
            "expiry_date": expiry_date,
        }
    else:
        return False, f"Пользователь '{email}' не найден на панели '{panel_name}'."