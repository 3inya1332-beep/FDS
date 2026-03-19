from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    DEFAULT_START_URL,
    EDIT_DIR_CANDIDATES,
    EMAIL_DIR_CANDIDATES,
    EMAIL_FILENAME,
    LOGS_DIR,
    SENT_EMAILS_LOG_FILENAME,
    TEXT_FILENAME,
)
from profile_store import Profile


class CodaAutomationError(RuntimeError):
    pass


@dataclass
class RunStats:
    total_emails: int
    sent_emails: int
    failed_batches: int
    remaining_emails: int


def _resolve_or_create_dir(candidates: list[Path]) -> Path:
    for folder in candidates:
        if folder.exists():
            return folder
    candidates[0].mkdir(parents=True, exist_ok=True)
    return candidates[0]


def _read_non_empty_lines(file_path: Path) -> list[str]:
    if not file_path.exists():
        return []
    lines = [line.strip() for line in file_path.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line]


def _find_email_file(email_dir: Path) -> Path:
    default_file = email_dir / EMAIL_FILENAME
    if default_file.exists():
        return default_file
    txt_files = sorted(email_dir.glob("*.txt"))
    if txt_files:
        return txt_files[0]
    return default_file


def ensure_input_files() -> None:
    edit_dir = _resolve_or_create_dir(EDIT_DIR_CANDIDATES)
    email_dir = _resolve_or_create_dir(EMAIL_DIR_CANDIDATES)
    logs_dir = LOGS_DIR
    logs_dir.mkdir(parents=True, exist_ok=True)

    text_file = edit_dir / TEXT_FILENAME
    if not text_file.exists():
        text_file.write_text(
            "Hey everyone,\n\nHere's the Coda doc. Take a look and leave comments if needed.",
            encoding="utf-8",
        )

    email_file = _find_email_file(email_dir)
    if not email_file.exists():
        email_file.write_text("example1@mail.com\nexample2@mail.com\n", encoding="utf-8")

    sent_log = logs_dir / SENT_EMAILS_LOG_FILENAME
    if not sent_log.exists():
        sent_log.write_text("", encoding="utf-8")


def get_email_file_path() -> Path:
    email_dir = _resolve_or_create_dir(EMAIL_DIR_CANDIDATES)
    return _find_email_file(email_dir)


def get_pending_emails() -> list[str]:
    return _read_non_empty_lines(get_email_file_path())


def get_sent_history(limit: int = 100) -> list[str]:
    sent_log = LOGS_DIR / SENT_EMAILS_LOG_FILENAME
    if not sent_log.exists():
        return []
    lines = _read_non_empty_lines(sent_log)
    return lines[-limit:]


def load_job_data() -> tuple[str, list[str], Path]:
    edit_dir = _resolve_or_create_dir(EDIT_DIR_CANDIDATES)
    message_text = (edit_dir / TEXT_FILENAME).read_text(encoding="utf-8").strip()

    email_file = get_email_file_path()
    raw_emails = _read_non_empty_lines(email_file)
    if not raw_emails:
        raise CodaAutomationError("Список email пуст. Добавьте адреса в email/emails.txt.")

    unique_emails: list[str] = []
    seen: set[str] = set()
    for email in raw_emails:
        normalized = email.strip().lower()
        if normalized and normalized not in seen:
            unique_emails.append(email.strip())
            seen.add(normalized)

    if not unique_emails:
        raise CodaAutomationError("Список email после очистки пуст.")

    return message_text, unique_emails, email_file


def _write_pending_emails(email_file: Path, emails: list[str]) -> None:
    content = "\n".join(emails)
    if content:
        content += "\n"
    email_file.write_text(content, encoding="utf-8")


def _append_sent_history(profile_id: str, batch: list[str]) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    sent_log = LOGS_DIR / SENT_EMAILS_LOG_FILENAME
    with sent_log.open("a", encoding="utf-8") as file:
        for email in batch:
            file.write(f"{timestamp} | profile={profile_id} | {email}\n")


def _write_log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_file = LOGS_DIR / "automation.log"
    with log_file.open("a", encoding="utf-8") as file:
        file.write(f"[{timestamp}] {message}\n")


class CodaUi:
    def __init__(self, page: Page) -> None:
        self.page = page

    def open_doc(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=120_000)
        self.page.wait_for_timeout(1500)
        self.page.get_by_role("button", name=re.compile(r"^Share$", re.I)).first.wait_for(
            state="visible",
            timeout=60_000,
        )

    def _open_share_dialog(self) -> Locator:
        share_button = self.page.get_by_role("button", name=re.compile(r"^Share$", re.I)).first
        share_button.wait_for(state="visible", timeout=30_000)
        share_button.click()
        dialog = self.page.locator('div[role="dialog"]:visible, .modal:visible').last
        dialog.wait_for(state="visible", timeout=20_000)
        return dialog

    @staticmethod
    def _try_fill_text(target: Locator, text: str, page: Page) -> None:
        try:
            target.fill(text)
            return
        except Exception:  # noqa: BLE001
            pass
        target.click()
        page.keyboard.type(text)

    def _recipient_field(self, dialog: Locator) -> Locator:
        candidates = [
            dialog.get_by_placeholder(re.compile(r"type names or emails", re.I)).first,
            dialog.locator(
                "input[placeholder*='name' i], input[placeholder*='email' i], [contenteditable='true']"
            ).first,
        ]
        for field in candidates:
            try:
                field.wait_for(state="visible", timeout=4_000)
                return field
            except Exception:  # noqa: BLE001
                continue
        raise CodaAutomationError("Не удалось найти поле Type names or emails в Share окне Coda.")

    def _message_field(self, dialog: Locator) -> Locator | None:
        candidates = [
            dialog.get_by_placeholder(re.compile(r"message", re.I)).first,
            dialog.locator("textarea").first,
            dialog.locator("[role='textbox'][aria-label*='message' i]").first,
        ]
        for field in candidates:
            if field.count() == 0:
                continue
            try:
                field.wait_for(state="visible", timeout=2_000)
                return field
            except Exception:  # noqa: BLE001
                continue
        return None

    def _invite_button(self, dialog: Locator) -> Locator:
        candidates = [
            dialog.get_by_role("button", name=re.compile(r"^Invite$", re.I)).first,
            dialog.get_by_role("button", name=re.compile(r"^Send$", re.I)).first,
        ]
        for button in candidates:
            if button.count() == 0:
                continue
            try:
                button.wait_for(state="visible", timeout=4_000)
                return button
            except Exception:  # noqa: BLE001
                continue
        raise CodaAutomationError("Не удалось найти кнопку Invite/Send в Share окне Coda.")

    def _close_dialog(self, dialog: Locator) -> None:
        close_candidates = [
            dialog.get_by_role("button", name=re.compile(r"^Close$", re.I)).first,
            dialog.get_by_role("button", name=re.compile(r"^Back$", re.I)).first,
            dialog.locator("button[aria-label*='close' i]").first,
        ]
        for button in close_candidates:
            if button.count() == 0:
                continue
            try:
                button.click(timeout=1_000)
                self.page.wait_for_timeout(300)
                return
            except Exception:  # noqa: BLE001
                continue
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(300)

    def invite_batch(self, emails: list[str], message_text: str) -> None:
        dialog = self._open_share_dialog()
        recipient_field = self._recipient_field(dialog)

        for email in emails:
            recipient_field.click()
            self._try_fill_text(recipient_field, email, self.page)
            self.page.keyboard.press("Enter")
            self.page.wait_for_timeout(140)

        if message_text:
            message_field = self._message_field(dialog)
            if message_field is not None:
                message_field.click()
                self.page.keyboard.press("Control+A")
                self._try_fill_text(message_field, message_text, self.page)

        invite_button = self._invite_button(dialog)
        invite_button.click()

        # Coda may keep the dialog open after successful invite.
        self.page.wait_for_timeout(1200)
        try:
            dialog.wait_for(state="hidden", timeout=6_000)
        except PlaywrightTimeoutError:
            self._close_dialog(dialog)


def run_job(profile: Profile) -> RunStats:
    if sync_playwright is None:
        raise CodaAutomationError(
            "Модуль playwright не установлен. Выполните: pip install -r requirements.txt"
        )

    ensure_input_files()
    message_text, all_emails, email_file = load_job_data()
    pending_emails = all_emails.copy()

    ads_client = AdsApiClient()
    ads_session = None
    sent_emails = 0
    failed_batches = 0

    try:
        _write_log(f"Job started: profile={profile.ads_profile_id}; emails={len(all_emails)}")
        ads_session = ads_client.start_browser(
            profile_id=profile.ads_profile_id,
            headless=ADS_HEADLESS,
            open_tabs=ADS_OPEN_TABS,
        )

        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(ads_session.cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()

            ui = CodaUi(page)
            ui.open_doc(profile.start_url or DEFAULT_START_URL)

            while pending_emails:
                batch = pending_emails[:BATCH_SIZE]
                try:
                    ui.invite_batch(batch, message_text)
                    sent_emails += len(batch)
                    _append_sent_history(profile.ads_profile_id, batch)
                    pending_emails = pending_emails[len(batch) :]
                    _write_pending_emails(email_file, pending_emails)
                    _write_log(f"Batch sent: {len(batch)} emails")
                except Exception as batch_exc:  # noqa: BLE001
                    failed_batches += 1
                    _write_log(f"Batch failed ({len(batch)} emails): {batch_exc}")
                    break
                time.sleep(BATCH_DELAY_SECONDS)

            browser.close()

    except AdsApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CodaAutomationError(str(exc)) from exc
    finally:
        if ads_session is not None:
            ads_client.stop_browser(ads_session.profile_id)
        _write_log(
            f"Job finished: total={len(all_emails)}, sent={sent_emails}, "
            f"failed_batches={failed_batches}, remaining={len(pending_emails)}"
        )

    return RunStats(
        total_emails=len(all_emails),
        sent_emails=sent_emails,
        failed_batches=failed_batches,
        remaining_emails=len(pending_emails),
    )
