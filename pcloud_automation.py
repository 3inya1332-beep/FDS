from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

# Hide noisy node deprecation warnings from Playwright driver.
os.environ.setdefault("NODE_NO_WARNINGS", "1")

try:
    from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright
except ModuleNotFoundError:  # pragma: no cover - handled in runtime check
    Locator = Any  # type: ignore[assignment]
    Page = Any  # type: ignore[assignment]
    PlaywrightTimeoutError = TimeoutError
    sync_playwright = None

from ads_api import AdsApiClient, AdsApiError
from config import (
    ADS_HEADLESS,
    ADS_OPEN_TABS,
    BATCH_DELAY_SECONDS,
    BATCH_SIZE,
    DEFAULT_PCLOUD_URL,
    EDIT_DIR_CANDIDATES,
    EMAIL_DIR_CANDIDATES,
    EMAIL_FILENAME,
    LOGS_DIR,
    NAME_FILENAME,
    TEXT_FILENAME,
)
from profile_store import Profile


class PcloudAutomationError(RuntimeError):
    pass


@dataclass
class RunStats:
    total_emails: int
    sent_emails: int
    failed_batches: int


@dataclass
class SetupStats:
    folder_name: str
    folder_created: bool


T = TypeVar("T")
UI_SHORT_TIMEOUT = 8_000
UI_MEDIUM_TIMEOUT = 12_000
UI_STEP_DELAY_MS = 900


def _resolve_or_create_dir(candidates: list[Path]) -> Path:
    for folder in candidates:
        if folder.exists():
            return folder
    candidates[0].mkdir(parents=True, exist_ok=True)
    return candidates[0]


def ensure_input_files() -> None:
    edit_dir = _resolve_or_create_dir(EDIT_DIR_CANDIDATES)
    email_dir = _resolve_or_create_dir(EMAIL_DIR_CANDIDATES)

    name_file = edit_dir / NAME_FILENAME
    if not name_file.exists():
        name_file.write_text("my_new_folder", encoding="utf-8")

    text_file = edit_dir / TEXT_FILENAME
    if not text_file.exists():
        text_file.write_text("Hello! Please join this folder.", encoding="utf-8")

    email_file = email_dir / EMAIL_FILENAME
    if not email_file.exists():
        email_file.write_text("example1@mail.com\nexample2@mail.com\n", encoding="utf-8")

    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _read_non_empty_lines(file_path: Path) -> list[str]:
    if not file_path.exists():
        raise PcloudAutomationError(f"Файл не найден: {file_path}")
    lines = [line.strip() for line in file_path.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line]


def _find_email_file(email_dir: Path) -> Path:
    default_file = email_dir / EMAIL_FILENAME
    if default_file.exists():
        return default_file
    txt_files = sorted(email_dir.glob("*.txt"))
    if txt_files:
        return txt_files[0]
    raise PcloudAutomationError(
        f"В папке {email_dir} нет txt-файла с email-адресами. Добавьте хотя бы один файл."
    )


def load_job_data() -> tuple[str, str, list[str]]:
    edit_dir = _resolve_or_create_dir(EDIT_DIR_CANDIDATES)
    email_dir = _resolve_or_create_dir(EMAIL_DIR_CANDIDATES)

    folder_name = (edit_dir / NAME_FILENAME).read_text(encoding="utf-8").strip()
    if not folder_name:
        raise PcloudAutomationError(f"Файл {NAME_FILENAME} пуст.")

    message_text = (edit_dir / TEXT_FILENAME).read_text(encoding="utf-8").strip()
    email_file = _find_email_file(email_dir)
    raw_emails = _read_non_empty_lines(email_file)

    # Keep order but remove accidental duplicates.
    unique_emails: list[str] = []
    seen: set[str] = set()
    for email in raw_emails:
        lowered = email.lower()
        if lowered not in seen:
            unique_emails.append(email)
            seen.add(lowered)

    if not unique_emails:
        raise PcloudAutomationError("Список email пуст.")

    return folder_name, message_text, unique_emails


def _chunked(items: Iterable[str], size: int) -> list[list[str]]:
    batch: list[str] = []
    chunks: list[list[str]] = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            chunks.append(batch)
            batch = []
    if batch:
        chunks.append(batch)
    return chunks


def _say(message: str) -> None:
    print(f"• {message}")
    _write_log(message)


class PcloudUi:
    def __init__(self, page: Page) -> None:
        self.page = page

    def force_desktop_view(self) -> None:
        # Some ADS profiles open with small/mobile viewport.
        try:
            self.page.set_viewport_size({"width": 1920, "height": 1080})
        except Exception:  # noqa: BLE001
            pass
        try:
            cdp = self.page.context.new_cdp_session(self.page)
            window_data = cdp.send("Browser.getWindowForTarget")
            window_id = window_data.get("windowId")
            if window_id:
                cdp.send(
                    "Browser.setWindowBounds",
                    {
                        "windowId": window_id,
                        "bounds": {"windowState": "maximized"},
                    },
                )
        except Exception:  # noqa: BLE001
            pass

    def open_home(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        # pCloud keeps background requests, so networkidle can hang forever.
        self.page.wait_for_load_state("load", timeout=UI_MEDIUM_TIMEOUT)
        self.page.wait_for_selector("body", timeout=UI_SHORT_TIMEOUT)
        self.page.wait_for_timeout(UI_STEP_DELAY_MS)

    def _is_login_page(self) -> bool:
        has_password = self.page.locator("input[type='password']").count() > 0
        has_email = (
            self.page.locator("input[type='email']").count() > 0
            or self.page.locator("input[name*='mail' i]").count() > 0
        )
        return has_password and has_email

    def _visible_dialog(self) -> Locator:
        selectors = [
            'div[role="dialog"]:visible',
            '.modal:visible',
            '.visitor:visible',
            "[class*='dialog']:visible",
            "[class*='modal']:visible",
            ".popup:visible",
        ]
        for selector in selectors:
            locator = self.page.locator(selector).last
            try:
                locator.wait_for(state="visible", timeout=2_500)
                return locator
            except PlaywrightTimeoutError:
                continue
        raise PcloudAutomationError(
            "Окно не открылось. Проверьте, что пункт Invite to Folder доступен."
        )

    def _ensure_invite_tab(self, dialog: Locator) -> None:
        tab_candidates = [
            dialog.get_by_text(re.compile(r"^Invite to folder$", re.I)).first,
            dialog.get_by_text(re.compile(r"^Invite$", re.I)).first,
        ]
        for tab in tab_candidates:
            try:
                if tab.count() > 0:
                    tab.click(timeout=1_200)
                    self.page.wait_for_timeout(120)
                    return
            except Exception:  # noqa: BLE001
                continue

    def _get_visible_email_input(self, dialog: Locator) -> Locator:
        candidates = [
            dialog.locator("input[name='emails']:visible").first,
            dialog.locator("input.comboinput:visible").first,
            dialog.locator("input[placeholder*='Name or Email' i]:visible").first,
            dialog.locator("input[type='text']:visible").first,
            dialog.locator("input:visible").first,
        ]
        for candidate in candidates:
            try:
                candidate.wait_for(state="visible", timeout=1_800)
                return candidate
            except PlaywrightTimeoutError:
                continue
        raise PcloudAutomationError("Не удалось найти видимое поле Name or Email в окне Invite.")

    def _folder_locator(self, folder_name: str) -> Locator:
        escaped = re.escape(folder_name.strip())
        return self.page.get_by_text(re.compile(rf"^\s*{escaped}\s*$")).first

    def _ensure_folder_visible(self, folder_name: str, timeout: int = UI_SHORT_TIMEOUT) -> Locator:
        locator = self._folder_locator(folder_name)
        locator.wait_for(state="visible", timeout=timeout)
        return locator

    def ensure_folder_exists(self, folder_name: str, timeout: int = UI_SHORT_TIMEOUT) -> None:
        try:
            self._ensure_folder_visible(folder_name, timeout=timeout)
        except PlaywrightTimeoutError as exc:
            raise PcloudAutomationError(
                f'Папка "{folder_name}" не найдена. Сначала выполните пункт меню 4 '
                "(Настройка профиля: создать папку)."
            ) from exc

    def _find_add_button(self) -> Locator:
        try:
            self.page.evaluate("window.scrollTo(0, 0)")
        except Exception:  # noqa: BLE001
            pass

        candidates = [
            self.page.get_by_role("button", name=re.compile(r"^(Add|New|Создать)$", re.I)).first,
            self.page.get_by_text(re.compile(r"^(Add|Создать)$", re.I)).first,
            self.page.locator("button:has-text('Add')").first,
            self.page.locator("a:has-text('Add')").first,
            self.page.locator("[role='button']:has-text('Add')").first,
            self.page.locator("button:has-text('Создать')").first,
            self.page.locator("button[aria-label*='add' i]").first,
            self.page.locator("[role='button'][aria-label*='add' i]").first,
            self.page.locator(".add-button, .create-button, .upload-btn").first,
        ]
        for candidate in candidates:
            try:
                candidate.wait_for(state="visible", timeout=1_200)
                return candidate
            except PlaywrightTimeoutError:
                continue

        current_url = self.page.url
        if self._is_login_page():
            raise PcloudAutomationError(
                "Не найдена кнопка Add: похоже, в профиле ADS не выполнен вход в pCloud. "
                f"Откройте профиль вручную и авторизуйтесь. URL: {current_url}"
            )
        raise PcloudAutomationError(
            "Не удалось найти кнопку Add на странице pCloud. "
            f"Проверьте, что открыта главная страница my.pcloud.com. URL: {current_url}"
        )

    def create_folder(self, folder_name: str) -> bool:
        try:
            self._ensure_folder_visible(folder_name, timeout=2_500)
            return False
        except PlaywrightTimeoutError:
            pass

        try:
            add_button = self._find_add_button()
            add_button.click()
        except Exception:  # noqa: BLE001
            # Last resort: top-right click area where Add button is in desktop pCloud UI.
            view = self.page.viewport_size or {"width": 1280, "height": 720}
            self.page.mouse.click(view["width"] - 75, 48)
            self.page.wait_for_timeout(500)

        folder_menu_candidates = [
            self.page.get_by_text(re.compile(r"^(New Folder|Новая папка)$", re.I)).first,
            self.page.get_by_text(re.compile(r"^(Folder|Папка)$", re.I)).first,
        ]
        folder_menu_item: Locator | None = None
        for candidate in folder_menu_candidates:
            try:
                candidate.wait_for(state="visible", timeout=1_500)
                folder_menu_item = candidate
                break
            except PlaywrightTimeoutError:
                continue

        if folder_menu_item is None:
            raise PcloudAutomationError(
                "После клика Add не найден пункт New Folder/Folder в выпадающем меню."
            )
        folder_menu_item.click()

        # Flow A: modal/visitor dialog with name input and create button.
        try:
            dialog = self._visible_dialog()
            name_input = dialog.locator("input[type='text'], input").first
            name_input.wait_for(state="visible", timeout=2_500)
            name_input.fill(folder_name)

            create_button = dialog.get_by_role(
                "button",
                name=re.compile(r"Create|Save|OK|Done|Готово|Создать", re.I),
            ).first
            try:
                create_button.wait_for(state="visible", timeout=1_500)
                create_button.click()
            except PlaywrightTimeoutError:
                name_input.press("Enter")
        except Exception:  # noqa: BLE001
            # Flow B: inline create input without modal.
            inline_candidates = [
                self.page.locator("input:visible[placeholder*='folder' i]").first,
                self.page.locator("input:visible[placeholder*='папк' i]").first,
                self.page.locator("input:visible[value*='New Folder' i]").first,
                self.page.locator("input:visible[value*='Новая папка' i]").first,
            ]
            inline_input: Locator | None = None
            for candidate in inline_candidates:
                try:
                    candidate.wait_for(state="visible", timeout=1_200)
                    inline_input = candidate
                    break
                except PlaywrightTimeoutError:
                    continue
            if inline_input is not None:
                inline_input.fill(folder_name)
                inline_input.press("Enter")
            else:
                raise PcloudAutomationError("Не получилось открыть форму создания папки.")

        self._ensure_folder_visible(folder_name, timeout=UI_MEDIUM_TIMEOUT)
        return True

    def _open_folder_context_menu(self, folder_name: str) -> Locator:
        folder_label = self._ensure_folder_visible(folder_name, timeout=UI_MEDIUM_TIMEOUT)
        folder_label.scroll_into_view_if_needed()
        folder_label.click(button="right")
        self.page.wait_for_timeout(350)
        return folder_label

    def _open_invite_dialog(self, folder_name: str) -> None:
        self._open_folder_context_menu(folder_name)
        invite_candidates = [
            self.page.get_by_text(re.compile(r"^Invite to Folder$", re.I)).first,
            self.page.get_by_text(re.compile(r"^Invite to folder$", re.I)).first,
            self.page.get_by_role("menuitem", name=re.compile(r"Invite to Folder", re.I)).first,
            self.page.get_by_text(re.compile(r"^Invite", re.I)).first,
        ]
        for candidate in invite_candidates:
            try:
                candidate.wait_for(state="visible", timeout=1_500)
                candidate.click()
                self.page.wait_for_timeout(300)
                self._visible_dialog()
                return
            except Exception:  # noqa: BLE001
                continue

        raise PcloudAutomationError("Не удалось нажать пункт 'Invite to Folder' в меню папки.")

    def invite_batch(self, folder_name: str, emails: list[str], message_text: str) -> None:
        self._open_invite_dialog(folder_name)
        dialog = self._visible_dialog()
        self._ensure_invite_tab(dialog)

        for email in emails:
            sent = False
            for _ in range(3):
                try:
                    email_input = self._get_visible_email_input(dialog)
                    email_input.click(timeout=1_200)
                    email_input.fill(email, timeout=1_200)
                    email_input.press("Enter")
                    self.page.wait_for_timeout(120)
                    sent = True
                    break
                except Exception:  # noqa: BLE001
                    self.page.wait_for_timeout(220)
            if not sent:
                raise PcloudAutomationError(f"Не удалось добавить email: {email}")

        if message_text:
            message_candidates = [
                dialog.locator("textarea:visible").first,
                dialog.locator("textarea").first,
            ]
            for message_area in message_candidates:
                try:
                    if message_area.count() > 0:
                        message_area.fill(message_text, timeout=1_500)
                        break
                except Exception:  # noqa: BLE001
                    continue

        share_candidates = [
            dialog.get_by_role("button", name=re.compile(r"^Share$", re.I)).first,
            dialog.get_by_role("button", name=re.compile(r"^(Send|Отправить)$", re.I)).first,
            dialog.get_by_text(re.compile(r"^Share$", re.I)).first,
        ]
        share_clicked = False
        for button in share_candidates:
            try:
                button.wait_for(state="visible", timeout=1_200)
                button.click()
                share_clicked = True
                break
            except Exception:  # noqa: BLE001
                continue
        if not share_clicked:
            raise PcloudAutomationError("Не найдена кнопка Share в окне приглашения.")

        try:
            dialog.wait_for(state="hidden", timeout=UI_SHORT_TIMEOUT)
        except PlaywrightTimeoutError:
            # Some pCloud states keep the dialog open with a success toast.
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(250)


def _run_in_ads_browser(profile: Profile, worker: Callable[[PcloudUi], T]) -> T:
    if sync_playwright is None:
        raise PcloudAutomationError(
            "Модуль playwright не установлен. Выполните: pip install -r requirements.txt"
        )

    ads_client = AdsApiClient()
    ads_session = None
    page: Page | None = None

    try:
        _say("Запускаю профиль ADS Browser...")
        ads_session = ads_client.start_browser(
            profile_id=profile.ads_profile_id,
            headless=ADS_HEADLESS,
            open_tabs=ADS_OPEN_TABS,
        )

        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(ads_session.cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(UI_SHORT_TIMEOUT)

            ui = PcloudUi(page)
            ui.force_desktop_view()
            _say("Открываю pCloud...")
            ui.open_home(profile.start_url or DEFAULT_PCLOUD_URL)
            ui.force_desktop_view()
            result = worker(ui)
            browser.close()
            return result
    except AdsApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        if page is not None:
            try:
                LOGS_DIR.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(LOGS_DIR / "last_error.png"), full_page=True)
                _write_log(f"Saved screenshot to {LOGS_DIR / 'last_error.png'}")
            except Exception:  # noqa: BLE001
                pass
        _write_log(f"TECH ERROR: {exc}")
        raise PcloudAutomationError(
            "Не получилось нажать нужные кнопки на странице. "
            "Проверьте, что открыт обычный интерфейс My pCloud."
        ) from exc
    finally:
        if ads_session is not None:
            ads_client.stop_browser(ads_session.profile_id)


def setup_folder(profile: Profile) -> SetupStats:
    ensure_input_files()
    folder_name, _, _ = load_job_data()
    _say(f'Настройка: папка "{folder_name}"')

    def worker(ui: PcloudUi) -> SetupStats:
        _say("Пробую создать папку...")
        created = ui.create_folder(folder_name)
        return SetupStats(folder_name=folder_name, folder_created=created)

    stats = _run_in_ads_browser(profile, worker)
    if stats.folder_created:
        _say(f'Готово: папка "{stats.folder_name}" создана.')
    else:
        _say(f'Папка "{stats.folder_name}" уже существовала.')
    return stats


def send_invites(profile: Profile) -> RunStats:
    ensure_input_files()
    folder_name, message_text, emails = load_job_data()
    sent_emails = 0
    failed_batches = 0
    batches = _chunked(emails, BATCH_SIZE)
    _say(f'Отправка: папка "{folder_name}", всего email: {len(emails)}')

    def worker(ui: PcloudUi) -> RunStats:
        nonlocal sent_emails, failed_batches
        ui.ensure_folder_exists(folder_name, timeout=UI_MEDIUM_TIMEOUT)
        for index, batch in enumerate(batches, start=1):
            try:
                _say(f"Пакет {index}/{len(batches)}: отправляю {len(batch)} email...")
                ui.invite_batch(folder_name, batch, message_text)
                sent_emails += len(batch)
                _say(f"Пакет {index}: успешно.")
            except Exception as batch_exc:  # noqa: BLE001
                failed_batches += 1
                _write_log(f"Batch failed ({len(batch)} emails): {batch_exc}")
                _say(f"Пакет {index}: не удалось отправить, перехожу к следующему.")
            time.sleep(BATCH_DELAY_SECONDS)

        return RunStats(total_emails=len(emails), sent_emails=sent_emails, failed_batches=failed_batches)

    stats = _run_in_ads_browser(profile, worker)
    _say(
        f"Отправка завершена: {stats.sent_emails}/{stats.total_emails}, "
        f"ошибочных пакетов: {stats.failed_batches}"
    )
    return stats


def run_job(profile: Profile) -> RunStats:
    setup_folder(profile)
    return send_invites(profile)


def _write_log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_file = LOGS_DIR / "automation.log"
    with log_file.open("a", encoding="utf-8") as file:
        file.write(f"[{timestamp}] {message}\n")
