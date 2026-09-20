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
├── Dockerfile            # Опционально: сборка образа
├── docker-compose.yml    # Опционально: запуск в Docker
├── .gitignore            # Что не коммитить
├── .dockerignore         # Что не копировать в образ
├── .flake8               # Конфиг линтера
│
├── README.md             # Это руководство
├── MANUAL.md             # Подробное руководство
├── MIGRATION.md          # Инструкция по переезду и SSL
├── LICENSE               # MIT
│
├── data/                 # SQLite-база (не коммитится)
└── .venv/                # Python venv (не коммитится)
```

| Файл | Что делает |
|------|-----------|
| `main.py` | Регистрирует команды, диалоги `/setting` и `/addclient`, callback-обработчики |
| `helpers.py` | Хелперы для `main.py` и `jobs.py`: форматирование, валидация, клавиатуры, работа с панелями |
| `jobs.py` | Задачи по расписанию: снимок трафика, отчёт, проверка панелей, напоминания |
| `config.py` | Читает и пишет `config.yml`, валидирует URL, проверяет суперадмина |
| `database.py` | Четыре таблицы: `traffic_records`, `bot_users`, `client_bindings`, `notification_log` |
| `xui_api.py` | Общается с панелью 3x-ui по HTTPS |

---

## 🚀 Быстрый старт

### Вариант A. venv + systemd (рекомендуемый для VPS с ограниченной RAM)

**Требования:** Debian/Ubuntu, Python 3.11+, root-доступ.

1. **Установить Python 3.11** (если на сервере < 3.11):

   ```bash
   apt update
   apt install -y make build-essential libssl-dev zlib1g-dev \
       libbz2-dev libreadline-dev libsqlite3-dev wget curl llvm \
       libncursesw5-dev xz-utils tk-dev libxml2-dev libxmlsec1-dev \
       libffi-dev liblzma-dev

   curl https://pyenv.run | bash

   echo 'export PYENV_ROOT="$HOME/.pyenv"' >> ~/.bashrc
   echo 'export PATH="$PYENV_ROOT/bin:$PATH"' >> ~/.bashrc
   echo 'eval "$(pyenv init -)"' >> ~/.bashrc
   source ~/.bashrc

   pyenv install 3.11.9
   ```

2. **Клонировать проект:**

   ```bash
   cd /root
   git clone https://github.com/<твой-аккаунт>/XUIHelper.git
   cd XUIHelper
   pyenv local 3.11.9
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Настроить `config.yml`:**

   ```bash
   cp config.yml.example config.yml
   vi config.yml
   ```

4. **Создать systemd-сервис** — см. `MANUAL.md`, раздел 10.

5. **Запустить:**

   ```bash
   systemctl start xuihelper
   systemctl status xuihelper
   ```

### Вариант B. Docker (для серверов с RAM ≥ 1 ГБ)

```bash
git clone https://github.com/<твой-аккаунт>/XUIHelper.git
cd XUIHelper
cp config.yml.example config.yml
vi config.yml
docker-compose up -d --build
```

---

## 📖 Работа с ботом

### Для клиента

| Команда | Что делает |
|---------|-----------|
| `/start` | Активировать бота, получить TG ID |
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

**Заявка:** при `/start` появляется inline-кнопка «📝 Отправить заявку». По нажатию кнопка исчезает, админам приходит уведомление с TG ID, именем и username клиента.

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
| `/guidesuper` | Гайд для суперадмина |

---

## 🔧 Управление (systemd)

```bash
systemctl status xuihelper    # статус
systemctl restart xuihelper   # перезапуск
systemctl stop xuihelper      # остановка
journalctl -u xuihelper -f    # логи в реальном времени
journalctl -u xuihelper -n 100  # последние 100 строк
```

## 🐳 Управление (Docker)

```bash
docker-compose logs -f
docker-compose down
docker-compose up -d --build
```

---

## ⁉️ FAQ

**В: Как узнать свой Telegram User ID?**
О: В Telegram напиши `@userinfobot`.

**В: Клиент не получает подписку.**
О: Клиент должен сначала нажать `/start` у бота. Telegram не даёт ботам писать первыми.

**В: Как узнать ID инбаунда?**
О: Команда `/inbounds TMT` покажет все инбаунды с их ID.

**В: Где посмотреть логи?**
О: `journalctl -u xuihelper -f` (systemd) или `docker-compose logs -f` (Docker).

**В: Как обновить бота?**
О:
- venv: `cd /root/XUIHelper && git pull && source .venv/bin/activate && pip install -r requirements.txt && systemctl restart xuihelper`
- Docker: `docker-compose down && git pull && docker-compose up -d --build`

**В: Можно ли запускать второй диалог, пока первый активен?**
О: Нет. Но если запустишь — бот **автоматически отменит** первый и продолжит второй. Ничего не сломается.

---

## 📄 Лицензия

MIT. См. файл `LICENSE`.