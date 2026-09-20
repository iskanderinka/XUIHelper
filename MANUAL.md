# Руководство по эксплуатации XUIHelper

## 1. Обзор

XUIHelper — Telegram-бот для управления несколькими панелями 3x-ui: создаёт клиентов, выдаёт ссылки подписки, следит за сроком действия, поддерживает паузу и продление, ведёт статистику трафика и присылает дневные отчёты.

Три роли:

- **Клиент**: получает подписку и кнопки быстрого доступа, отправляет заявки, смотрит свой гайд.
- **Администратор**: создаёт клиентов, управляет подписками, смотрит статистику.
- **Суперадмин** (первый в списке): управляет панелями (добавление, удаление).

## 2. Файлы проекта

| Файл | Назначение |
|------|-----------|
| `main.py` | Точка входа и хендлеры бота: команды, диалоги, callback-обработчики |
| `helpers.py` | Общие утилиты: форматирование, валидация, клавиатуры, работа с панелями и датами |
| `jobs.py` | Задачи по расписанию: снимок трафика, дневной отчёт, проверка панелей, напоминания |
| `config.py` | Работа с `config.yml`, валидация URL, суперадмин, чтение настроек |
| `database.py` | SQLite: трафик, привязки клиентов, пользователи бота, уведомления |
| `xui_api.py` | HTTP-клиент панели 3x-ui 3.8.x |
| `config.yml` | Личный конфиг (токен, панели, админы) — **не коммитится** |
| `config.yml.example` | Шаблон конфига |
| `MIGRATION.md` | Инструкция по переносу бота и настройке SSL |
| `data/traffic.db` | Локальная база данных |

## 3. Требования

- Python 3.11+
- Зависимости из `requirements.txt`

## 4. Подготовка

### 4.1. Получение токена бота

1. Telegram → `@BotFather` → `/newbot`.
2. Скопируй токен.

### 4.2. Получение Telegram User ID

1. Telegram → `@userinfobot` → `/start`.
2. Скопируй `Id`.

## 5. Первичная настройка

### 5.1. `config.yml`

Скопируй `config.yml.example` в `config.yml` и заполни:

```yaml
bot_token: "YOUR_TELEGRAM_BOT_TOKEN"

timezone: "Asia/Hong_Kong"

users:
  # Список ID администраторов. ПЕРВЫЙ — суперадмин.
  # Без суперадмина админские команды не работают.
  admin_users:
    - 197066617       # ← суперадмин
    - 123456789       # обычный админ (опционально)

panels:
  "TMT":
    url: "https://185.200.190.40:24487/x8UGpUW143YI9P3JVyQm"
    username: "mvernita"
    password: "..."
    sub_url: "https://185.200.190.40:2096/sub"

traffic:
  accounting_mode: unidirectional

policy:
  url: ""
  message: ""

tariffs:
  url: ""
  message: ""

guide:
  client:
    url: ""
    message: ""
  admin:
    url: ""
    message: ""
  superadmin:
    url: ""
    message: ""
```

**Важно:**

- URL панели должен содержать префикс (`/x8UGpUW143YI9P3JVyQm`), если он настроен.
- URL панели — с `127.0.0.1`, если бот и панель на одном сервере.
- URL панели — внешний, если бот на другой машине (см. `MIGRATION.md`).

### 5.2. Установка и запуск

```bash
pyenv local 3.11.9
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 main.py
```

Для фона — systemd (см. раздел 10) или Docker (раздел 11).

## 6. Команды

### 6.1. Для всех пользователей

| Команда | Описание |
|---------|----------|
| `/start` | Начать работу, зарегистрировать TG ID |
| `/help` | Справка (разная для каждой роли) |
| `/policy` | Политика конфиденциальности (если URL задан) |
| `/guide` | Гайд по боту |
| `/mylink` | Получить свою sub-ссылку |

### 6.2. Кнопки быстрого доступа клиента

Появляются автоматически после выдачи подписки:

| Кнопка | Действие |
|--------|----------|
| 🔗 Ссылка подписки | Присылает sub-ссылку |
| 📊 Тарифы | Сообщение с inline-кнопкой на страницу тарифов |
| 🆘 Нужна помощь | Уведомляет всех админов о запросе |

### 6.3. Заявка от клиента

При `/start` клиент видит inline-кнопку **«📝 Отправить заявку»**.

По нажатию:

- Кнопка исчезает, приветствие остаётся.
- Всем админам приходит уведомление:

```
🆕 Новая заявка 🆕

👤 Иван Иванов
🆔 123456789
🐶 @ivan

Важно: создавай клиента лишь тем, кто прошёл через тебя. Все заявки, которые ты не ждёшь, игнорируй!
```

### 6.4. Для администраторов

| Команда | Описание |
|---------|----------|
| `/addclient <tg_id> <email> <панель> <id1> [id2]...` | Создать клиента |
| `/revoke <tg_id> [email]` | Удалить клиента из панели и БД |
| `/pausesub <tg_id> [email]` | Приостановить подписку |
| `/resumesub <tg_id> [email]` | Возобновить и продлить на дни паузы |
| `/extendsub <tg_id> <+N \| дата> [email]` | Продлить подписку |
| `/listclients` | Список клиентов (постранично, по 5) |
| `/getlink <tg_id> [email]` | Получить sub-ссылку клиента |
| `/inbounds <панель>` | Список инбаундов с ID |
| `/status <панель>` | Подробный статус панели |
| `/listpanels` | Список панелей со статусом Xray |
| `/report` | Дневной отчёт по трафику |
| `/guideadmin` | Гайд для администратора |

### 6.5. Кнопки админской клавиатуры

Одна и та же клавиатура для админа и суперадмина:

| Кнопка | Действие |
|--------|----------|
| ➕ Добавить пользователя | Подсказка по `/addclient` |
| ⏸️ Пауза | Список клиентов → подтверждение паузы |
| ▶️ Продолжить | Список клиентов → подтверждение возобновления |
| 📅 Продлить | Список клиентов → готовая команда `/extendsub` |
| 🗑️ Удалить | Список клиентов → подтверждение удаления |

### 6.6. Только для суперадмина

| Команда | Описание |
|---------|----------|
| `/setting` | Диалог добавления или обновления панели |
| `/delpanel <имя>` | Удалить панель из конфига бота |
| `/guidesuper` | Гайд для суперадмина |

**Почему это критично:** `/setting` может подменить URL/логин/пароль панели, а `/delpanel` — отвязать панель от бота.

## 7. Сценарии работы

### 7.1. Выдача подписки новому клиенту

1. **Клиент** нажимает `/start` и **«📝 Отправить заявку»**.
2. **Админ** получает уведомление с TG ID, именем и username клиента.
3. **Админ** создаёт клиента:

   ```
   /addclient 123456789 user123 TMT 8 9
   ```

4. **Бот** спрашивает HWID лимит (0–11, где 0 — безлимит).
5. **Бот** спрашивает срок подписки — кнопками или датой `ГГГГ-ММ-ДД`.
6. **Бот** спрашивает комментарий (виден только админам).
7. **Бот** показывает превью и запрашивает подтверждение.
8. **Бот** создаёт клиента, отправляет клиенту sub-ссылку и reply-клавиатуру, админу — отчёт с `Sub ID`.

### 7.2. Повторная выдача ссылки

- Клиент нажимает **🔗 Ссылка подписки** или пишет `/mylink`.
- Если подписка на паузе — бот ответит «приостановлена».
- Админ может получить ссылку через `/getlink <tg_id> [email]`.

### 7.3. Пауза и возобновление

```
/pausesub 123456789 user123 → [✅ Да]
```

Клиент вернулся:

```
/resumesub 123456789 user123 → [✅ Да]
```

Бот продлит срок на число дней паузы. Для бессрочных подписок срок не меняется.

### 7.4. Продление

```
/extendsub 123456789 +30              # +30 дней
/extendsub 123456789 2027-01-01       # до даты
/extendsub 123456789 +30 user123      # конкретная связка
```

### 7.5. Удаление

```
/revoke 123456789 user123 → [✅ Да]
```

Удаляет клиента из панели **и** из БД бота.

## 8. Автоматические задачи

| Задача | Когда | Что делает |
|--------|-------|-----------|
| `check_inbounds_job` | Каждые 6 часов | Проверяет связь с панелями, алерт при недоступности |
| `record_traffic_job` | Ежедневно в 23:50 | Снимок трафика всех клиентов |
| `daily_report_job` | Ежедневно в 8:00 (если включён) | Дневной отчёт админам |
| `expiry_notification_job` | Ежедневно в 9:00 | Напоминания за 7 и 3 дня, алерт админам после истечения |

Время — в часовом поясе из `config.yml` (по умолчанию `Asia/Hong_Kong`).

**Отчёт заработает через сутки** — нужно два снимка трафика.

## 9. Суперадмин

**Первый ID в `users.admin_users`.**

Если его нет — **все админские команды заблокированы**, бот напишет: «Суперадмин не назначен. Зайди на сервер и добавь ID первым в `users.admin_users` файла `config.yml`».

Чтобы назначить/сменить — правь `config.yml` руками и перезапускай бота.

## 10. Развёртывание

### 10.1. Выбор сервера

Возможны три схемы:

| Схема | Когда подходит |
|-------|----------------|
| Бот и панель на одном сервере | Панель и бот делят 1 ГБ+ RAM, IP сервера не блокируется РКН |
| Бот на отдельном сервере в РФ | Панель отдельно, бот в РФ, между ними SSH-туннель |
| Бот на зарубежном сервере | Панель и бот раздельно, Telegram доступен напрямую |

Эта инструкция описывает **вторую схему** — бот в РФ, панель за границей, SSH-туннель для Telegram.

### 10.2. Установка зависимостей (Alpine)

```bash
apk add --no-cache git python3 py3-pip gcc make musl-dev \
    python3-dev libffi-dev openssl-dev yaml-dev tzdata logrotate
```

### 10.3. Клонирование

```bash
cd /root
git clone https://github.com/iskanderinka/XUIHelper.git
cd XUIHelper
```

### 10.4. Окружение

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 10.5. Конфигурация

Скопируй `config.yml` с рабочей машины через scp (там уже настроены токены, панели, ссылки):

```bash
# С ноутбука
scp ~/XUIHelper/config.yml root@YOUR_RU_SERVER:/root/XUIHelper/config.yml
```

Или заполни `config.yml` на месте — см. раздел 5.

**Важно:** URL панели должен быть **внешним** (не `127.0.0.1`), потому что панель на другом сервере.

### 10.6. SSH SOCKS5-туннель

**Проверь, доступен ли Telegram напрямую:**

```bash
curl -s --max-time 10 https://api.telegram.org/bot123:test/getMe
```

**Если ответ `{"ok":false,...}` — Telegram доступен, туннель не нужен.** Пропусти этот раздел.

**Если timeout** — туннель обязателен.

```bash
# Ключ
ssh-keygen -t ed25519 -C "tunnel@xuihelper" -f ~/.ssh/id_ed25519 -N ""

# Копирование ключа на зарубежный сервер
ssh-copy-id -i ~/.ssh/id_ed25519.pub root@YOUR_REMOTE_SERVER

# Проверка
ssh -o PasswordAuthentication=no root@YOUR_REMOTE_SERVER "echo OK"
```

Создай `/etc/init.d/tg-tunnel`:

```bash
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

**Проверка:**

```bash
ss -tlnp | grep 1080
curl -s --max-time 10 --proxy socks5h://127.0.0.1:1080 https://api.telegram.org/bot123:test/getMe
```

**Важно:** используй `socks5h://` (а не `socks5://`) — тогда DNS-резолв произойдёт на удалённом сервере. Иначе curl будет резолвить у себя, найдёт IPv6 адрес Telegram, а у удалённого сервера может не быть IPv6-маршрута.

### 10.7. OpenRC-сервис для бота

```bash
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

**Проверка:**

```bash
rc-service xuihelper status
tail -20 /var/log/xuihelper.err
```

### 10.8. Logrotate

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

## 11. Развёртывание через Docker (опционально)

Подходит только для серверов с RAM ≥ 1 ГБ.

### 11.1. Подготовка

Установи [Docker](https://docs.docker.com/engine/install/) и [Docker Compose](https://docs.docker.com/compose/install/).

### 11.2. Запуск

```bash
docker-compose up -d --build
```

### 11.3. Управление

```bash
docker-compose logs -f
docker-compose down
docker-compose up -d --build
```

## 12. FAQ

**В: Клиент не получает подписку.**
О: Проверь, что он нажал `/start`. Без этого Telegram не даёт ботам писать первыми.

**В: Как узнать ID инбаунда?**
О: `/inbounds TMT` покажет список.

**В: Клиент просит вернуть ссылку.**
О: `/getlink 123456789 user123` — ссылка придёт в личку админу.

**В: Почему `/report` показывает «данные недоступны»?**
О: Отчёту нужны два снимка трафика (за вчера и позавчера). Первые сутки — не работает.

**В: Забыл, на каком инбаунде сидит клиент.**
О: `/listclients` — увидишь список с инбаундами и комментариями.

**В: Что делать, если панель отключена (`disabled: true`)?**
О: Включи её в `config.yml` или через `/setting` (суперадмин). Пока отключена — все операции с ней блокируются.