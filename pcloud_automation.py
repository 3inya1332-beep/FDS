from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

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


class PcloudUi:
    def __init__(self, page: Page) -> None:
        self.page = page

    def open_home(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        self.page.wait_for_timeout(1500)

    def _visible_dialog(self) -> Locator:
        dialog = self.page.locator('div[role="dialog"]:visible, .modal:visible').last
        dialog.wait_for(state="visible", timeout=20_000)
        return dialog

    def create_folder(self, folder_name: str) -> None:
        add_button = self.page.get_by_role("button", name=re.compile(r"^Add$", re.I)).first
        add_button.wait_for(state="visible", timeout=30_000)
        add_button.click()

        folder_menu_item = self.page.get_by_text(re.compile(r"^Folder$", re.I)).first
        folder_menu_item.wait_for(state="visible", timeout=10_000)
        folder_menu_item.click()

        dialog = self._visible_dialog()
        name_input = dialog.locator("input[type='text'], input").first
        name_input.wait_for(state="visible", timeout=10_000)
        name_input.fill(folder_name)

        create_button = dialog.get_by_role("button", name=re.compile(r"Create|Save|OK", re.I)).first
        create_button.click()

        folder_label = self.page.get_by_text(folder_name, exact=True).first
        folder_label.wait_for(state="visible", timeout=25_000)

    def _open_folder_context_menu(self, folder_name: str) -> None:
        folder_label = self.page.get_by_text(folder_name, exact=True).first
        folder_label.wait_for(state="visible", timeout=25_000)
        folder_label.scroll_into_view_if_needed()
        folder_label.click(button="right")

    def invite_batch(self, folder_name: str, emails: list[str], message_text: str) -> None:
        self._open_folder_context_menu(folder_name)

        invite_item = self.page.get_by_text(re.compile(r"Invite to Folder", re.I)).first
        invite_item.wait_for(state="visible", timeout=15_000)
        invite_item.click()

        dialog = self._visible_dialog()
        email_input = dialog.locator("input[type='text'], input").first
        email_input.wait_for(state="visible", timeout=10_000)

        for email in emails:
            email_input.click()
            email_input.fill(email)
            email_input.press("Enter")
            self.page.wait_for_timeout(120)

        if message_text:
            message_area = dialog.locator("textarea").first
            if message_area.count() > 0:
                message_area.fill(message_text)

        share_button = dialog.get_by_role("button", name=re.compile(r"^Share$", re.I)).first
        share_button.click()

        try:
            dialog.wait_for(state="hidden", timeout=20_000)
        except PlaywrightTimeoutError:
            # Some pCloud states keep the dialog open with a success toast.
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(500)


def run_job(profile: Profile) -> RunStats:
    if sync_playwright is None:
        raise PcloudAutomationError(
            "Модуль playwright не установлен. Выполните: pip install -r requirements.txt"
        )

    ensure_input_files()
    folder_name, message_text, emails = load_job_data()

    ads_client = AdsApiClient()
    ads_session = None
    sent_emails = 0
    failed_batches = 0

    batches = _chunked(emails, BATCH_SIZE)

    try:
        _write_log(
            f"Job started for profile={profile.ads_profile_id}; folder={folder_name}; emails={len(emails)}"
        )
        ads_session = ads_client.start_browser(
            profile_id=profile.ads_profile_id,
            headless=ADS_HEADLESS,
            open_tabs=ADS_OPEN_TABS,
        )

        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(ads_session.cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()

            ui = PcloudUi(page)
            ui.open_home(profile.start_url or DEFAULT_PCLOUD_URL)
            ui.create_folder(folder_name)

            for batch in batches:
                try:
                    ui.invite_batch(folder_name, batch, message_text)
                    sent_emails += len(batch)
                    _write_log(f"Batch sent successfully: {len(batch)} emails")
                except Exception as batch_exc:  # noqa: BLE001
                    failed_batches += 1
                    _write_log(f"Batch failed ({len(batch)} emails): {batch_exc}")
                time.sleep(BATCH_DELAY_SECONDS)

            browser.close()
    except AdsApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PcloudAutomationError(str(exc)) from exc
    finally:
        if ads_session is not None:
            ads_client.stop_browser(ads_session.profile_id)
        _write_log(
            f"Job finished: total={len(emails)}, sent={sent_emails}, failed_batches={failed_batches}"
        )

    return RunStats(total_emails=len(emails), sent_emails=sent_emails, failed_batches=failed_batches)


def _write_log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_file = LOGS_DIR / "automation.log"
    with log_file.open("a", encoding="utf-8") as file:
        file.write(f"[{timestamp}] {message}\n")
