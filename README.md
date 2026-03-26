# Kleinanzeigen Auto-Reg Bot

Telegram-бот для автоматической регистрации аккаунтов на [kleinanzeigen.de](https://www.kleinanzeigen.de).

## Возможности

- Пошаговый диалог: прокси → email → пароль email → телефон
- Автозаполнение формы регистрации через Playwright (Chromium)
- Автоматическая проверка письма верификации через IMAP
- Ввод SMS-кода для подтверждения телефона
- Поддержка HTTP-прокси с авторизацией и без
- Генерация случайного надёжного пароля для аккаунта
- Режим headless для работы на VDS

---

## Структура проекта

```
Autoreg/
├── bot.py            — Telegram-бот (точка входа)
├── registrar.py      — Автоматизация браузера (Playwright)
├── email_helper.py   — Чтение письма верификации (IMAP)
├── config.py         — Конфигурация из .env
├── requirements.txt  — Зависимости
├── .env.example      — Пример файла переменных окружения
└── README.md
```

---

## Установка на VDS (Ubuntu/Debian)

### 1. Установить Python 3.11+

```bash
sudo apt update && sudo apt install python3 python3-pip python3-venv -y
```

### 2. Клонировать / загрузить проект

```bash
cd /opt
# скопируйте папку Autoreg на сервер (scp, sftp, git и т.д.)
cd Autoreg
```

### 3. Создать виртуальное окружение и установить зависимости

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium
```

### 4. Создать файл `.env`

```bash
cp .env.example .env
nano .env
```

Заполните:

```env
BOT_TOKEN=123456789:AABBCCDDEEFFaabbccddeeff1234567890
ADMIN_IDS=ваш_telegram_id
HEADLESS=True
EMAIL_CHECK_TIMEOUT=180
```

> Ваш Telegram ID можно узнать у бота [@userinfobot](https://t.me/userinfobot).

### 5. Запустить бота

```bash
python bot.py
```

Для фонового запуска используйте `screen` или `systemd`:

```bash
# через screen
screen -S autoreg
source venv/bin/activate
python bot.py
# Ctrl+A, D — отключиться от screen без остановки
```

---

## Использование в Telegram

| Команда   | Описание                        |
|-----------|---------------------------------|
| `/start`  | Приветствие                     |
| `/reg`    | Начать регистрацию аккаунта     |
| `/cancel` | Отменить текущий процесс        |
| `/help`   | Справка по форматам             |

### Диалог регистрации

```
/reg
  ↓
Введите прокси:     1.2.3.4:8080  или  1.2.3.4:8080:login:pass  или  -
  ↓
Введите email:      example@gmail.com
  ↓
Введите пароль от email (для IMAP):  emailpassword123
  ↓
Введите телефон:    +491234567890  или  -
  ↓
[Бот запускает браузер, заполняет форму, ждёт письмо, верифицирует]
  ↓
Введите SMS-код:    123456
  ↓
✅ Аккаунт создан! Email / пароль выведены в чат.
```

---

## Форматы прокси

| Формат                      | Описание                        |
|-----------------------------|---------------------------------|
| `ip:port`                   | HTTP-прокси без авторизации     |
| `ip:port:login:password`    | HTTP-прокси с авторизацией      |
| `-`                         | Без прокси (прямое подключение) |

---

## Email-провайдеры с поддержкой IMAP

Бот автоматически определяет IMAP-сервер по домену email.  
Поддерживаются: Gmail, Outlook, Hotmail, Yahoo, web.de, GMX, t-online.de, mail.ru, yandex.ru и другие.

> **Gmail**: перед использованием включите IMAP и создайте «Пароль приложения» в настройках Google-аккаунта (если включена двухфакторная аутентификация).

---

## Важные замечания

- Kleinanzeigen.de использует защиту от ботов. При блокировке попробуйте другой прокси или увеличьте задержки в `registrar.py`.
- Если изменился дизайн сайта, CSS-селекторы в `registrar.py` могут потребовать обновления.
- Бот не сохраняет пароли пользователей — они используются только в памяти во время сессии.
