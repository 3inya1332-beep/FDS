# ADS Browser + Calendly automation

## Структура

- `main.py` — консольное меню запуска.
- `config.py` — настройки ADS API, AnyMessage API, 9proxy и путей.
- `ads_api.py` — запросы к локальному ADS API.
- `anymessage_api.py` — покупка email и ожидание письма подтверждения.
- `proxy_manager.py` — смена прокси через 9proxy API (только на ошибках).
- `calendly_automation.py` — сценарии регистрации/настройки/бронирования в Calendly.
- `profile_store.py` — база профилей (`sqlite`).
- `edit/subject.txt` — шаблон Subject для Email confirmation.
- `edit/body.txt` — шаблон Body для Email confirmation.
- `email/emails.txt` — список email (по одному в строке).
- `coockie/` — сохраненные cookies профилей.

## Установка

```bash
pip install -r requirements.txt
playwright install chromium
```

## Настройка

Откройте `config.py` и укажите:

- `LOCAL_API_BASE` (обычно `http://127.0.0.1:50325`)
- `API_KEY` (если используется вашей версией ADS)
- `ANYMESSAGE_TOKEN` и параметры домена/site
- параметры `NINEPROXY_*` (если нужна аварийная смена proxy)

## Запуск

```bash
python main.py
```

В меню:

1. Регистрация аккаунта Calendly через ADS + AnyMessage.
2. Ручное добавление профиля.
3. Привязка ссылок (`meeting_types` и `copy link`).
4. Настройка шаблонов `Email confirmation`.
5. Цикл `Schedule Event` по email-списку.

## Важно

- Скрипт использует запросы к ADS API и UI-автоматизацию через Playwright CDP.
- Cookies сохраняются в папку `coockie` после регистрации/настройки/отправки.
- Смена proxy не делается после каждого батча, только при ошибках регистрации.
