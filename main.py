from __future__ import annotations

import sqlite3
from typing import Iterable

from ads_api import AdsApiError
from config import DEFAULT_INFLOW_URL
from inflow_automation import InflowAutomationError, ensure_input_files, run_job
from profile_store import Profile, ProfileStore


def print_header() -> None:
    print("\n" + "=" * 60)
    print("              ADS + INFLOW PURCHASE ORDER SENDER")
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
    start_url = input(f"URL старта [{DEFAULT_INFLOW_URL}]: ").strip() or DEFAULT_INFLOW_URL

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
        print(f"  Всего отправок (по 3 email): {stats.total_orders}")
        print(f"  Успешных отправок: {stats.sent_orders}")
        print(f"  Ошибочных отправок: {stats.failed_orders}")
        print(f"  Пропущено email (остаток < 3): {stats.skipped_emails}")
    except (AdsApiError, InflowAutomationError) as exc:
        print(f"Ошибка запуска: {exc}")


def show_menu() -> None:
    store = ProfileStore()
    ensure_input_files()

    while True:
        print_header()
        print("1. Добавить профиль ADS Browser")
        print("2. Показать профили")
        print("3. Удалить профиль")
        print("4. Запустить отправку Purchase Order через Email")
        print("5. Выход")
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
            print("Выход.")
            break
        else:
            print("Неизвестный пункт меню.")

        input("\nНажмите Enter, чтобы продолжить...")


if __name__ == "__main__":
    show_menu()
