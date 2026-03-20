from __future__ import annotations

import os
import sqlite3
from typing import Iterable

from ads_api import AdsApiError
from config import DEFAULT_INFLOW_URL, LOGS_DIR
from inflow_automation import (
    InflowAutomationError,
    ensure_input_files,
    get_dashboard_stats,
    register_new_account_for_profile,
    run_inbox_check,
    run_job,
)
from profile_store import Profile, ProfileStore
from runtime_settings import RuntimeSettingsStore


def clear_console() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def _print_box(lines: list[str], title: str = "") -> None:
    width = max(70, *(len(line) + 4 for line in lines + ([title] if title else [])))
    top = f"╔{'═' * (width - 2)}╗"
    bottom = f"╚{'═' * (width - 2)}╝"
    print(top)
    if title:
        title_line = f"  {title}  "
        title_line = title_line[: width - 4]
        print(f"║{title_line.ljust(width - 2)}║")
        print(f"╠{'═' * (width - 2)}╣")
    for line in lines:
        print(f"║ {line.ljust(width - 4)} ║")
    print(bottom)


def print_header() -> None:
    stats = get_dashboard_stats()
    banner_lines = [
        "🚀 ADS + INFLOW PURCHASE ORDER SENDER",
        f"📥 Всего email в файле: {stats.total_input_emails}",
        f"✅ Уже отправлено (из файла): {stats.already_sent_emails}",
        f"🕓 Еще не отправлено: {stats.unsent_emails}",
    ]
    _print_box(banner_lines, title="СТАТУС РАССЫЛКИ")


def print_profiles(profiles: Iterable[Profile]) -> None:
    profiles = list(profiles)
    if not profiles:
        print("⚠️ Профилей пока нет.")
        return
    print("\n📂 Сохраненные ADS профили:")
    for profile in profiles:
        print(
            f"  [{profile.local_id}] {profile.name} | ADS ID: {profile.ads_profile_id} | URL: {profile.start_url}"
        )


def ask_int(prompt: str) -> int | None:
    raw = input(prompt).strip()
    if not raw:
        return None
    if not raw.isdigit():
        print("Нужно ввести число.")
        return None
    return int(raw)


def ask_yes_no(prompt: str, *, default: bool = False) -> bool:
    raw = input(prompt).strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes", "1", "да", "д"}


def _mask_secret(value: str) -> str:
    value = value.strip()
    if not value:
        return "(пусто)"
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:3]}***{value[-2:]}"


def _read_last_log_lines(limit: int = 8) -> list[str]:
    log_path = LOGS_DIR / "automation.log"
    if not log_path.exists():
        return []
    lines = [line.strip() for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return lines[-limit:]


def _format_exc(exc: BaseException) -> str:
    message = str(exc).strip()
    if message:
        return message
    return f"{exc.__class__.__name__}: {exc!r}"


def show_api_settings(settings_store: RuntimeSettingsStore) -> None:
    settings = settings_store.load()
    lines = [
        f"ADS API KEY: {_mask_secret(settings.ads_api_key)}",
        f"ANYMESSAGE TOKEN: {_mask_secret(settings.anymessage_api_token)}",
        f"PROXY API: {settings.proxy_api_url or '(пусто)'}",
    ]
    _print_box(lines, title="API / PROXY SETTINGS")


def _setup_ads_api_flow(settings_store: RuntimeSettingsStore) -> None:
    settings = settings_store.load()
    ads_key = input(f"ADS API KEY [{_mask_secret(settings.ads_api_key)}]: ").strip()
    if ads_key:
        settings.ads_api_key = ads_key
    settings_store.save(settings)
    print("✅ ADS API настройки сохранены.")


def _setup_anymessage_api_flow(settings_store: RuntimeSettingsStore) -> None:
    settings = settings_store.load()
    anymessage_token = input(f"ANYMESSAGE TOKEN [{_mask_secret(settings.anymessage_api_token)}]: ").strip()
    if anymessage_token:
        settings.anymessage_api_token = anymessage_token
    settings_store.save(settings)
    print("✅ AnyMessage API настройки сохранены.")


def _setup_proxy_flow(settings_store: RuntimeSettingsStore) -> None:
    settings = settings_store.load()
    settings.proxy_api_url = (
        input(f"PROXY API URL [{settings.proxy_api_url}]: ").strip() or settings.proxy_api_url
    )
    settings_store.save(settings)
    print("✅ Proxy настройки сохранены.")


def setup_api_flow(settings_store: RuntimeSettingsStore) -> None:
    while True:
        clear_console()
        show_api_settings(settings_store)
        print("\n⚙️ НАСТРОЙКА API (ВЫБОР БЛОКА)")
        print("1. API ADS")
        print("2. API AnyMessage")
        print("3. API PROXY")
        print("0. Назад")
        choice = input("\n👉 Выберите блок: ").strip()

        if choice == "1":
            _setup_ads_api_flow(settings_store)
        elif choice == "2":
            _setup_anymessage_api_flow(settings_store)
        elif choice == "3":
            _setup_proxy_flow(settings_store)
        elif choice == "0":
            return
        else:
            print("⚠️ Неизвестный пункт.")

        input("\nНажмите Enter, чтобы продолжить...")


def add_profile_flow(store: ProfileStore) -> None:
    print("\n➕ Добавление ADS профиля")
    name = input("Название профиля (любое): ").strip()
    ads_profile_id = input("ADS profile id: ").strip()
    start_url = input(f"URL старта [{DEFAULT_INFLOW_URL}]: ").strip() or DEFAULT_INFLOW_URL

    if not name or not ads_profile_id:
        print("Название и ADS profile id обязательны.")
        return
    try:
        store.add_profile(name=name, ads_profile_id=ads_profile_id, start_url=start_url)
        print("✅ Профиль добавлен.")
    except sqlite3.IntegrityError:
        print("⚠️ Такой ADS profile id уже существует в базе.")


def delete_profile_flow(store: ProfileStore) -> None:
    profiles = list(store.list_profiles())
    print_profiles(profiles)
    if not profiles:
        return
    selected = ask_int("Введите локальный ID профиля для удаления: ")
    if selected is None:
        return
    if store.delete_profile(selected):
        print("🗑️ Профиль удален.")
    else:
        print("⚠️ Профиль не найден.")


def run_flow(store: ProfileStore, settings_store: RuntimeSettingsStore) -> None:
    profiles = list(store.list_profiles())
    print_profiles(profiles)
    if not profiles:
        print("⚠️ Сначала добавьте профиль.")
        return

    selected = ask_int("Введите локальный ID профиля для запуска: ")
    if selected is None:
        return

    profile = store.get_profile(selected)
    if profile is None:
        print("⚠️ Профиль не найден.")
        return

    registration_name = input(
        f"Имя для регистрации нового Inflow-аккаунта при лимите [{profile.name}]: "
    ).strip() or profile.name
    register_before_send = ask_yes_no("Сначала зарегистрировать новый аккаунт перед отправкой? [y/N]: ", default=False)
    runtime_settings = settings_store.load()

    print("\n🚀 Запускаю автоматизацию...")
    print("   • Подготовка профиля")
    if register_before_send:
        print("   • Сначала регистрация нового аккаунта")
    print("   • Далее отправка Purchase Order")
    try:
        stats = run_job(
            profile,
            registration_name=registration_name,
            profile_store=store,
            runtime_settings=runtime_settings,
            register_before_send=register_before_send,
        )
        lines = [
            "🎉 Готово",
            f"Всего email во входном файле: {stats.total_input_emails}",
            f"Уже отправлено ранее (из файла): {stats.already_sent_emails}",
            f"Email к обработке сейчас: {stats.processable_emails}",
            f"✅ Успешных отправок email: {stats.sent_emails}",
            f"❌ Ошибочных отправок email: {stats.failed_emails}",
            f"⏭️ Пропущено email (остаток < 3): {stats.skipped_emails}",
        ]
        if stats.last_error:
            lines.append(f"⚠️ Последняя ошибка: {stats.last_error}")
        _print_box(lines, title="ОТЧЕТ ЗАПУСКА")
    except (AdsApiError, InflowAutomationError) as exc:
        lines = [f"Ошибка запуска: {_format_exc(exc)}"]
        recent = _read_last_log_lines()
        if recent:
            lines.append("")
            lines.append("Последние действия:")
            lines.extend(recent[-5:])
        _print_box(lines, title="ОШИБКА")


def register_flow(store: ProfileStore, settings_store: RuntimeSettingsStore) -> None:
    profiles = list(store.list_profiles())
    print_profiles(profiles)
    if not profiles:
        print("⚠️ Сначала добавьте профиль.")
        return

    selected = ask_int("Введите локальный ID профиля для регистрации нового аккаунта: ")
    if selected is None:
        return
    profile = store.get_profile(selected)
    if profile is None:
        print("⚠️ Профиль не найден.")
        return

    registration_name = input(f"Имя для регистрации [{profile.name}]: ").strip() or profile.name
    runtime_settings = settings_store.load()

    print("\n🆕 Запускаю регистрацию нового аккаунта...")
    print("   • Покупка почты через AnyMessage")
    print("   • Регистрация в Inflow")
    print("   • Подтверждение почты и создание нового Purchase Order")
    try:
        result = register_new_account_for_profile(
            profile=profile,
            registration_name=registration_name,
            profile_store=store,
            runtime_settings=runtime_settings,
        )
        updated = store.get_profile(selected)
        lines = [
            "Регистрация завершена.",
            f"Новый ADS profile id: {result.new_profile_id}",
            f"Новый Purchase Order URL: {result.new_start_url}",
        ]
        if updated is not None:
            lines.append(f"Профиль [{updated.local_id}] обновлен в базе.")
        _print_box(lines, title="РЕГИСТРАЦИЯ ГОТОВА")
    except (AdsApiError, InflowAutomationError) as exc:
        lines = [f"Ошибка регистрации: {_format_exc(exc)}"]
        recent = _read_last_log_lines()
        if recent:
            lines.append("")
            lines.append("Последние действия:")
            lines.extend(recent[-5:])
        _print_box(lines, title="ОШИБКА")


def inbox_check_flow(store: ProfileStore) -> None:
    profiles = list(store.list_profiles())
    print_profiles(profiles)
    if not profiles:
        print("⚠️ Сначала добавьте профиль.")
        return

    selected = ask_int("Введите локальный ID профиля для запуска проверки: ")
    if selected is None:
        return
    profile = store.get_profile(selected)
    if profile is None:
        print("⚠️ Профиль не найден.")
        return

    email = input("Введите email для проверки инбокса: ").strip()
    if not email:
        print("⚠️ Email пустой.")
        return

    print("\n📨 Отправляю тестовое письмо...")
    try:
        run_inbox_check(profile, email)
        _print_box(
            [
                f"Тестовое письмо отправлено на: {email}",
                "Этот email НЕ добавляется в базу отправленных, можно отправлять повторно.",
            ],
            title="ПРОВЕРКА ИНБОКСА",
        )
    except (AdsApiError, InflowAutomationError) as exc:
        _print_box([f"Ошибка проверки инбокса: {_format_exc(exc)}"], title="ОШИБКА")


def show_menu() -> None:
    store = ProfileStore()
    settings_store = RuntimeSettingsStore()
    ensure_input_files()

    while True:
        clear_console()
        print_header()
        print("\n📋 МЕНЮ:")
        print("1. Добавить профиль ADS Browser")
        print("2. Показать профили")
        print("3. Удалить профиль")
        print("4. Запустить отправку Purchase Order")
        print("5. Проверка инбокса (одноразовая отправка)")
        print("6. Регистрация нового аккаунта (без отправки)")
        print("7. Показать API/PROXY настройки")
        print("8. Выход")
        print("9. Настройка API (PROXY + ANYMESSAGE)")
        choice = input("\n👉 Выберите действие: ").strip()

        if choice == "1":
            clear_console()
            add_profile_flow(store)
        elif choice == "2":
            clear_console()
            print_profiles(store.list_profiles())
        elif choice == "3":
            clear_console()
            delete_profile_flow(store)
        elif choice == "4":
            clear_console()
            run_flow(store, settings_store)
        elif choice == "5":
            clear_console()
            inbox_check_flow(store)
        elif choice == "6":
            clear_console()
            register_flow(store, settings_store)
        elif choice == "7":
            clear_console()
            show_api_settings(settings_store)
        elif choice == "8":
            print("👋 Выход.")
            break
        elif choice == "9":
            clear_console()
            setup_api_flow(settings_store)
        else:
            print("⚠️ Неизвестный пункт меню.")

        input("\nНажмите Enter, чтобы вернуться в меню...")


if __name__ == "__main__":
    show_menu()
