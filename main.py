from __future__ import annotations

import sqlite3
from typing import Iterable

from ads_api import AdsApiError
from calendly_automation import (
    CalendlyAutomationError,
    NoAvailableSlotError,
    ensure_input_files,
    register_calendly_account,
    run_booking_sender,
)
from config import CALENDLY_MEETING_TYPES_URL
from profile_store import Profile, ProfileStore


def print_header() -> None:
    print("\n" + "=" * 60)
    print("              ADS + Calendly Automation CLI")
    print("=" * 60)


def print_profiles(profiles: Iterable[Profile]) -> None:
    profiles = list(profiles)
    if not profiles:
        print("Профилей пока нет.")
        return
    print("\nСохраненные ADS профили:")
    for profile in profiles:
        print(
            f"  [{profile.local_id}] {profile.name} | ADS ID: {profile.ads_profile_id} | "
            f"Email: {profile.login_email or '-'} | booking: {profile.booking_url or '-'} | status: {profile.status}"
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
    print("\nРучное добавление ADS профиля")
    name = input("Название профиля (любое): ").strip()
    ads_profile_id = input("ADS profile id: ").strip()
    login_email = input("Логин email (если есть): ").strip()
    password = input("Пароль (если есть): ").strip()
    main_page_url = input(f"Main page URL [{CALENDLY_MEETING_TYPES_URL}]: ").strip() or CALENDLY_MEETING_TYPES_URL
    booking_url = input("Booking page URL (copy link): ").strip()

    if not name or not ads_profile_id:
        print("Название и ADS profile id обязательны.")
        return
    try:
        store.add_profile(
            name=name,
            ads_profile_id=ads_profile_id,
            login_email=login_email,
            password=password,
            main_page_url=main_page_url,
            booking_url=booking_url,
            status="manual",
        )
        print("Профиль добавлен.")
    except sqlite3.IntegrityError:
        print("Такой ADS profile id уже существует в базе.")


def register_account_flow(store: ProfileStore) -> None:
    print("\nРегистрация нового аккаунта Calendly")
    account_name = input("Введите Full name (например Depop Support): ").strip()
    ads_profile_id = input("ADS profile id для регистрации: ").strip()
    if not account_name or not ads_profile_id:
        print("Имя аккаунта и ADS profile id обязательны.")
        return

    print("\nЗапускаю регистрацию через ADS + AnyMessage API ...")
    result = register_calendly_account(account_name=account_name, ads_profile_id=ads_profile_id)
    try:
        store.add_profile(
            name=result.account_name,
            ads_profile_id=ads_profile_id,
            login_email=result.login_email,
            password=result.password,
            main_page_url=result.main_page_url,
            cookie_file=result.cookie_file,
            anymessage_activation_id=result.activation_id,
            status="registered",
        )
    except sqlite3.IntegrityError:
        existing = [profile for profile in store.list_profiles() if profile.ads_profile_id == ads_profile_id]
        if existing:
            store.update_profile(
                existing[0].local_id,
                name=result.account_name,
                login_email=result.login_email,
                password=result.password,
                main_page_url=result.main_page_url,
                cookie_file=result.cookie_file,
                anymessage_activation_id=result.activation_id,
                status="registered",
            )

    print("Готово. Аккаунт зарегистрирован и сохранен:")
    print(f"  Email: {result.login_email}")
    print(f"  Password: {result.password}")
    print(f"  Cookie file: {result.cookie_file}")


def select_profile_flow(store: ProfileStore) -> Profile | None:
    profiles = list(store.list_profiles())
    print_profiles(profiles)
    if not profiles:
        return None
    selected = ask_int("Введите локальный ID профиля: ")
    if selected is None:
        return None
    profile = store.get_profile(selected)
    if profile is None:
        print("Профиль не найден.")
        return None
    return profile


def update_links_flow(store: ProfileStore) -> None:
    print("\nОбновить ссылки аккаунта")
    profile = select_profile_flow(store)
    if profile is None:
        return

    main_page_url = input(
        f"Ссылка на главную страницу [{profile.main_page_url or CALENDLY_MEETING_TYPES_URL}]: "
    ).strip() or profile.main_page_url or CALENDLY_MEETING_TYPES_URL
    booking_url = input(f"Ссылка на страницу отправки (copy link) [{profile.booking_url or '-'}]: ").strip()
    if not booking_url:
        booking_url = profile.booking_url

    ads_profile_id = input(f"ADS profile id [{profile.ads_profile_id}]: ").strip() or profile.ads_profile_id
    updated = store.update_profile(
        profile.local_id,
        main_page_url=main_page_url,
        booking_url=booking_url,
        ads_profile_id=ads_profile_id,
        status="ready",
    )
    print("Профиль обновлен." if updated else "Не удалось обновить профиль.")


def setup_account_flow(store: ProfileStore) -> None:
    print("\nНастройка аккаунта временно недоступна.")
    print("Функция будет включена позже.")


def run_sender_flow(store: ProfileStore) -> None:
    print("\nНачало отправки бронирований")
    profile = select_profile_flow(store)
    if profile is None:
        return

    booking_url = input(
        f"Вставьте booking ссылку (пример: https://calendly.com/username/30min) [{profile.booking_url or '-'}]: "
    ).strip() or profile.booking_url

    stats = run_booking_sender(
        profile,
        booking_url=booking_url,
    )
    print("\nОтправка завершена:")
    print(f"  Запланировано событий: {stats.scheduled_events}")
    print(f"  Израсходовано email: {stats.consumed_emails}")
    print(f"  Осталось email: {stats.remaining_emails}")


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


def show_menu() -> None:
    store = ProfileStore()
    ensure_input_files()

    while True:
        print_header()
        print("1. Регистрация аккаунта Calendly")
        print("2. Добавить профиль ADS Browser вручную")
        print("3. Добавить/обновить ссылки профиля")
        print("4. Настройка аккаунта (временно недоступно)")
        print("5. Начало отправки (Schedule Event loop)")
        print("6. Показать профили")
        print("7. Удалить профиль")
        print("8. Выход")
        choice = input("\nВыберите действие: ").strip()

        if choice == "1":
            register_account_flow(store)
        elif choice == "2":
            add_profile_flow(store)
        elif choice == "3":
            update_links_flow(store)
        elif choice == "4":
            setup_account_flow(store)
        elif choice == "5":
            run_sender_flow(store)
        elif choice == "6":
            print_profiles(store.list_profiles())
        elif choice == "7":
            delete_profile_flow(store)
        elif choice == "8":
            print("Выход.")
            break
        else:
            print("Неизвестный пункт меню.")

        input("\nНажмите Enter, чтобы продолжить...")


if __name__ == "__main__":
    try:
        show_menu()
    except (AdsApiError, CalendlyAutomationError, NoAvailableSlotError) as exc:
        print(f"\nКритическая ошибка: {exc}")
