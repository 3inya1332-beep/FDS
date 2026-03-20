from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
import re
from typing import Iterable

from ads_api import AdsApiError
from calendly_automation import (
    CalendlyAutomationError,
    NoAvailableSlotError,
    SenderCaptchaDetected,
    ensure_input_files,
    recreate_ads_profile_after_sender_captcha,
    register_calendly_account,
    run_booking_sender,
    run_inbox_check_sender,
)
from config import CALENDLY_MEETING_TYPES_URL, SENDER_CAPTCHA_MAX_RECOVERIES, SENDER_CAPTCHA_RECOVERY_ENABLED
from profile_store import Profile, ProfileStore

REGISTERED_ACCOUNTS_DIR = Path(__file__).resolve().parent / "registered-accounts"


def clear_console() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def print_header() -> None:
    print("╔" + "═" * 70 + "╗")
    print("║ 🚀 ADS + Calendly Automation CLI".ljust(71) + "║")
    print("╠" + "═" * 70 + "╣")
    print("║ ⚡ Быстрый режим рассылки и регистрации".ljust(71) + "║")
    print("╚" + "═" * 70 + "╝")


def print_profiles(profiles: Iterable[Profile]) -> None:
    profiles = list(profiles)
    if not profiles:
        print("📭 Профилей пока нет.")
        return
    print("\n📚 Сохраненные ADS профили:")
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
        print("⚠️ Нужно ввести число.")
        return None
    return int(raw)


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", value.strip())
    return cleaned.strip("_") or "account"


def save_registered_credentials(
    *,
    account_name: str,
    ads_profile_id: str,
    email: str,
    password: str,
) -> Path:
    REGISTERED_ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"{_safe_filename(account_name)}_{stamp}.txt"
    file_path = REGISTERED_ACCOUNTS_DIR / filename
    payload = (
        f"account_name={account_name}\n"
        f"ads_profile_id={ads_profile_id}\n"
        f"email={email}\n"
        f"password={password}\n"
        f"saved_at_utc={datetime.utcnow().isoformat(timespec='seconds')}\n"
    )
    file_path.write_text(payload, encoding="utf-8")
    return file_path


def add_profile_flow(store: ProfileStore) -> None:
    print("\n➕ Ручное добавление ADS профиля")
    name = input("Название профиля (любое): ").strip()
    ads_profile_id = input("ADS profile id: ").strip()
    login_email = input("Логин email (если есть): ").strip()
    password = input("Пароль (если есть): ").strip()
    main_page_url = input(f"Main page URL [{CALENDLY_MEETING_TYPES_URL}]: ").strip() or CALENDLY_MEETING_TYPES_URL
    booking_url = input("Booking page URL (copy link): ").strip()

    if not name or not ads_profile_id:
        print("⚠️ Название и ADS profile id обязательны.")
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
        print("✅ Профиль добавлен.")
    except sqlite3.IntegrityError:
        print("⚠️ Такой ADS profile id уже существует в базе.")


def register_account_flow(store: ProfileStore) -> None:
    print("\n🧩 Регистрация аккаунта Calendly")
    account_name = input("Введите Full name (например Depop Support): ").strip()
    ads_profile_id = input("ADS profile id для регистрации: ").strip()
    if not account_name or not ads_profile_id:
        print("⚠️ Имя аккаунта и ADS profile id обязательны.")
        return

    print("\n▶️ Начинаем регистрацию через ADS + AnyMessage API...")
    result = register_calendly_account(account_name=account_name, ads_profile_id=ads_profile_id)
    credentials_path = save_registered_credentials(
        account_name=result.account_name,
        ads_profile_id=ads_profile_id,
        email=result.login_email,
        password=result.password,
    )
    booking_url = input("🔗 Ссылка отправки (booking link, можно оставить пустым): ").strip()
    try:
        store.add_profile(
            name=result.account_name,
            ads_profile_id=ads_profile_id,
            login_email=result.login_email,
            password=result.password,
            main_page_url=result.main_page_url,
            booking_url=booking_url,
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
                booking_url=booking_url or existing[0].booking_url,
                cookie_file=result.cookie_file,
                anymessage_activation_id=result.activation_id,
                status="registered",
            )

    print("✅ Аккаунт зарегистрирован и сохранен:")
    print(f"   📧 Email: {result.login_email}")
    print(f"   🔐 Password: {result.password}")
    print(f"   🍪 Cookie file: {result.cookie_file}")
    print(f"   🗂️ Credentials file: {credentials_path}")
    print("🛠️ Профиль оставлен открытым: настройте шаблоны вручную в браузере.")


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
        print("⚠️ Профиль не найден.")
        return None
    return profile


def update_links_flow(store: ProfileStore) -> None:
    print("\n🔗 Обновить ссылки аккаунта")
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
    print("✅ Профиль обновлен." if updated else "⚠️ Не удалось обновить профиль.")


def setup_account_flow(store: ProfileStore) -> None:
    print("\n⛔ Настройка аккаунта временно недоступна.")
    print("   Настраивайте шаблоны вручную.")


def run_sender_flow(store: ProfileStore) -> None:
    print("\n📨 Начинаем рассылку бронирований")
    profile = select_profile_flow(store)
    if profile is None:
        return

    booking_url = input(
        f"🔗 Вставьте booking ссылку (Enter = использовать сохраненную) [{profile.booking_url or '-'}]: "
    ).strip() or profile.booking_url
    if not booking_url:
        print("⚠️ Ссылка не указана.")
        return

    if not profile.booking_url:
        store.update_profile(profile.local_id, booking_url=booking_url, status="ready")

    recovery_count = 0
    while True:
        try:
            stats = run_booking_sender(
                profile,
                booking_url=booking_url,
            )
            break
        except SenderCaptchaDetected as exc:
            if not SENDER_CAPTCHA_RECOVERY_ENABLED:
                raise CalendlyAutomationError(f"Captcha detected and auto-recovery is disabled: {exc}") from exc
            recovery_count += 1
            if recovery_count > SENDER_CAPTCHA_MAX_RECOVERIES:
                raise CalendlyAutomationError(
                    f"Captcha detected too many times ({recovery_count}). Stop sender."
                ) from exc

            print("\n🛑 Капча обнаружена во время рассылки.")
            print("♻️ Пересоздаем ADS профиль через API и продолжаем отправку...")
            new_ads_profile_id = recreate_ads_profile_after_sender_captcha(
                old_profile_id=profile.ads_profile_id,
                profile_name=profile.name,
            )
            store.update_profile(
                profile.local_id,
                ads_profile_id=new_ads_profile_id,
                status="captcha_recovered",
            )
            refreshed = store.get_profile(profile.local_id)
            if refreshed is not None:
                profile = refreshed
            print(f"✅ Новый ADS профиль: {profile.ads_profile_id}")
            print("▶️ Возобновляем рассылку на той же ссылке...\n")

    print("\n✅ Рассылка завершена:")
    print(f"   🗓️ Запланировано событий: {stats.scheduled_events}")
    print(f"   📬 Израсходовано email: {stats.consumed_emails}")
    print(f"   📂 Осталось email: {stats.remaining_emails}")


def run_inbox_check_flow(store: ProfileStore) -> None:
    print("\n📮 Проверка инбокса")
    profile = select_profile_flow(store)
    if profile is None:
        return
    booking_url = input(
        f"🔗 Booking ссылка (Enter = сохраненная) [{profile.booking_url or '-'}]: "
    ).strip() or profile.booking_url
    if not booking_url:
        print("⚠️ Ссылка не указана.")
        return
    target_email = input("📧 Укажите email для тестовой отправки: ").strip()
    if "@" not in target_email:
        print("⚠️ Некорректный email.")
        return
    stats = run_inbox_check_sender(profile, booking_url=booking_url, target_email=target_email)
    print("\n✅ Тестовая отправка завершена:")
    print(f"   🗓️ Событий: {stats.scheduled_events}")


def delete_profile_flow(store: ProfileStore) -> None:
    profiles = list(store.list_profiles())
    print_profiles(profiles)
    if not profiles:
        return
    selected = ask_int("Введите локальный ID профиля для удаления: ")
    if selected is None:
        return
    if store.delete_profile(selected):
        print("✅ Профиль удален.")
    else:
        print("⚠️ Профиль не найден.")


def show_menu() -> None:
    store = ProfileStore()
    ensure_input_files()
    REGISTERED_ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)

    while True:
        clear_console()
        print_header()
        print("1. 🧩 Регистрация аккаунта Calendly")
        print("2. ➕ Добавить профиль ADS вручную")
        print("3. 🔗 Добавить/обновить ссылки профиля")
        print("4. ⛔ Настройка аккаунта (временно недоступно)")
        print("5. 📨 Начать рассылку (Schedule Event loop)")
        print("6. 📮 Проверка инбокса (тестовая отправка)")
        print("7. 📚 Показать профили")
        print("8. 🗑️ Удалить профиль")
        print("9. 🚪 Выход")
        choice = input("\n👉 Выберите действие: ").strip()

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
            run_inbox_check_flow(store)
        elif choice == "7":
            print_profiles(store.list_profiles())
        elif choice == "8":
            delete_profile_flow(store)
        elif choice == "9":
            print("👋 Выход.")
            break
        else:
            print("⚠️ Неизвестный пункт меню.")

        input("\n↩️ Нажмите Enter, чтобы вернуться в меню...")


if __name__ == "__main__":
    try:
        show_menu()
    except (AdsApiError, CalendlyAutomationError, NoAvailableSlotError) as exc:
        print(f"\nКритическая ошибка: {exc}")
