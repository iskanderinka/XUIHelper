# XUIHelper

Telegram-бот для управления несколькими панелями 3x-ui: создание клиентов, выдача подписок, управление сроками, статистика трафика.

---

## 🙏 Благодарности

Идея и структура проекта вдохновлены [TgXUIMgr](https://github.com/GeQainZz/TgXUIMgr).

XUIHelper — **переработанная и расширенная версия**: перевод на русский, отказ от веб-админки, поддержка API 3x-ui 3.8.0+, роль суперадмина, пауза/продление подписок, комментарии к клиентам, автозадачи, аудит безопасности.

---

## ✨ Основные возможности

- **Мультипанельность**: один бот — все ваши панели 3x-ui.
- **Выдача подписок через бота**: клиент нажимает `/start`, отправляет заявку, админ создаёт клиента — подписка приходит автоматически.
- **Пауза и продление**: `/pausesub` замораживает срок, `/resumesub` продлевает на дни паузы, `/extendsub` продлевает вручную.
- **Автоматические задачи**: напоминания клиентам за 7 и 3 дня, алерт админам после истечения, дневной отчёт, проверка панелей каждые 6 часов.
- **Роль суперадмина**: чувствительные команды (`/setting`, `/delpanel`) доступны только первому в списке админов.
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
├── xui_api.py            # HTTP-клиент панели 3x-ui 3.8.x
│
├── config.yml            # Личный конфиг (не коммитится)
├── config.yml.example    # Шаблон конфига
├── requirements.txt      # Зависимости (с пинами версий)
│
├── README.md             # Это руководство
├── MANUAL.md             # Подробное руководство
├── MIGRATION.md          # Инструкция по развёртыванию и переезду
├── LICENSE               # MIT
├── ver.md                # Описание мажорных и минорных версий
│
├── data/                 # SQLite-база (не коммитится)
└── .venv/                # Python venv (не коммитится)
```

| Файл | Что делает |
|------|-----------|
| `main.py` | Регистрирует команды, диалоги `/setting` и `/addclient`, callback-обработчики |
| `helpers.py` | Хелперы для `main.py` и `jobs.py`: форматирование, валидация, клавиатуры |
| `jobs.py` | Задачи по расписанию: снимок трафика, отчёт, проверка панелей, напоминания |
| `config.py` | Читает и пишет `config.yml`, валидирует URL, проверяет суперадмина |
| `database.py` | Четыре таблицы: `traffic_records`, `bot_users`, `client_bindings`, `notification_log` |
| `xui_api.py` | Общается с панелью 3x-ui по HTTPS |

---

## 🚀 Установка

### Требования

- VPS с Linux (Alpine / Debian / Ubuntu).
- Python 3.11+.
- Git, curl, gcc/make.
- **SSH-доступ к панели 3x-ui** — если Telegram блокируется в вашем регионе (Россия).

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
cd /root
git clone https://github.com/iskanderinka/XUIHelper.git
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
    - 197066617       # ← суперадмин (первый)

panels:
  "TMT":
    url: "https://panel-host:port/PATH"
    username: "admin"
    password: "..."
    sub_url: "https://panel-host:port/sub"

timezone: "Asia/Hong_Kong"
```

### Шаг 4. Прокси для Telegram (если нужно)

**Если Telegram доступен с вашего сервера напрямую** — пропустите этот шаг.

**Если Telegram блокируется** (типично для РФ) — нужен SOCKS5-туннель через сервер за границей:

```bash
# Создаём SSH-ключ
ssh-keygen -t ed25519 -C "tunnel@xuihelper" -f ~/.ssh/id_ed25519 -N ""

# Копируем публичный ключ на удалённый сервер
ssh-copy-id -i ~/.ssh/id_ed25519.pub root@YOUR_REMOTE_SERVER

# Проверяем, что работает без пароля
ssh -o PasswordAuthentication=no root@YOUR_REMOTE_SERVER "echo OK"
```

Подробности — в `MIGRATION.md`, раздел «Туннель для Telegram».

### Шаг 5. Проверка запуска

```bash
cd /root/XUIHelper
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
    root@YOUR_REMOTE_SERVER"

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

command="/root/XUIHelper/.venv/bin/python"
command_args="-u main.py"
command_user="root:root"
directory="/root/XUIHelper"
pidfile="/run/${RC_SVCNAME}.pid"

command_background="yes"
output_log="/var/log/xuihelper.log"
error_log="/var/log/xuihelper.err"

export HTTPS_PROXY="socks5://127.0.0.1:1080"
export HTTP_PROXY="socks5://127.0.0.1:1080"
export ALL_PROXY="socks5://127.0.0.1:1080"

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
| `/listclients` | Список клиентов (постранично) |
| `/getlink <tg_id> [email]` | Получить sub-ссылку |
| `/setcomment <tg_id> <email> <текст>` | Изменить комментарий клиента |
| `/rename <tg_id> <старый_email> <новый_email>` | Изменить email клиента |
| `/inbounds <панель>` | Список инбаундов с ID |
| `/status <панель>` | Подробный статус панели |
| `/listpanels` | Список панелей со статусом |
| `/report` | Дневной отчёт |
| `/guideadmin` | Гайд для админа |

**Reply-клавиатура:**

```
[➕ Добавить пользователя]
[⏸️ Пауза]       [▶️ Продолжить]
[📅 Продлить]    [🗑️ Удалить]
```

### Для суперадмина (первый в списке)

Всё, что у админа, **плюс**:

| Команда | Что делает |
|---------|-----------|
| `/setting` | Добавить или обновить панель |
| `/delpanel <имя>` | Удалить панель из config.yml |
| `/sync` | Синхронизировать БД с панелью (если менял что-то руками) |
| `/guidesuper` | Гайд для суперадмина |


### Синхронизация с панелью

Обычно бот сам пишет изменения **и** в БД, **и** в панель. Но если админ менял что-то в панели **руками** (email, комментарий, HWID, срок) — данные разошлись.

**Когда что использовать:**

| Ситуация | Команда |
|----------|---------|
| Хочу изменить комментарий через бота | `/setcomment` |
| Хочу изменить email через бота | `/rename` |
| Менял что-то в панели руками | `/sync` (только суперадмин) |

**`/sync`** обходит все панели, находит связки по UUID и обновляет в БД: email, комментарий, HWID, срок действия. История трафика тоже перепривязывается к новому email.

**Важно:** `/sync` не трогает клиентов, созданных в панели **вручную** (их нет в БД). Он работает только с теми, кто создан через бота.

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
О: Команда `/inbounds TMT` покажет все инбаунды с их ID.

**В: Бот не подключается к Telegram (таймаут).**
О: Скорее всего, Telegram блокируется в вашем регионе. Настройте SSH SOCKS5-туннель (см. `MIGRATION.md`).

**В: Логи бота пишутся в `.err`, а не в `.log`.**
О: Это нормально. Python `logging` пишет в stderr. Все INFO/ERROR идут в `.err`.

**В: Как обновить бота?**
О:
```bash
cd /root/XUIHelper
git pull
source .venv/bin/activate
pip install -r requirements.txt
deactivate
rc-service xuihelper restart
```

---

## 📄 Лицензия

MIT. См. файл `LICENSE`.