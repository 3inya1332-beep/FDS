# ADS Browser + pCloud inviter

Скрипт автоматизирует работу с `https://my.pcloud.com/` через **ADS Browser API**:

1. Запускает профиль ADS Browser по `profile_id`.
2. Открывает `my.pcloud.com`.
3. Создает папку (имя берется из `edit/NAME.txt`).
4. Открывает `Invite to folder` для этой папки.
5. Вставляет email-адреса батчами по 20 (каждый email подтверждается `Enter`).
6. Вставляет сообщение из `edit/text.txt`.
7. Нажимает `Share`.
8. Повторяет, пока email не закончатся.

## Структура

- `main.py` — консольное меню запуска.
- `config.py` — настройки ADS API и поведения скрипта.
- `ads_api.py` — запросы к локальному ADS API.
- `profile_store.py` — база профилей (`sqlite`).
- `pcloud_automation.py` — действия в интерфейсе pCloud через Playwright.
- `edit/NAME.txt` — имя создаваемой папки.
- `edit/text.txt` — текст сообщения для инвайта.
- `email/emails.txt` — список email (по одному в строке).

## Установка

```bash
pip install -r requirements.txt
```

## Настройка

Откройте `config.py` и укажите:

- `LOCAL_API_BASE` (обычно `http://127.0.0.1:50325`)
- `API_KEY` (если используется вашей версией ADS)
- при необходимости `ADS_HEADLESS`, `BATCH_SIZE`, `BATCH_DELAY_SECONDS`

## Запуск

```bash
python main.py
```

В меню:

1. Добавьте профиль ADS (введите `ADS profile id` и URL).
2. Выберите запуск автоматизации.

## Важно

- Скрипт использует **запросы к ADS API** и DOM-автоматизацию (без координат/скрин-кликов).
- Перед запуском профиль ADS должен быть уже готов (авторизация в pCloud выполнена).
- Если в `email/` есть другой `.txt`, скрипт сможет взять его, если `emails.txt` отсутствует.
