from __future__ import annotations

import os
import sqlite3
import sys
import time
from typing import Iterable

from ads_api import AdsApiError
from config import DEFAULT_PCLOUD_URL
from pcloud_automation import (
    InboxCheckStats,
    PcloudAutomationError,
    ensure_input_files,
    get_sent_emails,
    get_unsent_emails,
    inbox_check,
    refresh_email_data,
    send_invites,
    setup_folder,
)
from profile_store import Profile, ProfileStore


def clear_console() -> None:
    if os.name == "nt":
        os.system("cls")
    else:
        print("\033[2J\033[H", end="")


def print_header() -> None:
    print("\n" + "═" * 66)
    print("🌩️  ADS + pCloud AUTO INVITER")
    print("✨ Быстрое создание папок и отправка приглашений")
    print("═" * 66)


def print_profiles(profiles: Iterable[Profile]) -> None:
    profiles = list(profiles)
    if not profiles:
        print("📭 Профилей пока нет.")
        return
    print("\n📁 Сохраненные ADS профили:")
    for profile in profiles:
        print(
            f"  [{profile.local_id}] {profile.name} | ADS ID: {profile.ads_profile_id} | URL: {profile.start_url}"
        )


def ask_int(prompt: str) -> int | None:
    raw = input(prompt).strip()
    if not raw:
        return None
    if not raw.isdigit():
        print("⚠️ Нужно ввести число.")
        return None
    return int(raw)


def ask_email(prompt: str) -> str | None:
    value = input(prompt).strip().lower()
    if not value:
        return None
    if "@" not in value or "." not in value.split("@")[-1]:
        print("⚠️ Это не похоже на email.")
        return None
    return value


def print_email_list(title: str, emails: list[str], preview_limit: int = 25) -> None:
    print(f"\n{title}")
    print(f"Количество: {len(emails)}")
    if not emails:
        print("  (пусто)")
        return
    for email in emails[:preview_limit]:
        print(f"  • {email}")
    if len(emails) > preview_limit:
        print(f"  ... и еще {len(emails) - preview_limit}")


def restart_script() -> None:
    print("\n🔄 Обновляю данные и перезапускаю скрипт...")
    time.sleep(0.7)
    os.execv(sys.executable, [sys.executable, *sys.argv])


def add_profile_flow(store: ProfileStore) -> None:
    print("\n➕ Добавление ADS профиля")
    name = input("Название профиля (любое): ").strip()
    ads_profile_id = input("ADS profile id: ").strip()
    start_url = input(f"URL старта [{DEFAULT_PCLOUD_URL}]: ").strip() or DEFAULT_PCLOUD_URL

    if not name or not ads_profile_id:
        print("⚠️ Название и ADS profile id обязательны.")
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


def choose_profile(store: ProfileStore) -> Profile | None:
    profiles = list(store.list_profiles())
    print_profiles(profiles)
    if not profiles:
        print("ℹ️ Сначала добавьте профиль.")
        return None

    selected = ask_int("Введите локальный ID профиля для запуска: ")
    if selected is None:
        return None

    profile = store.get_profile(selected)
    if profile is None:
        print("⚠️ Профиль не найден.")
        return None
    return profile


def setup_flow(store: ProfileStore) -> None:
    profile = choose_profile(store)
    if profile is None:
        return

    print("\n🚀 Запускаю настройку профиля (создание папки)...")
    try:
        stats = setup_folder(profile)
        print("\n✅ Готово:")
        print(f"  📂 Папка: {stats.folder_name}")
        print(f"  🧩 Создана сейчас: {'Да' if stats.folder_created else 'Нет (уже существовала)'}")
    except (AdsApiError, PcloudAutomationError) as exc:
        print(f"\n❌ Не получилось выполнить настройку: {exc}")


def send_flow(store: ProfileStore) -> None:
    profile = choose_profile(store)
    if profile is None:
        return

    print("\n🚀 Запускаю отправку email-приглашений...")
    try:
        stats = send_invites(profile)
        print("\n✅ Готово:")
        print(f"  📬 Всего email: {stats.total_emails}")
        print(f"  ✅ Отправлено: {stats.sent_emails}")
        print(f"  ⚠️ Ошибочных батчей: {stats.failed_batches}")
    except (AdsApiError, PcloudAutomationError) as exc:
        print(f"\n❌ Не получилось выполнить отправку: {exc}")


def show_unsent_flow() -> None:
    try:
        unsent = get_unsent_emails()
        print_email_list("📭 Неотправленные email", unsent)
        print("Файл со списком: email_history/unsent_emails.txt")
    except PcloudAutomationError as exc:
        print(f"\n❌ Ошибка: {exc}")


def show_sent_flow() -> None:
    sent = get_sent_emails()
    print_email_list("✅ Отправленные email (за всё время)", sent)
    print("Файл со списком: email_history/sent_emails.txt")


def refresh_data_flow() -> None:
    print("🔄 Обновляю данные из папки email...")
    try:
        sent_count, unsent_count = refresh_email_data()
        print(f"✅ Готово: отправленных = {sent_count}, неотправленных = {unsent_count}")
        restart_script()
    except PcloudAutomationError as exc:
        print(f"\n❌ Не удалось обновить данные: {exc}")


def inbox_check_flow(store: ProfileStore) -> None:
    profile = choose_profile(store)
    if profile is None:
        return

    email = ask_email("Введите email для проверки инбокса: ")
    if not email:
        return

    print("\n🧪 Запускаю проверку инбокса...")
    try:
        stats: InboxCheckStats = inbox_check(profile, email)
        print("\n✅ Проверка выполнена:")
        print(f"  📂 Создана папка: {stats.folder_name}")
        print(f"  📧 Email: {stats.email}")
        print("  📨 Инвайт отправлен")
    except (AdsApiError, PcloudAutomationError) as exc:
        print(f"\n❌ Не получилось выполнить проверку: {exc}")


def show_menu() -> None:
    store = ProfileStore()
    ensure_input_files()

    while True:
        clear_console()
        print_header()
        print("1. ➕ Добавить профиль ADS Browser")
        print("2. 👀 Показать профили")
        print("3. 🗑️ Удалить профиль")
        print("4. ⚙️ Настройка профиля (создать папку)")
        print("5. 📨 Отправка email + сообщения")
        print("6. 📭 Показать неотправленные email")
        print("7. ✅ Показать отправленные email (история)")
        print("8. 🧪 Проверка инбокса (TEST1/TEST2/...)")
        print("9. 🔄 Обновление данных (перезапуск)")
        print("10. 🚪 Выход")
        choice = input("\nВыберите действие: ").strip()

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
            setup_flow(store)
        elif choice == "5":
            clear_console()
            send_flow(store)
        elif choice == "6":
            clear_console()
            show_unsent_flow()
        elif choice == "7":
            clear_console()
            show_sent_flow()
        elif choice == "8":
            clear_console()
            inbox_check_flow(store)
        elif choice == "9":
            clear_console()
            refresh_data_flow()
        elif choice == "10":
            print("👋 Выход.")
            break
        else:
            print("⚠️ Неизвестный пункт меню.")

        input("\nНажмите Enter, чтобы продолжить...")


if __name__ == "__main__":
    show_menu()
