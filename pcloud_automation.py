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
    EMAIL_HISTORY_DIR,
    EDIT_DIR_CANDIDATES,
    EMAIL_DIR_CANDIDATES,
    EMAIL_FILENAME,
    INBOX_TEST_COUNTER_FILE,
    LOGS_DIR,
    NAME_FILENAME,
    SENT_EMAILS_FILE,
    TEXT_FILENAME,
    UNSENT_EMAILS_FILE,
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


@dataclass
class InboxCheckStats:
    folder_name: str
    email: str
    invite_sent: bool


T = TypeVar("T")
UI_SHORT_TIMEOUT = 4_200
UI_MEDIUM_TIMEOUT = 6_500
UI_STEP_DELAY_MS = 90
_ACTIVE_ADS_SESSIONS: dict[str, AdsBrowserSession] = {}


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
    EMAIL_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    if not SENT_EMAILS_FILE.exists():
        SENT_EMAILS_FILE.write_text("", encoding="utf-8")
    if not UNSENT_EMAILS_FILE.exists():
        UNSENT_EMAILS_FILE.write_text("", encoding="utf-8")
    if not INBOX_TEST_COUNTER_FILE.exists():
        INBOX_TEST_COUNTER_FILE.write_text("0", encoding="utf-8")


def _read_non_empty_lines(file_path: Path) -> list[str]:
    if not file_path.exists():
        raise PcloudAutomationError(f"Файл не найден: {file_path}")
    lines = [line.strip() for line in file_path.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line]


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _load_sent_emails_set() -> set[str]:
    ensure_input_files()
    return {_normalize_email(line) for line in _read_non_empty_lines(SENT_EMAILS_FILE)}


def get_sent_emails() -> list[str]:
    return sorted(_load_sent_emails_set())


def _append_sent_emails(emails: list[str]) -> None:
    if not emails:
        return
    existing = _load_sent_emails_set()
    new_items: list[str] = []
    for email in emails:
        normalized = _normalize_email(email)
        if normalized and normalized not in existing:
            existing.add(normalized)
            new_items.append(normalized)
    if new_items:
        with SENT_EMAILS_FILE.open("a", encoding="utf-8") as file:
            for email in new_items:
                file.write(email + "\n")


def _save_unsent_emails(emails: list[str]) -> None:
    ensure_input_files()
    with UNSENT_EMAILS_FILE.open("w", encoding="utf-8") as file:
        for email in emails:
            file.write(_normalize_email(email) + "\n")


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


def _load_all_unique_emails() -> list[str]:
    email_dir = _resolve_or_create_dir(EMAIL_DIR_CANDIDATES)
    email_file = _find_email_file(email_dir)
    raw_emails = _read_non_empty_lines(email_file)

    unique_emails: list[str] = []
    seen: set[str] = set()
    for email in raw_emails:
        lowered = _normalize_email(email)
        if lowered and lowered not in seen:
            unique_emails.append(lowered)
            seen.add(lowered)
    return unique_emails


def load_message_text() -> str:
    edit_dir = _resolve_or_create_dir(EDIT_DIR_CANDIDATES)
    return (edit_dir / TEXT_FILENAME).read_text(encoding="utf-8").strip()


def get_unsent_emails() -> list[str]:
    all_emails = _load_all_unique_emails()
    sent = _load_sent_emails_set()
    unsent = [email for email in all_emails if _normalize_email(email) not in sent]
    _save_unsent_emails(unsent)
    return unsent


def refresh_email_data() -> tuple[int, int]:
    ensure_input_files()
    unsent = get_unsent_emails()
    sent = get_sent_emails()
    return len(sent), len(unsent)


def load_job_data(exclude_sent: bool = False) -> tuple[str, str, list[str]]:
    edit_dir = _resolve_or_create_dir(EDIT_DIR_CANDIDATES)

    folder_name = (edit_dir / NAME_FILENAME).read_text(encoding="utf-8").strip()
    if not folder_name:
        raise PcloudAutomationError(f"Файл {NAME_FILENAME} пуст.")

    message_text = load_message_text()
    unique_emails = _load_all_unique_emails()

    if exclude_sent:
        sent = _load_sent_emails_set()
        unique_emails = [email for email in unique_emails if _normalize_email(email) not in sent]
        _save_unsent_emails(unique_emails)

    if not unique_emails:
        if exclude_sent:
            raise PcloudAutomationError(
                "Новых email для отправки нет. Все адреса уже были отправлены раньше."
            )
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
    print(f"✨ {message}", flush=True)
    _write_log(message)


def _get_or_start_ads_session(profile: Profile, ads_client: AdsApiClient) -> AdsBrowserSession:
    profile_id = profile.ads_profile_id.strip()
    cached = _ACTIVE_ADS_SESSIONS.get(profile_id)
    if cached is not None:
        _say("Использую уже открытый ADS профиль.")
        return cached

    _say("Запускаю профиль ADS Browser...")
    session = ads_client.start_browser(
        profile_id=profile_id,
        headless=ADS_HEADLESS,
        open_tabs=ADS_OPEN_TABS,
    )
    _ACTIVE_ADS_SESSIONS[profile_id] = session
    return session


def _force_restart_ads_session(profile: Profile, ads_client: AdsApiClient) -> AdsBrowserSession:
    profile_id = profile.ads_profile_id.strip()
    _ACTIVE_ADS_SESSIONS.pop(profile_id, None)
    try:
        ads_client.stop_browser(profile_id)
    except Exception:  # noqa: BLE001
        pass
    _say("Перезапускаю ADS профиль из-за ошибки подключения...")
    session = ads_client.start_browser(
        profile_id=profile_id,
        headless=ADS_HEADLESS,
        open_tabs=ADS_OPEN_TABS,
    )
    _ACTIVE_ADS_SESSIONS[profile_id] = session
    return session


class PcloudUi:
    def __init__(self, page: Page) -> None:
        self.page = page

    def _dismiss_blocking_overlays(self) -> None:
        close_selectors = [
            "button[aria-label*='close' i]:visible",
            "[role='button'][aria-label*='close' i]:visible",
            "button:has-text('Close'):visible",
            "button:has-text('Not now'):visible",
            "button:has-text('Позже'):visible",
            "button:has-text('Отмена'):visible",
            ".Modal__FadeTranstionContainer-sc-4ci1nh-13 a:has-text('×'):visible",
            ".Modal__FadeTranstionContainer-sc-4ci1nh-13 button:visible",
        ]
        overlay_selector = (
            ".Modal__Overlay-sc-4ci1nh-0:visible, "
            ".Modal__FadeTranstionContainer-sc-4ci1nh-13:visible, "
            ".modal:visible, [role='dialog']:visible, .visitor:visible, .popup:visible"
        )

        for _ in range(3):
            try:
                self.page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass
            self.page.wait_for_timeout(45)

            clicked_close = False
            for selector in close_selectors:
                button = self.page.locator(selector).first
                try:
                    button.click(timeout=300, force=True)
                    clicked_close = True
                    self.page.wait_for_timeout(60)
                    break
                except Exception:  # noqa: BLE001
                    continue

            try:
                if self.page.locator(overlay_selector).count() == 0:
                    return
            except Exception:  # noqa: BLE001
                return

            if not clicked_close:
                self.page.wait_for_timeout(90)

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
        self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        # pCloud keeps background requests, so networkidle can hang forever.
        self.page.wait_for_load_state("load", timeout=5_500)
        self.page.wait_for_selector("body", timeout=3_500)
        self.page.wait_for_timeout(UI_STEP_DELAY_MS)

    def _is_login_page(self) -> bool:
        has_password = self.page.locator("input[type='password']").count() > 0
        has_email = (
            self.page.locator("input[type='email']").count() > 0
            or self.page.locator("input[name*='mail' i]").count() > 0
        )
        return has_password and has_email

    def _visible_dialog(self) -> Locator:
        selector = (
            'div[role="dialog"]:visible, .modal:visible, .visitor:visible, '
            "[class*='dialog']:visible, [class*='modal']:visible, .popup:visible"
        )
        locator = self.page.locator(selector).last
        try:
            locator.wait_for(state="visible", timeout=2_000)
            return locator
        except PlaywrightTimeoutError:
            pass
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
                tab.click(timeout=500)
                self.page.wait_for_timeout(60)
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
                candidate.wait_for(state="visible", timeout=1_000)
                return candidate
            except PlaywrightTimeoutError:
                continue
        raise PcloudAutomationError("Не удалось найти видимое поле Name or Email в окне Invite.")

    def _folder_locator(self, folder_name: str) -> Locator:
        escaped = re.escape(folder_name.strip())
        by_text = self.page.get_by_text(re.compile(rf"^\s*{escaped}\s*$")).first
        grid_name = self.page.locator("div[class*='GridItemName']", has_text=folder_name).first
        grid_folder = self.page.locator(
            "div.itemFolder, div[class*='itemFolder'], div[class*='GridCellWrapper']",
            has=grid_name,
        ).first
        try:
            if grid_folder.count() > 0:
                return grid_folder
        except Exception:  # noqa: BLE001
            pass
        return by_text

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
        self._dismiss_blocking_overlays()
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
            self.page.wait_for_timeout(200)

        folder_menu_item = self.page.get_by_text(
            re.compile(r"^(New Folder|Новая папка|Folder|Папка)$", re.I)
        ).first
        try:
            folder_menu_item.wait_for(state="visible", timeout=1_200)
        except PlaywrightTimeoutError:
            raise PcloudAutomationError(
                "После клика Add не найден пункт New Folder/Folder в выпадающем меню."
            )
        folder_menu_item.click()

        # Flow A: modal/visitor dialog with name input and create button.
        try:
            dialog = self._visible_dialog()
            name_input = dialog.locator(
                "input[name='name']:visible, input[type='text']:visible, input:visible"
            ).first
            name_input.wait_for(state="visible", timeout=1_400)
            name_input.fill(folder_name)

            create_button = dialog.get_by_role(
                "button",
                name=re.compile(r"Create|Save|OK|Done|Готово|Создать", re.I),
            ).first
            try:
                create_button.wait_for(state="visible", timeout=900)
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
        self._dismiss_blocking_overlays()
        folder_label = self._ensure_folder_visible(folder_name, timeout=UI_MEDIUM_TIMEOUT)
        folder_label.scroll_into_view_if_needed()
        try:
            folder_label.click(button="right", force=True, timeout=1_100)
        except Exception:  # noqa: BLE001
            box = folder_label.bounding_box()
            if box is None:
                raise PcloudAutomationError(f'Не удалось открыть меню папки "{folder_name}".')
            self.page.mouse.click(
                box["x"] + box["width"] * 0.5,
                box["y"] + box["height"] * 0.5,
                button="right",
            )
        self.page.wait_for_timeout(50)
        return folder_label

    def _open_invite_dialog(self, folder_name: str) -> None:
        self._dismiss_blocking_overlays()
        self._open_folder_context_menu(folder_name)
        invite_item = self.page.get_by_text(re.compile(r"^Invite to Folder$", re.I)).first
        try:
            invite_item.wait_for(state="visible", timeout=1_100)
            invite_item.click()
            self.page.wait_for_timeout(35)
            self._visible_dialog()
            return
        except Exception:  # noqa: BLE001
            pass

        fallback_item = self.page.get_by_text(re.compile(r"^Invite", re.I)).first
        try:
            fallback_item.wait_for(state="visible", timeout=800)
            fallback_item.click()
            self.page.wait_for_timeout(35)
            self._visible_dialog()
            return
        except Exception as exc:  # noqa: BLE001
            raise PcloudAutomationError("Не удалось нажать пункт 'Invite to Folder' в меню папки.") from exc

    def invite_batch(self, folder_name: str, emails: list[str], message_text: str) -> None:
        self._open_invite_dialog(folder_name)
        dialog = self._visible_dialog()
        self._ensure_invite_tab(dialog)

        for email in emails:
            sent = False
            for _ in range(2):
                try:
                    email_input = self._get_visible_email_input(dialog)
                    email_input.click(timeout=700)
                    email_input.fill(email, timeout=700)
                    email_input.press("Enter")
                    self.page.wait_for_timeout(30)
                    sent = True
                    break
                except Exception:  # noqa: BLE001
                    self.page.wait_for_timeout(60)
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
                        message_area.fill(message_text, timeout=900)
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
                button.wait_for(state="visible", timeout=800)
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
            self.page.wait_for_timeout(120)


def _run_in_ads_browser(profile: Profile, worker: Callable[[PcloudUi], T]) -> T:
    if sync_playwright is None:
        raise PcloudAutomationError(
            "Модуль playwright не установлен. Выполните: pip install -r requirements.txt"
        )

    ads_client = AdsApiClient()
    ads_session: AdsBrowserSession | None = None
    page: Page | None = None
    browser = None

    try:
        ads_session = _get_or_start_ads_session(profile, ads_client)

        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.connect_over_cdp(ads_session.cdp_url)
            except Exception:  # noqa: BLE001
                ads_session = _force_restart_ads_session(profile, ads_client)
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
            # Disconnect only from CDP session; do not stop ADS profile.
            if browser is not None:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass
            return result
    except AdsApiError:
        if ads_session is not None:
            _ACTIVE_ADS_SESSIONS.pop(ads_session.profile_id, None)
        raise
    except Exception as exc:  # noqa: BLE001
        if ads_session is not None:
            _ACTIVE_ADS_SESSIONS.pop(ads_session.profile_id, None)
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


def setup_folder(profile: Profile) -> SetupStats:
    ensure_input_files()
    folder_name, _, _ = load_job_data(exclude_sent=False)
    _say(f'Настройка: папка "{folder_name}"')

    def worker(ui: PcloudUi) -> SetupStats:
        _say("Пробую создать папку...")
        created = ui.create_folder(folder_name)
        if created:
            _say("Папка создана.")
        else:
            _say("Папка уже есть.")
        return SetupStats(folder_name=folder_name, folder_created=created)

    stats = _run_in_ads_browser(profile, worker)
    if stats.folder_created:
        _say(f'Готово: папка "{stats.folder_name}" создана.')
    else:
        _say(f'Папка "{stats.folder_name}" уже существовала.')
    return stats


def send_invites(profile: Profile) -> RunStats:
    ensure_input_files()
    folder_name, message_text, emails = load_job_data(exclude_sent=True)
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
                _append_sent_emails(batch)
                _say(f"Пакет {index}: успешно.")
            except Exception as batch_exc:  # noqa: BLE001
                failed_batches += 1
                _write_log(f"Batch failed ({len(batch)} emails): {batch_exc}")
                _say(f"Пакет {index}: не удалось отправить, перехожу к следующему.")
            time.sleep(BATCH_DELAY_SECONDS)

        return RunStats(total_emails=len(emails), sent_emails=sent_emails, failed_batches=failed_batches)

    stats = _run_in_ads_browser(profile, worker)
    _save_unsent_emails(get_unsent_emails())
    _say(
        f"Отправка завершена: {stats.sent_emails}/{stats.total_emails}, "
        f"ошибочных пакетов: {stats.failed_batches}"
    )
    return stats


def _next_inbox_test_folder_name() -> str:
    ensure_input_files()
    raw_value = INBOX_TEST_COUNTER_FILE.read_text(encoding="utf-8").strip()
    try:
        current = int(raw_value)
    except ValueError:
        current = 0
    current += 1
    INBOX_TEST_COUNTER_FILE.write_text(str(current), encoding="utf-8")
    return f"TEST{current}"


def inbox_check(profile: Profile, email: str) -> InboxCheckStats:
    ensure_input_files()
    target_email = _normalize_email(email)
    if not target_email or "@" not in target_email:
        raise PcloudAutomationError("Введите корректный email для проверки инбокса.")

    folder_name = _next_inbox_test_folder_name()
    message_text = load_message_text()
    _say(f'Проверка инбокса: создаю папку "{folder_name}" и отправляю инвайт на {target_email}')

    def worker(ui: PcloudUi) -> InboxCheckStats:
        ui.create_folder(folder_name)
        ui.invite_batch(folder_name, [target_email], message_text)
        return InboxCheckStats(folder_name=folder_name, email=target_email, invite_sent=True)

    return _run_in_ads_browser(profile, worker)


def run_job(profile: Profile) -> RunStats:
    setup_folder(profile)
    return send_invites(profile)


def _write_log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_file = LOGS_DIR / "automation.log"
    with log_file.open("a", encoding="utf-8") as file:
        file.write(f"[{timestamp}] {message}\n")
