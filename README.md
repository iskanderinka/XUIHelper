# XUIHelper

Telegram-бот для управления несколькими панелями 3x-ui: создание клиентов, выдача подписок, управление сроками, статистика трафика, журнал действий администраторов.

---

## 🙏 Благодарности

Идея и структура проекта вдохновлены [TgXUIMgr](https://github.com/GeQainZz/TgXUIMgr).

XUIHelper — **переработанная и расширенная версия**: перевод на русский, отказ от веб-админки, поддержка API 3x-ui 3.8.x, роль суперадмина, пауза/продление подписок, комментарии к клиентам, автозадачи, аудит действий, выдача прямых конфигураций.

---

## ✨ Основные возможности

- **Мультипанельность**: один бот — все ваши панели 3x-ui.
- **Выдача подписок через бота**: клиент нажимает `/start`, отправляет заявку, админ создаёт клиента — подписка приходит автоматически.
- **Пауза и продление**: `/pausesub` замораживает срок, `/resumesub` продлевает на дни паузы, `/extendsub` продлевает вручную.
- **Выдача конфигураций**: `/getlink` может отдать как sub-ссылку, так и прямые ссылки на каждый инбаунд (актуально при блокировках провайдера).
- **Автоматические задачи**: напоминания клиентам за 7 и 3 дня, алерт админам после истечения, дневной отчёт, проверка панелей каждые 6 часов, автосинхронизация БД.
- **Журнал действий админов**: все команды и результаты операций пишутся в `data/audit.log`.
- **Роль суперадмина**: чувствительные команды (`/setting`, `/delpanel`, `/sync`) доступны только первому в списке админов.
- **Безопасность**: пароли панелей в `gitignore`, автоудаление пароля в диалоге, подтверждение опасных действий кнопками Да/Нет.
- **Гайды внутри бота**: три статьи — для клиента (`/guide`), админа (`/guideadmin`), суперадмина (`/guidesuper`).

---

## 📂 Структура проекта

```
XUIHelper/
├── main.py               # Точка входа и хендлеры: команды, диалоги, callback
├── helpers.py            # Общие утилиты: формат, валидация, клавиатуры, панели
├── jobs.py               # Задачи по расписанию
├── config.py             # Работа с config.yml, валидация URL, суперадмин
├── database.py           # SQLite: трафик, привязки, пользователи бота
├── audit.py              # Журнал действий администраторов
├── xui_api.py            # HTTP-клиент панели 3x-ui 3.8.x
│
├── config.yml            # Личный конфиг (не коммитится)
├── config.yml.example    # Шаблон конфига
├── requirements.txt      # Зависимости (с пинами версий)
│
├── README.md             # Это руководство
├── MANUAL.md             # Подробное руководство
├── MIGRATION.md          # Инструкция по развёртыванию и переезду
├── CHANGELOG.md          # История версий
├── LICENSE               # MIT
│
├── data/                 # SQLite-база и audit.log (не коммитится)
└── .venv/                # Python venv (не коммитится)
```

| Файл | Что делает |
|------|-----------|
| `main.py` | Регистрирует команды, диалоги `/setting` и `/addclient`, callback-обработчики |
| `helpers.py` | Хелперы для `main.py` и `jobs.py`: форматирование, валидация, клавиатуры |
| `jobs.py` | Задачи по расписанию: снимок трафика, отчёт, проверка панелей, напоминания, автосинк |
| `config.py` | Читает и пишет `config.yml`, валидирует URL, проверяет суперадмина |
| `database.py` | Четыре таблицы: `traffic_records`, `bot_users`, `client_bindings`, `notification_log` |
| `audit.py` | Пишет действия админов в `data/audit.log` с дневным разделителем |
| `xui_api.py` | Общается с панелью 3x-ui по HTTPS |

---

## 🚀 Установка

### Требования

- VPS с Linux (Alpine / Debian / Ubuntu).
- Python 3.11+.
- Git, curl, gcc/make.
- **SSH-доступ к панели 3x-ui** — если Telegram блокируется в вашем регионе (Россия, Туркменистан).

### Шаг 1. Установка системных зависимостей

**Alpine:**

```bash
apk add --no-cache git python3 py3-pip gcc make musl-dev \
    python3-dev libffi-dev openssl-dev yaml-dev tzdata logrotate
```

**Debian / Ubuntu:**

```bash
apt update
apt install -y git python3 python3-venv python3-pip \
    build-essential libssl-dev libffi-dev libyaml-dev tzdata
```

### Шаг 2. Клонирование и окружение

```bash
cd <PROJECT_DIR>
git clone https://github.com/<your-repo>/XUIHelper.git
cd XUIHelper

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Шаг 3. Конфигурация

```bash
cp config.yml.example config.yml
vi config.yml
```

Минимум, что нужно заполнить:

```yaml
bot_token: "YOUR_TELEGRAM_BOT_TOKEN"

users:
  admin_users:
    - <superadmin_tg_id>   # ← первый ID — суперадмин

panels:
  "PANEL_NAME":
    url: "https://<PANEL_IP>:<PANEL_PORT>/<WEB_BASE_PATH>"
    username: "<panel_login>"
    password: "<panel_password>"
    sub_url: "https://<PANEL_IP>:<SUB_PORT>/<SUB_PATH>"

timezone: "Asia/Hong_Kong"
```

### Шаг 4. Прокси для Telegram (если нужно)

**Если Telegram доступен с вашего сервера напрямую** — пропустите этот шаг.

**Если Telegram блокируется** (типично для РФ) — нужен SOCKS5-туннель через сервер за границей:

```bash
# Создаём SSH-ключ
ssh-keygen -t ed25519 -C "tunnel@xuihelper" -f ~/.ssh/id_ed25519 -N ""

# Копируем публичный ключ на удалённый сервер
ssh-copy-id -i ~/.ssh/id_ed25519.pub root@<REMOTE_SERVER>

# Проверяем, что работает без пароля
ssh -o PasswordAuthentication=no root@<REMOTE_SERVER> "echo OK"
```

Подробности — в `MIGRATION.md`, раздел «Туннель для Telegram».

### Шаг 5. Проверка запуска

```bash
cd <PROJECT_DIR>/XUIHelper
source .venv/bin/activate

# Если используется туннель
export HTTPS_PROXY=socks5://127.0.0.1:1080
export HTTP_PROXY=socks5://127.0.0.1:1080
export ALL_PROXY=socks5://127.0.0.1:1080

python3 main.py
```

В логе должно появиться:

```
database - INFO - Database initialised at ...
__main__ - INFO - Бот запущен...
telegram.ext.Application - INFO - Application started
```

Напишите боту в Telegram `/start` — если отвечает, всё работает. Остановите `Ctrl+C`.

### Шаг 6. Автозапуск через OpenRC (Alpine)

```bash
# Туннель (если нужен)
cat > /etc/init.d/tg-tunnel << 'EOF'
#!/sbin/openrc-run

name="Telegram SSH tunnel"

command="/usr/bin/ssh"
command_args="-N -D 127.0.0.1:1080 \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes \
    -o StrictHostKeyChecking=accept-new \
    -o BatchMode=yes \
    root@<REMOTE_SERVER>"

command_user="root"
pidfile="/run/${RC_SVCNAME}.pid"

command_background="yes"
output_log="/var/log/tg-tunnel.log"
error_log="/var/log/tg-tunnel.err"

respawn_delay=5
respawn_max=0

depend() {
    need net
}
EOF

chmod +x /etc/init.d/tg-tunnel
rc-update add tg-tunnel default
rc-service tg-tunnel start
```

```bash
# Бот
cat > /etc/init.d/xuihelper << 'EOF'
#!/sbin/openrc-run

name="XUIHelper Telegram bot"

command="<PROJECT_DIR>/XUIHelper/.venv/bin/python"
command_args="-u main.py"
command_user="root:root"
directory="<PROJECT_DIR>/XUIHelper"
pidfile="/run/${RC_SVCNAME}.pid"

command_background="yes"
output_log="/var/log/xuihelper.log"
error_log="/var/log/xuihelper.err"

export HTTPS_PROXY="socks5://127.0.0.1:1080"
export HTTP_PROXY="socks5://127.0.0.1:1080"
export ALL_PROXY="socks5://127.0.0.1:1080"
export NO_PROXY="127.0.0.1,localhost"

respawn_delay=10
respawn_max=0

depend() {
    need net
    use tg-tunnel
}
EOF

chmod +x /etc/init.d/xuihelper
rc-update add xuihelper default
rc-service xuihelper start
```

> **`NO_PROXY` важен.** Без него бот будет гнать через SOCKS5 и запросы к панели, что добавит лишний крюк. Если панель имеет внешний IP — добавь его в список через запятую.

### Шаг 7. Logrotate для логов

```bash
cat > /etc/logrotate.d/xuihelper << 'EOF'
/var/log/xuihelper.log /var/log/xuihelper.err /var/log/tg-tunnel.log /var/log/tg-tunnel.err {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF

echo "0 3 * * * logrotate /etc/logrotate.d/xuihelper" >> /etc/crontabs/root
rc-service crond restart
```

> **`data/audit.log` ротируется автоматически** самим ботом (при превышении 1 МБ → `audit.log.1`). Logrotate к нему не нужен.

---

## 📖 Работа с ботом

### Для клиента

| Команда | Что делает |
|---------|-----------|
| `/start` | Активировать бота, получить TG ID, отправить заявку |
| `/help` | Справка |
| `/guide` | Гайд по боту |
| `/policy` | Политика конфиденциальности |
| `/mylink` | Получить свою sub-ссылку |

**Reply-клавиатура после выдачи подписки:**

```
[🔗 Ссылка подписки]
[📊 Тарифы]
[🆘 Нужна помощь]
```

### Для админа

| Команда | Что делает |
|---------|-----------|
| `/addclient <tg_id> <email> <панель> <id1> [id2]...` | Создать клиента |
| `/revoke <tg_id> [email]` | Удалить клиента |
| `/pausesub <tg_id> [email]` | Приостановить подписку |
| `/resumesub <tg_id> [email]` | Возобновить и продлить на дни паузы |
| `/extendsub <tg_id> <+N \| дата> [email]` | Продлить |
| `/listclients` | Список клиентов (постранично, по 5) |
| `/getlink <tg_id> [email]` | Получить ссылки клиента (см. ниже) |
| `/setcomment <tg_id> <email> <текст>` | Изменить комментарий клиента |
| `/rename <tg_id> <старый_email> <новый_email>` | Изменить email клиента |
| `/inbounds <панель>` | Список инбаундов с ID |
| `/status <панель>` | Подробный статус панели |
| `/listpanels` | Список панелей со статусом Xray |
| `/broadcast [tg_id] <текст>` | Рассылка всем клиентам или конкретному |
| `/report` | Дневной отчёт |
| `/guideadmin` | Гайд для админа |

**Reply-клавиатура:**

```
[➕ Добавить пользователя]
[✏️ Переименовать]  [💬 Комментарий]
[📅 Продлить]       [🗑️ Удалить]
```

### Для суперадмина (первый в списке)

Всё, что у админа, **плюс**:

| Команда | Что делает |
|---------|-----------|
| `/setting` | Добавить или обновить панель |
| `/delpanel <имя>` | Удалить панель из config.yml |
| `/sync` | Синхронизировать БД с панелью |
| `/guidesuper` | Гайд для суперадмина |

---

## 🔗 Выдача ссылок клиенту (`/getlink`)

Команда `/getlink <tg_id> [email]` показывает админу диалог с двумя вариантами:

```
🔗 Что выдать для клиента <tg_id>?
Найдено подписок: 1

[ 📦 Ссылка на подписку ]
[ 🧩 Ссылки на все конфигурации ]
[ ❌ Отмена ]
```

### 📦 Ссылка на подписку

Одна строка вида `https://<PANEL_IP>:<SUB_PORT>/<SUB_PATH>/<sub_id>`. Клиент импортирует её в приложение, и оно само подтягивает список серверов и автообновляется.

**Плюсы:** один тап, обновляется автоматически.
**Минусы:** sub-порт может блокироваться провайдером (типично для Туркменистана).

### 🧩 Ссылки на все конфигурации

Бот обращается к панели через API, получает готовые ссылки на **каждый инбаунд** отдельно и присылает их одним сообщением на каждую связку:

```
🧩 Конфигурации клиента <tg_id>
Панель: <panel>
Подписка: <email> — ▶️ активна

Найдено конфигов: 6

1. 🇹🇲 <inbound_name-email>
vless://<uuid>@<panel-host>:443?...

2. 🇹🇲 <inbound_name-email>
vless://<uuid>@<panel-host>:14839?...
...
```

Каждая ссылка обёрнута в `<code>` — тап по ней копирует целиком, а не открывает браузер.

**Плюсы:** работает даже когда sub-порт заблокирован; клиент может импортировать ссылки по одной.
**Минусы:** не обновляется автоматически, если в панели что-то поменялось.

### Когда что использовать

| Ситуация | Что давать |
|----------|------------|
| Клиент в РФ, обычный провайдер | 📦 Ссылку на подписку |
| Клиент в Туркменистане | 🧩 Прямые конфиги |
| Клиент на iOS с XHTTP-инбаундами | 🧩 Прямые конфиги (XHTTP на iOS не работает) |
| Не знаешь — начни с подписки | Если не заберётся — переключись на конфиги |

---

## 📋 Журнал действий (`audit.log`)

Все действия админов и суперадминов логируются в **`data/audit.log`**. Файл — обычный текст, по строке на событие.

### Формат записи

```
<YYYY-MM-DD HH:MM:SS> [<admin_tg_id> @<admin_username>] /<command> args=[...]
<YYYY-MM-DD HH:MM:SS> [<admin_tg_id> @<admin_username>] <action> <status> <details>
```

Пример:

```
2026-01-15 10:00:00 [123456789 @admin] /broadcast args=['123456789', 'Test', 'message']
2026-01-15 10:00:01 [123456789 @admin] broadcast OK recipients=1 delivered=1 failed=0
```

- **Дата и время** — в часовом поясе из `config.yml`.
- **`[TG_ID @username]`** — кто сделал.
- **Что сделал** — либо вызов команды (`/broadcast args=[...]`), либо результат операции (`broadcast OK ...`).

### Статусы операций

| Статус | Значение |
|--------|----------|
| **`OK`** | Операция полностью успешна. |
| **`PARTIAL`** | Частичный успех: часть связок обработана, часть — нет (или в панели ок, а в БД ошибка). |
| **`FAIL`** | Ни одна связка не обработана. Обычно причина указана в `reason="..."`. |

### Разделитель дней

Перед первой записью нового календарного дня в лог пишется визуальный блок:

```
╔══════════════════════════════════════════════════════════════════════════╗
║  📅  <YYYY-MM-DD>                                                        ║
╚══════════════════════════════════════════════════════════════════════════╝
```

Это позволяет быстро ориентироваться в больших логах и отделять события по дням.

### Ротация

При превышении файлом **1 МБ** — он переименовывается в `audit.log.1`, начинается новый `audit.log`. Старый `audit.log.1` при этом удаляется. Логика встроена в `audit.py`, никаких cron-задач настраивать не надо.

### Полезные команды для чтения

```bash
# Последние 20 событий
tail -20 <PROJECT_DIR>/XUIHelper/data/audit.log

# Только операции broadcast за всё время
grep ' broadcast ' <PROJECT_DIR>/XUIHelper/data/audit.log

# Только FAIL'ы — смотрим, что ломалось
grep 'FAIL' <PROJECT_DIR>/XUIHelper/data/audit.log

# Всё, что делал конкретный админ
grep '@<admin_username>' <PROJECT_DIR>/XUIHelper/data/audit.log

# Все успешные создания клиентов
grep 'addclient OK' <PROJECT_DIR>/XUIHelper/data/audit.log

# Всё за конкретный день
grep '^<YYYY-MM-DD>' <PROJECT_DIR>/XUIHelper/data/audit.log
```

---

## 🔧 Управление (Alpine / OpenRC)

```bash
rc-service xuihelper status    # статус
rc-service xuihelper restart   # перезапуск
rc-service xuihelper stop      # остановка
tail -f /var/log/xuihelper.err # логи в реальном времени
```

## 🔧 Управление (Debian / systemd)

```bash
systemctl status xuihelper
systemctl restart xuihelper
journalctl -u xuihelper -f
```

---

## ⁉️ FAQ

**В: Как узнать свой Telegram User ID?**
О: В Telegram напиши `@userinfobot`.

**В: Клиент не получает подписку.**
О: Клиент должен сначала нажать `/start` у бота. Telegram не даёт ботам писать первыми.

**В: Как узнать ID инбаунда?**
О: Команда `/inbounds <panel>` покажет все инбаунды с их ID и именами.

**В: Бот не подключается к Telegram (таймаут).**
О: Скорее всего, Telegram блокируется в вашем регионе. Настройте SSH SOCKS5-туннель (см. `MIGRATION.md`).

**В: Логи бота пишутся в `.err`, а не в `.log`.**
О: Это нормально. Python `logging` пишет в stderr. Все INFO/ERROR идут в `.err`.

**В: Sub-ссылка не открывается в Туркменистане.**
О: Провайдер блокирует sub-порт. Используй `/getlink` → **«🧩 Ссылки на все конфигурации»** — они идут на порты инбаундов, которые обычно не блокируются.

**В: Клиент с HWID-лимитом не может получить подписку.**
О: В 3x-ui 3.8.5+ при `limit_hwid > 0` sub-ссылка отдаётся **только** приложению, которое отправляет заголовок `X-HWID`. HAPP 4.6+ это умеет, старые версии — нет. Если клиент не может забрать подписку — снять HWID-лимит в панели или использовать `/getlink` → конфиги напрямую.

**В: HAPP не подключается к Hysteria2.**
О: Hysteria2 работает через UDP/QUIC, а некоторые провайдеры (особенно в Туркменистане) применяют QoS к UDP-трафику. Инбаунд исправен, но сеть его «душит». Оставить в подписке один рабочий протокол или дать клиенту конфиги VLESS.

**В: Несколько инбаундов в подписке — не работает ничего.**
О: HAPP пытается подключиться ко всем профилям **последовательно**, а не параллельно. Если один несовместим (например, XHTTP на iOS), клиент может зависнуть на нём и не переключиться. Решение — оставить в подписке только совместимые инбаунды или включить «Автовыбор сервера» в настройках HAPP.

**В: Как обновить бота?**
О:
```bash
cd <PROJECT_DIR>/XUIHelper
git pull
source .venv/bin/activate
pip install -r requirements.txt
deactivate
rc-service xuihelper restart
```

---

## 📄 Лицензия

MIT. См. файл `LICENSE`.