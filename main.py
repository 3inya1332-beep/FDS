from __future__ import annotations

import sqlite3
from typing import Iterable

from ads_api import AdsApiError
from coda_automation import (
    CodaAutomationError,
    ensure_input_files,
    get_pending_emails,
    get_sent_history,
    run_job,
)
from config import DEFAULT_START_URL
from profile_store import Profile, ProfileStore


def print_header() -> None:
    print("\n" + "=" * 60)
    print("                PCLOUD EVE SENDER")
    print("             ADS Browser + Coda.io")
    print("=" * 60)


def print_profiles(profiles: Iterable[Profile]) -> None:
    profiles = list(profiles)
    if not profiles:
        print("Профилей пока нет.")
        return
    print("\nСохраненные ADS профили:")
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
    print("\nДобавление ADS профиля")
    name = input("Название профиля (любое): ").strip()
    ads_profile_id = input("ADS profile id: ").strip()
    start_url = input(f"URL старта [{DEFAULT_START_URL}]: ").strip() or DEFAULT_START_URL

    if not name or not ads_profile_id:
        print("Название и ADS profile id обязательны.")
        return
    try:
        store.add_profile(name=name, ads_profile_id=ads_profile_id, start_url=start_url)
        print("Профиль добавлен.")
    except sqlite3.IntegrityError:
        print("Такой ADS profile id уже существует в базе.")


def delete_profile_flow(store: ProfileStore) -> None:
    profiles = list(store.list_profiles())
    print_profiles(profiles)
    if not profiles:
        return
    selected = ask_int("Введите локальный ID профиля для удаления: ")
    if selected is None:
        return
    if store.delete_profile(selected):
        print("Профиль удален.")
    else:
        print("Профиль не найден.")


def run_flow(store: ProfileStore) -> None:
    profiles = list(store.list_profiles())
    print_profiles(profiles)
    if not profiles:
        print("Сначала добавьте профиль.")
        return

    selected = ask_int("Введите локальный ID профиля для запуска: ")
    if selected is None:
        return

    profile = store.get_profile(selected)
    if profile is None:
        print("Профиль не найден.")
        return

    print("\nЗапускаю автоматизацию...")
    try:
        stats = run_job(profile)
        print("\nГотово:")
        print(f"  Всего email: {stats.total_emails}")
        print(f"  Отправлено: {stats.sent_emails}")
        print(f"  Ошибочных батчей: {stats.failed_batches}")
        print(f"  Осталось в файле: {stats.remaining_emails}")
    except (AdsApiError, CodaAutomationError) as exc:
        print(f"Ошибка запуска: {exc}")


def show_pending_emails_flow() -> None:
    pending = get_pending_emails()
    print("\nНеотправленные email:")
    if not pending:
        print("  Список пуст.")
        return
    print(f"  Всего: {len(pending)}")
    for email in pending[:50]:
        print(f"  - {email}")
    if len(pending) > 50:
        print(f"  ... и еще {len(pending) - 50}")


def show_sent_history_flow() -> None:
    history = get_sent_history(limit=50)
    print("\nОтправленные email (история):")
    if not history:
        print("  История пуста.")
        return
    for line in history:
        print(f"  {line}")


def show_menu() -> None:
    store = ProfileStore()
    ensure_input_files()

    while True:
        print_header()
        print("1. Добавить профиль ADS Browser")
        print("2. Показать профили")
        print("3. Удалить профиль")
        print("4. Отправка email + сообщения (Coda Share)")
        print("5. Показать неотправленные email")
        print("6. Показать отправленные email (история)")
        print("7. Выход")
        choice = input("\nВыберите действие: ").strip()

        if choice == "1":
            add_profile_flow(store)
        elif choice == "2":
            print_profiles(store.list_profiles())
        elif choice == "3":
            delete_profile_flow(store)
        elif choice == "4":
            run_flow(store)
        elif choice == "5":
            show_pending_emails_flow()
        elif choice == "6":
            show_sent_history_flow()
        elif choice == "7":
            print("Выход.")
            break
        else:
            print("Неизвестный пункт меню.")

        input("\nНажмите Enter, чтобы продолжить...")


if __name__ == "__main__":
    show_menu()
