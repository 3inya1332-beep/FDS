# ADS Browser + Coda.io inviter

Скрипт автоматизирует рассылку инвайтов в `coda.io` через **ADS Browser API**:

1. Запускает профиль ADS Browser по `profile_id`.
2. Открывает ссылку документа Coda из профиля.
3. Нажимает кнопку `Share`.
4. Вставляет email-адреса батчами по 30 (каждый email подтверждается `Enter`).
5. Вставляет текст сообщения из `edit/text.txt`.
6. Нажимает `Invite`.
7. Удаляет успешно отправленные email из `email/emails.txt`.
8. Повторяет цикл, пока адреса не закончатся.

## Структура

- `main.py` — консольное меню запуска.
- `config.py` — настройки ADS API и поведения скрипта.
- `ads_api.py` — запросы к локальному ADS API.
- `profile_store.py` — база профилей (`sqlite`).
- `coda_automation.py` — действия в интерфейсе Coda через Playwright.
- `edit/text.txt` — текст сообщения для инвайта.
- `email/emails.txt` — список email (по одному в строке).
- `logs/sent_emails.txt` — история отправленных email.

## Установка

```bash
pip install -r requirements.txt
```

## Настройка

Откройте `config.py` и укажите:

- `LOCAL_API_BASE` (обычно `http://127.0.0.1:50325`)
- `API_KEY` (если используется вашей версией ADS)
- при необходимости `ADS_HEADLESS`, `BATCH_SIZE`, `BATCH_DELAY_SECONDS`
- `BATCH_SIZE` по умолчанию `30` (ограничение Coda на один invite)

## Запуск

```bash
python main.py
```

В меню:

1. Добавьте профиль ADS (введите `ADS profile id` и URL).
2. Выберите запуск автоматизации рассылки.

## Важно

- Скрипт использует **запросы к ADS API** и DOM-автоматизацию (без координат/скрин-кликов).
- Перед запуском профиль ADS должен быть уже готов (авторизация в Coda уже выполнена).
- Если в `email/` есть другой `.txt`, скрипт сможет взять его, если `emails.txt` отсутствует.
