## ✨ Основные возможности

- **Мультипанельность**: один бот — все ваши панели 3x-ui.
- **Выдача подписок через бота**: клиент нажимает `/start`, админ делает `/addclient` — подписка уходит клиенту автоматически.
- **Автоматические задачи**:
    - Оповещение о недоступности панелей.
    - Напоминание об истечении инбаундов.
    - Ежедневный снимок трафика и отчёт.
    - Ежемесячный автосброс трафика.
- **Безопасность**: разделение прав админа и клиента, пароли не остаются в истории чата.

---

## 📂 Структура проекта

```
TgXUIMgr/
├── main.py               # Бот: команды, диалоги, задачи по расписанию
├── config.py             # Работа с config.yml, валидация URL панелей
├── database.py           # SQLite: трафик, привязки клиентов
├── xui_api.py            # HTTP-клиент панели 3x-ui 3.8.x
├── query_logic.py        # Логика запроса трафика по email
│
├── config.yml            # Реальный конфиг (не коммитится!)
├── config.yml.example    # Шаблон конфига
│
├── requirements.txt      # Зависимости
├── Dockerfile            # Сборка образа
├── docker-compose.yml    # Запуск на сервере
├── .gitignore            # Что не коммитить
├── .dockerignore         # Что не копировать в образ
│
├── README.md             # Это руководство
├── MANUAL.md             # Подробное руководство
├── MIGRATION.md          # Инструкция по переезду
│
├── data/                 # SQLite-база (не коммитится)
├── tests/                # Тесты
└── .venv/                # Python venv (не коммитится)
```

| Файл | Что делает |
|------|-----------|
| `main.py` | Единственная точка входа. Регистрирует команды бота, диалоги `/setting` и `/addclient`, ежедневные задачи. |
| `config.py` | Читает и пишет `config.yml`. Валидирует URL. |
| `database.py` | SQLite: три таблицы — `traffic_records`, `client_bindings`, `bot_users`. |
| `xui_api.py` | Общается с панелью 3x-ui по HTTPS. |
| `query_logic.py` | Функция «найти клиента по email и вернуть трафик/срок». |

---

## 🚀 Быстрый старт (Docker)

### Шаг 1. Подготовка

Установите [Docker](https://docs.docker.com/engine/install/) и [Docker Compose](https://docs.docker.com/compose/install/).

### Шаг 2. Настройка

1. Клонируйте репозиторий:
   ```bash
   git clone https://github.com/GeQainZz/TgXUIMgr.git
   cd TgXUIMgr
   ```

2. Создайте `config.yml`:
   ```bash
   cp config.yml.example config.yml
   ```
   Откройте и заполните:
   ```yaml
   bot_token: "YOUR_TELEGRAM_BOT_TOKEN"

   users:
     admin_users:
       - 123456789  # Ваш Telegram User ID
     normal_users: []

   panels:
     "TMT":
       url: "https://185.200.190.40:24487/x8UGpUW143YI9P3JVyQm"
       username: "your_username"
       password: "your_password"
       sub_url: "https://185.200.190.40:2096/sub"

   monthly_reset:
     enable: false

   traffic:
     accounting_mode: 'unidirectional'
   ```

   > **Как узнать Telegram User ID?**
   > Найдите в Telegram `@userinfobot` и начните диалог.

### Шаг 3. Запуск

```bash
docker-compose up -d --build
```

Готово! Бот работает.

---

## 📖 Работа с ботом

### Для админа

```
/setting                                # добавить панель через диалог
/inbounds TMT                           # список инбаундов с ID
/addclient 123456789 user123 TMT 8 9    # создать клиента
/revoke 123456789                       # удалить клиента
/listclients                            # список всех выданных
/status                                 # статус всех панелей
/report                                 # дневной отчёт сейчас
```

### Для клиента

```
/start                                  # активировать бота
/mylink                                 # получить ссылку подписки
/mystatus                               # посмотреть свой трафик
```

---

## 🔧 Управление Docker

```bash
docker-compose logs -f          # логи
docker-compose down             # остановить
docker-compose up -d --build    # пересобрать и запустить
```

---

## ⚙️ Ручное развёртывание

1. Установите зависимости:
   ```bash
   pip install -r requirements.txt
   ```
2. Настройте `config.yml` (см. Шаг 2).
3. Запустите:
   ```bash
   nohup python3 main.py &
   ```

---

## ⁉️ FAQ

**В: Как узнать свой Telegram User ID?**
О: В Telegram напишите `@userinfobot`, он пришлёт ваш ID.

**В: Клиент не получает подписку после `/addclient`?**
О: Клиент должен сначала нажать `/start` у бота. Telegram не даёт ботам писать первыми.

**В: Как узнать ID инбаунда?**
О: Команда `/inbounds TMT` покажет все инбаунды с их ID.

**В: Где посмотреть логи бота?**
О: `docker-compose logs -f` (в Docker) или `tail -f nohup.out` (при ручном запуске).

**В: Как обновить бота?**
О: `docker-compose down && git pull && docker-compose up -d --build`.