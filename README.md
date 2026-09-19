## ✨ Основные возможности

- **Мультипанельность**: один бот — все ваши панели 3x-ui.
- **Выдача подписок через бота**: клиент нажимает `/start`, админ делает
  `/addclient` — подписка приходит клиенту автоматически.
- **Пауза и продление**: `/pausesub` замораживает срок, `/resumesub` продлевает
  на дни паузы, `/extendsub` продлевает вручную.
- **Автоматические задачи**: напоминания клиентам за 7 и 3 дня, алерт админам
  после истечения, дневной отчёт, проверка панелей каждые 6 часов.
- **Роль суперадмина**: чувствительные команды (`/setting`, `/delpanel`) доступны
  только первому в списке админов.
- **Безопасность**: пароли панелей в `gitignore`, автоудаление пароля в диалоге,
  подтверждение опасных действий кнопками Да/Нет.

---

## 📂 Структура проекта

```
TgXUIMgr/
├── main.py               # Точка входа и хендлеры: команды, диалоги, callback
├── helpers.py            # Общие утилиты: формат, валидация, клавиатуры, панели
├── jobs.py               # Задачи по расписанию
├── config.py             # Работа с config.yml, валидация URL, суперадмин
├── database.py           # SQLite: трафик, привязки, пользователи бота
├── xui_api.py            # HTTP-клиент панели 3x-ui 3.8.x
│
├── config.yml            # Личный конфиг (не коммитится)
├── config.yml.example    # Шаблон конфига
├── requirements.txt      # Зависимости (с пинами версий)
│
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
└── .venv/                # Python venv (не коммитится)
```

| Файл | Что делает |
|------|-----------|
| `main.py` | Регистрирует команды, диалоги `/setting` и `/addclient`, callback-обработчики |
| `helpers.py` | Хелперы, используемые `main.py` и `jobs.py` |
| `jobs.py` | Задачи по расписанию: снимок, отчёт, проверка панелей, напоминания |
| `config.py` | Читает и пишет `config.yml`, валидирует URL, проверяет суперадмина |
| `database.py` | Четыре таблицы: `traffic_records`, `bot_users`, `client_bindings`, `notification_log` |
| `xui_api.py` | Общается с панелью 3x-ui по HTTPS |

---

## 🚀 Быстрый старт (Docker)

### Шаг 1. Подготовка

Установи [Docker](https://docs.docker.com/engine/install/) и
[Docker Compose](https://docs.docker.com/compose/install/).

### Шаг 2. Настройка

1. Клонируй репозиторий:

   ```bash
   git clone https://github.com/GeQainZz/TgXUIMgr.git
   cd XUIHelper
   ```

2. Создай `config.yml`:

   ```bash
   cp config.yml.example config.yml
   ```

3. Открой `config.yml` и заполни:

   ```yaml
   bot_token: "YOUR_TELEGRAM_BOT_TOKEN"

   timezone: "Asia/Hong_Kong"

   users:
     admin_users:
       - 123456789       # ПЕРВЫЙ = суперадмин

   panels:
     "TMT":
       url: "https://185.200.190.40:24487/x8UGpUW143YI9P3JVyQm"
       username: "your_username"
       password: "your_password"
       sub_url: "https://185.200.190.40:2096/sub"

   traffic:
     accounting_mode: unidirectional

   policy:
     url: ""
     message: ""

   tariffs:
     url: ""
     message: ""
   ```

   > **Как узнать Telegram User ID?**
   > Напиши `@userinfobot` в Telegram.

### Шаг 3. Запуск

```bash
docker-compose up -d --build
```

Готово. Бот работает.

---

## 📖 Работа с ботом

### Для админа

```
/addclient 123456789 user123 TMT 8 9    # создать клиента
/revoke 123456789 user123               # удалить
/pausesub 123456789 user123             # приостановить
/resumesub 123456789 user123            # возобновить
/extendsub 123456789 +30 user123        # продлить
/listclients                            # список (постранично)
/getlink 123456789 user123              # получить ссылку
/inbounds TMT                           # список инбаундов
/status TMT                             # статус панели
/listpanels                             # все панели
/report                                 # дневной отчёт
```

### Только для суперадмина (первый в списке)

```
/setting                                # добавить/обновить панель
/delpanel TMT                           # удалить панель из бота
```

### Для клиента

```
/start                                  # активировать бота
/mylink                                 # получить ссылку
/policy                                 # политика конфиденциальности
```

Плюс reply-клавиатура с тремя кнопками после создания подписки:

- 🔗 Ссылка подписки
- 📊 Тарифы
- 🆘 Нужна помощь

---

## 🔧 Управление Docker

```bash
docker-compose logs -f          # логи
docker-compose down             # остановить
docker-compose up -d --build    # пересобрать
```

---

## ⚙️ Ручное развёртывание

1. Установи зависимости:

   ```bash
   pip install -r requirements.txt
   ```

2. Настрой `config.yml` (см. Шаг 2).

3. Запусти:

   ```bash
   nohup python3 main.py &
   ```

---

## ⁉️ FAQ

**В: Как узнать свой Telegram User ID?**
О: В Telegram напиши `@userinfobot`.

**В: Клиент не получает подписку после `/addclient`.**
О: Клиент должен сначала нажать `/start` у бота. Telegram не даёт ботам писать
первыми.

**В: Как узнать ID инбаунда?**
О: Команда `/inbounds TMT` покажет все инбаунды с их ID.

**В: Где посмотреть логи бота?**
О: `docker-compose logs -f` (Docker) или `tail -f nohup.out` (ручной запуск).

**В: Как обновить бота?**
О: `docker-compose down && git pull && docker-compose up -d --build`.

**В: Можно ли запускать второй диалог, пока первый активен?**
О: Нет. Но если запустишь — бот **автоматически отменит** первый диалог и
продолжит второй. Ничего не сломается.