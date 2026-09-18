import httpx
import logging
import re
import ssl
import json
import uuid as uuid_module
from typing import Dict, Any, Optional, List
from urllib.parse import quote as url_quote, urlparse

logger = logging.getLogger(__name__)

# CSRF-токен в HTML — в двух возможных порядках атрибутов
CSRF_PATTERNS = [
    re.compile(
        r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']',
        re.IGNORECASE,
    ),
    re.compile(
        r'<meta\s+content=["\']([^"\']+)["\']\s+name=["\']csrf-token["\']',
        re.IGNORECASE,
    ),
]


class XUIApi:
    """
    Клиент для панели 3x-ui 3.8.0+.

    Особенности 3.8.0:
      - Клиент может быть привязан сразу к нескольким инбаундам.
      - Создание клиента: POST <base>/panel/api/clients/add
      - Удаление клиента: POST <base>/panel/api/clients/del/<email>
      - Логин: сначала GET / (получаем cookie + CSRF), затем POST /login.
      - POST-запросы требуют X-Csrf-Token и X-Requested-With.
      - URL панели может содержать кастомный base-path (например, /x8UGpUW143YI9P3JVyQm).
    """

    def __init__(self, url: str, username: str, password: str, sub_url: str = ""):
        self.base_url = url.rstrip('/')
        self.sub_url = (sub_url or "").rstrip('/')
        self.username = username
        self.password = password
        verify = self._resolve_verify_setting()
        self.client = httpx.AsyncClient(verify=verify, timeout=30, follow_redirects=True)
        self.csrf_token: Optional[str] = None
        self._logged_in = False

    def _resolve_verify_setting(self):
        """
        Определяет режим проверки SSL для httpx.

        - 127.0.0.1 / localhost / ::1
              → verify=False (безопасно: трафик не покидает машину)

        - внешний адрес
              → SSLContext с обычной проверкой цепочки через системные корни,
                но с check_hostname=False. Это нужно, потому что панель
                обычно доступна по IP, а сертификат выписан на домен.
                Цепочка сертификата при этом проверяется полностью —
                MITM с самоподписанным сертификатом не пройдёт.
        """
        host = (urlparse(self.base_url).hostname or "").lower()
        if host in ("127.0.0.1", "localhost", "::1"):
            logger.info("Локальный адрес — SSL-проверка отключена (безопасно).")
            return False

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        # verify_mode = CERT_REQUIRED по умолчанию — цепочка проверяется.
        logger.info("Внешний адрес — проверка цепочки через системные корни.")
        return ctx

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.aclose()

    async def aclose(self):
        await self.client.aclose()

    # ---- Заголовки ----

    def _headers(self, method: str) -> Dict[str, str]:
        headers = {"X-Requested-With": "XMLHttpRequest"}
        if method.upper() != "GET" and self.csrf_token:
            headers["X-Csrf-Token"] = self.csrf_token
        return headers

    # ---- CSRF и логин ----

    async def _bootstrap(self) -> bool:
        """GET / — сервер выдаёт cookie и HTML с CSRF-токеном."""
        try:
            response = await self.client.get(f"{self.base_url}/")
            if response.status_code not in (200, 307):
                logger.error(f"Bootstrap: HTTP {response.status_code}")
                return False
            for pattern in CSRF_PATTERNS:
                match = pattern.search(response.text)
                if match:
                    self.csrf_token = match.group(1)
                    logger.debug("CSRF-токен получен из HTML.")
                    return True
            logger.warning("CSRF-токен не найден в HTML главной страницы.")
            return False
        except httpx.RequestError as e:
            logger.error(f"Bootstrap: ошибка сети: {e}")
            return False

    async def login(self) -> bool:
        """Полный цикл входа: GET / → POST /login → GET /."""
        if not await self._bootstrap():
            return False

        login_url = f"{self.base_url}/login"
        try:
            response = await self.client.post(
                login_url,
                data={
                    "username": self.username,
                    "password": self.password,
                    "twoFactorCode": "",
                },
                headers=self._headers("POST"),
            )
            if response.status_code != 200:
                logger.error(f"Логин: HTTP {response.status_code}")
                return False
            data = response.json()
            if not data.get("success"):
                logger.error(f"Логин: {data.get('msg', 'нет сообщения')}")
                return False
            logger.info("Логин успешен.")

            # После логина CSRF мог измениться
            self.csrf_token = None
            await self._bootstrap()
            self._logged_in = True
            return True

        except (httpx.RequestError, ValueError) as e:
            logger.error(f"Логин: {e}")
            return False

    async def _ensure_session(self) -> bool:
        if not self._logged_in:
            return await self.login()
        return True

    # ---- Универсальный запрос ----

    async def _request(self, method: str, path: str,
                       json_body: Optional[Dict] = None) -> Optional[Dict]:
        url = f"{self.base_url}{path}"
        try:
            kwargs: Dict[str, Any] = {"headers": self._headers(method)}
            if json_body is not None:
                kwargs["json"] = json_body

            if method.upper() == "GET":
                response = await self.client.get(url, **kwargs)
            else:
                response = await self.client.post(url, **kwargs)

            if response.status_code == 403:
                logger.warning(f"403 на {method} {path}, обновляю CSRF и повторяю.")
                self.csrf_token = None
                if await self._bootstrap():
                    kwargs["headers"] = self._headers(method)
                    if method.upper() == "GET":
                        response = await self.client.get(url, **kwargs)
                    else:
                        response = await self.client.post(url, **kwargs)

            if response.status_code == 404:
                return None
            return response.json()
        except (httpx.RequestError, ValueError) as e:
            logger.error(f"Ошибка запроса {method} {path}: {e}")
            return None

    # ---- Инбаунды ----

    async def get_inbounds(self) -> Optional[Dict[str, Any]]:
        """Сырой ответ /panel/api/inbounds/list (используется задачами)."""
        if not await self._ensure_session():
            return None
        return await self._request("GET", "/panel/api/inbounds/list")

    async def get_inbounds_list(self) -> List[Dict[str, Any]]:
        """Список инбаундов: id, remark, protocol, port, enable."""
        data = await self.get_inbounds()
        if not data or not data.get("success"):
            logger.error(f"Не удалось получить список инбаундов: {data}")
            return []
        return [
            {
                "id": ib.get("id"),
                "remark": ib.get("remark", ""),
                "protocol": ib.get("protocol", ""),
                "port": ib.get("port"),
                "enable": ib.get("enable", True),
            }
            for ib in (data.get("obj") or [])
        ]

    # ---- Статус сервера ----

    async def get_server_status(self) -> Optional[Dict[str, Any]]:
        """Статус сервера (CPU, RAM, диск, Xray и т.д.)."""
        if not await self._ensure_session():
            return None
        data = await self._request("GET", "/panel/api/server/status")
        return data.get("obj") if data else None

    # ---- Все клиенты ----

    async def get_all_clients(self) -> List[Dict[str, Any]]:
        """Плоский список всех клиентов всех инбаундов."""
        data = await self.get_inbounds()
        if not data or not data.get("success"):
            return []
        clients: List[Dict[str, Any]] = []
        for inbound in (data.get("obj") or []):
            for cs in (inbound.get("clientStats") or []):
                clients.append({
                    "email": cs.get("email", ""),
                    "up": cs.get("up", 0),
                    "down": cs.get("down", 0),
                    "total": cs.get("total", 0),
                    "expiryTime": cs.get("expiryTime", 0),
                })
        return clients

    # ---- Создание клиента ----
    async def create_client(
        self,
        email: str,
        client_uuid: str,
        sub_id: str,
        inbound_ids: List[int],
        limit_hwid: int = 0,
        tg_id: int = 0,
        total_gb: int = 0,
        expiry_time: int = 0,
        comment: str = "",
    ) -> bool:
        """Создаёт клиента и привязывает к указанным инбаундам."""
        if not await self._ensure_session():
            return False
        if not inbound_ids:
            logger.error("Не передан ни один ID инбаунда.")
            return False

        payload = {
            "client": {
                "email": email,
                "uuid": client_uuid,
                "id": client_uuid,
                "subId": sub_id,
                "password": uuid_module.uuid4().hex[:16],
                "auth": uuid_module.uuid4().hex[:16],
                "flow": "",
                "security": "auto",
                "limitIp": 0,
                "limitHwid": limit_hwid,
                "totalGB": total_gb,
                "expiryTime": expiry_time,
                "enable": True,
                "tgId": tg_id,
                "comment": comment or "",
                "group": "",
                "reset": 0,
                "resetDay": 0,
                "resetMax": 0,
                "trafficReset": "never",
                "trafficResetDay": 1,
            },
            "inboundIds": inbound_ids,
        }

        data = await self._request("POST", "/panel/api/clients/add", json_body=payload)
        if data and data.get("success"):
            logger.info(f"Клиент '{email}' создан в инбаундах {inbound_ids}.")
            return True
        logger.error(f"Ошибка создания клиента '{email}': {data}")
        return False

    # ---- Удаление клиента ----

    async def delete_client(self, email: str) -> bool:
        """Удаляет клиента по email (панель сама найдёт его во всех инбаундах)."""
        if not await self._ensure_session():
            return False
        safe_email = url_quote(email, safe="")
        data = await self._request("POST", f"/panel/api/clients/del/{safe_email}")
        if data and data.get("success"):
            logger.info(f"Клиент '{email}' удалён.")
            return True
        logger.error(f"Ошибка удаления клиента '{email}': {data}")
        return False

    # ---- Сброс трафика ----

    async def reset_all_client_traffic(self) -> bool:
        """Сбрасывает трафик всем клиентам всех инбаундов."""
        if not await self._ensure_session():
            return False
        data = await self._request("POST", "/panel/api/inbounds/resetAllClientTraffics/-1")
        return data is not None and data.get("success", False)

    # ---- Получение полного объекта клиента ----

    async def get_client_object(self, email: str) -> Optional[Dict[str, Any]]:
        """
        Возвращает полный объект клиента из панели.

        Пробует несколько эндпоинтов 3.8.x. Если ни один не сработал —
        ищет клиента в inbounds list.
        """
        if not await self._ensure_session():
            return None

        safe_email = url_quote(email, safe="")

        # Основной вариант: /panel/api/clients/get/<email>
        data = await self._request("GET", f"/panel/api/clients/get/{safe_email}")
        if data and data.get("success") and data.get("obj"):
            obj = data["obj"]
            # Некоторые версии оборачивают: {"obj": {"client": {...}, "inboundIds": [...]}}
            if isinstance(obj, dict) and "client" in obj:
                return obj["client"]
            return obj

        # Fallback: ищем клиента в inbounds list
        inbounds_data = await self.get_inbounds()
        if inbounds_data and inbounds_data.get("success"):
            for inbound in inbounds_data.get("obj", []) or []:
                settings_raw = inbound.get("settings", "")
                if not settings_raw:
                    continue
                try:
                    settings = json.loads(settings_raw)
                except (ValueError, TypeError):
                    continue
                for client in settings.get("clients", []) or []:
                    if client.get("email") == email:
                        return client
        return None

    # ---- Обновление клиента (enable / expiry / comment) ----

    async def update_client(self, email: str, **changes) -> bool:
        """
        Обновляет поля клиента в панели.

        Собирает payload вручную из известных полей — ровно тех,
        что отправляет UI 3.8.0. Лишние поля из GET (allowedIPs,
        clientStats, up, down и т.п.) не передаём: панель на них падает.
        """
        if not await self._ensure_session():
            return False

        client = await self.get_client_object(email)
        if not client:
            logger.error(f"Клиент '{email}' не найден в панели для обновления.")
            return False

        # id должен быть строкой (UUID). В GET приходит числовой DB-id.
        uuid_value = client.get("uuid") or ""
        if not isinstance(uuid_value, str):
            uuid_value = str(uuid_value)
        client_id = uuid_value or str(client.get("id", "") or "")

        def _to_int(v, default=0):
            try:
                return int(v)
            except (ValueError, TypeError):
                return default

        payload = {
            "email": client.get("email") or email,
            "uuid": uuid_value,
            "id": client_id,
            "subId": client.get("subId") or "",
            "password": client.get("password") or "",
            "auth": client.get("auth") or "",
            "flow": client.get("flow") or "",
            "security": client.get("security") or "auto",
            "limitIp": _to_int(client.get("limitIp"), 0),
            "limitHwid": _to_int(client.get("limitHwid"), 0),
            "totalGB": _to_int(client.get("totalGB"), 0),
            "expiryTime": _to_int(client.get("expiryTime"), 0),
            "enable": bool(client.get("enable", True)),
            "tgId": _to_int(client.get("tgId"), 0),
            "comment": client.get("comment") or "",
            "group": client.get("group") or "",
            "reset": _to_int(client.get("reset"), 0),
            "resetDay": _to_int(client.get("resetDay"), 0),
            "resetMax": _to_int(client.get("resetMax"), 0),
            "trafficReset": client.get("trafficReset") or "never",
            "trafficResetDay": _to_int(client.get("trafficResetDay"), 1),
        }

        # Применяем изменения
        for key, value in changes.items():
            payload[key] = value

        safe_email = url_quote(email, safe="")
        data = await self._request(
            "POST",
            f"/panel/api/clients/update/{safe_email}",
            json_body=payload,
        )
        if data and data.get("success"):
            logger.info(f"Клиент '{email}' обновлён: {list(changes.keys())}")
            return True
        logger.error(f"Ошибка обновления клиента '{email}': {data}")
        return False
    # ---- Sub-ссылка ----

    def get_client_sub_link(self, sub_id: str) -> Optional[str]:
        """Возвращает полную ссылку подписки."""
        if not self.sub_url:
            return None
        return f"{self.sub_url}/{sub_id}"