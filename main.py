from __future__ import annotations

import os
import sqlite3
from typing import Iterable

from ads_api import AdsApiError
from config import DEFAULT_INFLOW_URL
from inflow_automation import (
    InflowAutomationError,
    ensure_input_files,
    get_dashboard_stats,
    run_inbox_check,
    run_job,
)
from profile_store import Profile, ProfileStore


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


def run_flow(store: ProfileStore) -> None:
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

    print("\n🚀 Запускаю автоматизацию...")
    try:
        stats = run_job(profile, registration_name=registration_name, profile_store=store)
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
        _print_box([f"Ошибка запуска: {exc}"], title="ОШИБКА")


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
        _print_box([f"Ошибка проверки инбокса: {exc}"], title="ОШИБКА")


def show_menu() -> None:
    store = ProfileStore()
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
        print("6. Выход")
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
            run_flow(store)
        elif choice == "5":
            clear_console()
            inbox_check_flow(store)
        elif choice == "6":
            print("👋 Выход.")
            break
        else:
            print("⚠️ Неизвестный пункт меню.")

        input("\nНажмите Enter, чтобы вернуться в меню...")


if __name__ == "__main__":
    show_menu()
