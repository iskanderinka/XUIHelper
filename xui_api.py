import httpx
import logging
import re
import uuid as uuid_module
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)

# CSRF-токен в HTML может быть в двух порядках атрибутов
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

    Ключевые моменты версии 3.8.0:
      - Клиент может быть привязан сразу к нескольким инбаундам.
      - Создание клиента: POST <base>/panel/api/clients/add
      - Логин: сначала GET / (получаем cookie + CSRF из HTML), затем POST /login.
      - POST-запросы требуют X-Csrf-Token и X-Requested-With.
      - URL панели может содержать кастомный base-path (например, /x8UGpUW143YI9P3JVyQm).
    """

    def __init__(self, url: str, username: str, password: str, sub_url: str = ""):
        self.base_url = url.rstrip('/')
        self.sub_url = (sub_url or "").rstrip('/')
        self.username = username
        self.password = password
        self.client = httpx.AsyncClient(verify=False, timeout=30, follow_redirects=True)
        self.csrf_token: Optional[str] = None
        self._logged_in = False

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
        """
        Шаг 1: GET / — сервер выдаёт сессионную cookie и HTML с CSRF-токеном.
        Cookie автоматически сохраняется в self.client.cookies.
        """
        try:
            response = await self.client.get(f"{self.base_url}/")
            if response.status_code != 200:
                logger.error(f"Bootstrap: HTTP {response.status_code}")
                return False
            for pattern in CSRF_PATTERNS:
                match = pattern.search(response.text)
                if match:
                    self.csrf_token = match.group(1)
                    logger.info("CSRF-токен получен из HTML.")
                    return True
            logger.warning("CSRF-токен не найден в HTML главной страницы.")
            return False
        except httpx.RequestError as e:
            logger.error(f"Bootstrap: ошибка сети: {e}")
            return False

    async def login(self) -> bool:
        """
        Полный цикл входа:
          1. GET / → cookie + CSRF
          2. POST /login (form-urlencoded) с cookie + X-Csrf-Token
          3. GET / → обновление CSRF после логина
        """
        # 1. Bootstrap: получаем cookie + CSRF
        if not await self._bootstrap():
            return False

        # 2. Логин
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

            # 3. После логина CSRF мог обновиться — забираем свежий
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

            # При 403 обновим CSRF и попробуем один раз
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

    async def get_inbounds_list(self) -> List[Dict[str, Any]]:
        """Возвращает список инбаундов: id, remark, protocol, port, enable."""
        if not await self._ensure_session():
            return []
        data = await self._request("GET", "/panel/api/inbounds/list")
        if not data or not data.get("success"):
            logger.error(f"Не удалось получить список инбаундов: {data}")
            return []
        inbounds = data.get("obj", []) or []
        return [
            {
                "id": ib.get("id"),
                "remark": ib.get("remark", ""),
                "protocol": ib.get("protocol", ""),
                "port": ib.get("port"),
                "enable": ib.get("enable", True),
            }
            for ib in inbounds
        ]

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
    ) -> bool:
        """Создаёт клиента и привязывает его к указанным инбаундам."""
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
                "comment": "",
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
        data = await self._request("POST", f"/panel/api/clients/del/{email}")
        if data and data.get("success"):
            logger.info(f"Клиент '{email}' удалён.")
            return True
        logger.error(f"Ошибка удаления клиента '{email}': {data}")
        return False

    # ---- Sub-ссылка ----

    def get_client_sub_link(self, sub_id: str) -> Optional[str]:
        """Возвращает полную ссылку подписки."""
        if not self.sub_url:
            return None
        return f"{self.sub_url}/{sub_id}"