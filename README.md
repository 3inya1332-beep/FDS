# ADS Browser + Inflow sender

Скрипт автоматизирует отправку **Purchase Order email** в `https://app.inflowinventory.com/`
через **ADS Browser API**.

Как работает:

1. Запускает профиль ADS Browser по `profile_id`.
2. Открывает URL, который вы указали у профиля (страница заказа в Inflow).
3. Нажимает `Email` (справа вверху).
4. Выбирает `Purchase order`.
5. Заполняет поля:
   - `To` / `Cc` / `Bcc` — из файла email по порядку, по 3 адреса на одну отправку.
   - `Subject` — из `edit/SUBJECT.txt`.
   - `Message` — из `edit/MESSAGE.txt`.
6. Нажимает `Send`.
7. Повторяет отправку, пока есть полные тройки email (TO/CC/BCC).
8. Если email закончились, завершает работу.

## Структура

- `main.py` — консольное меню запуска.
- `config.py` — настройки ADS API и поведения скрипта.
- `ads_api.py` — запросы к локальному ADS API.
- `profile_store.py` — база профилей (`sqlite`).
- `inflow_automation.py` — действия в интерфейсе Inflow через Playwright.
- `edit/SUBJECT.txt` — тема письма.
- `edit/MESSAGE.txt` — текст письма.
- `email/emails.txt` (или `email/email.txt`) — список email (по одному в строке).

## Установка

```bash
pip install -r requirements.txt
```

## Настройка

Откройте `config.py` и укажите:

- `LOCAL_API_BASE` (обычно `http://127.0.0.1:50325`)
- `API_KEY` (если используется вашей версией ADS)
- при необходимости `ADS_HEADLESS`, `SEND_DELAY_SECONDS`

## Запуск

```bash
python main.py
```

В меню:

1. Добавьте профиль ADS (введите `ADS profile id` и URL).
2. Выберите запуск автоматизации.

## Важно

- Скрипт использует **запросы к ADS API** и DOM-автоматизацию (без координат/скрин-кликов).
- Перед запуском профиль ADS должен быть уже готов (авторизация в Inflow уже выполнена).
- Для каждой отправки нужна полная тройка email: TO, CC, BCC.
- Если email не кратны 3, остаток (1-2 адреса) пропускается.
