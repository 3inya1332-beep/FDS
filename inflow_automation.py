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
    DEFAULT_INFLOW_URL,
    EDIT_DIR_CANDIDATES,
    EMAIL_DIR_CANDIDATES,
    EMAIL_FILENAMES,
    LOGS_DIR,
    MESSAGE_FILENAME,
    SEND_DELAY_SECONDS,
    SUBJECT_FILENAME,
)
from profile_store import Profile


class InflowAutomationError(RuntimeError):
    pass


@dataclass
class RunStats:
    total_emails: int
    total_orders: int
    sent_orders: int
    failed_orders: int
    skipped_emails: int
    last_error: str | None


def _resolve_or_create_dir(candidates: list[Path]) -> Path:
    for folder in candidates:
        if folder.exists():
            return folder
    candidates[0].mkdir(parents=True, exist_ok=True)
    return candidates[0]


def ensure_input_files() -> None:
    edit_dir = _resolve_or_create_dir(EDIT_DIR_CANDIDATES)
    email_dir = _resolve_or_create_dir(EMAIL_DIR_CANDIDATES)

    subject_file = edit_dir / SUBJECT_FILENAME
    if not subject_file.exists():
        subject_file.write_text("Purchase Order", encoding="utf-8")

    message_file = edit_dir / MESSAGE_FILENAME
    if not message_file.exists():
        message_file.write_text("Hi,\n\nHere is your Purchase Order.", encoding="utf-8")

    email_file = email_dir / EMAIL_FILENAMES[0]
    if not email_file.exists():
        email_file.write_text(
            "to_example@mail.com\ncc_example@mail.com\nbcc_example@mail.com\n",
            encoding="utf-8",
        )

    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _read_non_empty_lines(file_path: Path) -> list[str]:
    if not file_path.exists():
        raise InflowAutomationError(f"Файл не найден: {file_path}")
    lines = [line.strip() for line in file_path.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line]


def _find_email_file(email_dir: Path) -> Path:
    for filename in EMAIL_FILENAMES:
        candidate = email_dir / filename
        if candidate.exists():
            return candidate

    txt_files = sorted(email_dir.glob("*.txt"))
    if txt_files:
        return txt_files[0]

    raise InflowAutomationError(
        f"В папке {email_dir} нет txt-файла с email адресами. Добавьте файл emails.txt или email.txt."
    )


def _group_email_triplets(emails: list[str]) -> list[tuple[str, str, str]]:
    triplets: list[tuple[str, str, str]] = []
    full_count = len(emails) // 3
    for index in range(full_count):
        chunk = emails[index * 3 : index * 3 + 3]
        to_email, cc_email, bcc_email = chunk
        # The user requested different emails for TO/CC/BCC.
        if len({to_email.lower(), cc_email.lower(), bcc_email.lower()}) < 3:
            raise InflowAutomationError(
                f"Тройка email #{index + 1} содержит дубликаты: {to_email}, {cc_email}, {bcc_email}"
            )
        triplets.append((to_email, cc_email, bcc_email))
    return triplets


def load_job_data() -> tuple[str, str, list[str], list[tuple[str, str, str]]]:
    edit_dir = _resolve_or_create_dir(EDIT_DIR_CANDIDATES)
    email_dir = _resolve_or_create_dir(EMAIL_DIR_CANDIDATES)

    subject = (edit_dir / SUBJECT_FILENAME).read_text(encoding="utf-8").strip()
    if not subject:
        raise InflowAutomationError(f"Файл {SUBJECT_FILENAME} пуст.")

    message = (edit_dir / MESSAGE_FILENAME).read_text(encoding="utf-8").strip()
    if not message:
        raise InflowAutomationError(f"Файл {MESSAGE_FILENAME} пуст.")

    email_file = _find_email_file(email_dir)
    emails = _read_non_empty_lines(email_file)
    if len(emails) < 3:
        raise InflowAutomationError("Нужно минимум 3 email для заполнения TO/CC/BCC.")

    triplets = _group_email_triplets(emails)
    if not triplets:
        raise InflowAutomationError("Нет полной тройки email для TO/CC/BCC.")

    return subject, message, emails, triplets


class InflowUi:
    def __init__(self, page: Page) -> None:
        self.page = page
        self._f11_used = False

    def _email_button_candidates(self) -> list[Locator]:
        return [
            self.page.get_by_role("button", name=re.compile(r"^Email$", re.I)),
            self.page.get_by_role("link", name=re.compile(r"^Email$", re.I)),
            self.page.locator("button:has-text('Email')"),
            self.page.locator("a:has-text('Email')"),
            self.page.get_by_text(re.compile(r"^Email$", re.I)),
        ]

    def _purchase_order_candidates(self) -> list[Locator]:
        return [
            self.page.get_by_role("menuitem", name=re.compile(r"Purchase order", re.I)),
            self.page.get_by_role("button", name=re.compile(r"Purchase order", re.I)),
            self.page.get_by_text(re.compile(r"^Purchase order$", re.I)),
        ]

    def maximize_window(self) -> None:
        try:
            self.page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass

        # Primary path: ask Chromium to maximize the native browser window.
        try:
            cdp = self.page.context.new_cdp_session(self.page)
            window_info = cdp.send("Browser.getWindowForTarget")
            window_id = window_info.get("windowId")
            if isinstance(window_id, int):
                cdp.send(
                    "Browser.setWindowBounds",
                    {"windowId": window_id, "bounds": {"windowState": "maximized"}},
                )
                self.page.wait_for_timeout(250)
        except Exception:  # noqa: BLE001
            pass

        # Fallback: emulate F11 fullscreen (some ADS builds ignore window bounds).
        if not self._f11_used:
            try:
                self.page.keyboard.press("F11")
                self.page.wait_for_timeout(250)
                self._f11_used = True
            except Exception:  # noqa: BLE001
                pass

        # Final fallback: viewport + JS resize.
        try:
            self.page.set_viewport_size({"width": 1920, "height": 1080})
        except Exception:  # noqa: BLE001
            pass
        try:
            self.page.evaluate(
                "() => { try { window.moveTo(0, 0); window.resizeTo(screen.availWidth, screen.availHeight); } catch (e) {} }"
            )
        except Exception:  # noqa: BLE001
            pass

    def wait_until_ready(self, timeout_ms: int = 60_000) -> None:
        deadline = time.time() + (timeout_ms / 1000)
        while time.time() < deadline:
            for locator in self._email_button_candidates():
                try:
                    if locator.count() == 0:
                        continue
                    locator.first.wait_for(state="visible", timeout=1_000)
                    return
                except Exception:  # noqa: BLE001
                    continue
            self.page.wait_for_timeout(400)
        raise InflowAutomationError("Страница Inflow не готова: кнопка Email не появилась вовремя.")

    def open_page(self, url: str) -> None:
        self.maximize_window()
        self.page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        self.page.wait_for_load_state("networkidle", timeout=60_000)
        self.maximize_window()
        self.wait_until_ready(timeout_ms=90_000)

    def _click_first_available(self, candidates: list[Locator], description: str) -> None:
        for locator in candidates:
            try:
                if locator.count() == 0:
                    continue
                target = locator.first
                target.wait_for(state="visible", timeout=8_000)
                target.click()
                return
            except Exception:  # noqa: BLE001
                continue
        raise InflowAutomationError(f"Не удалось нажать: {description}")

    def _visible_dialog(self, timeout_ms: int = 20_000) -> Locator:
        candidates = [
            self.page.locator('div[role="dialog"]:visible').last,
            self.page.locator(".modal:visible").last,
        ]
        for dialog in candidates:
            try:
                if dialog.count() == 0:
                    continue
                dialog.wait_for(state="visible", timeout=timeout_ms)
                return dialog
            except Exception:  # noqa: BLE001
                continue
        raise InflowAutomationError("Не удалось найти открытое окно отправки письма.")

    def _visible_dialog_or_none(self) -> Locator | None:
        try:
            return self._visible_dialog(timeout_ms=800)
        except Exception:  # noqa: BLE001
            return None

    def has_send_dialog_open(self) -> bool:
        return self._visible_dialog_or_none() is not None

    def open_purchase_order_modal(self) -> Locator:
        already_open = self._visible_dialog_or_none()
        if already_open is not None:
            return already_open

        last_error: Exception | None = None
        # Click Email only once, and if modal didn't open in 20s - click one more time.
        for attempt in range(2):
            try:
                self.wait_until_ready(timeout_ms=25_000)
                self._click_first_available(self._email_button_candidates(), "кнопка Email")
                self._click_first_available(self._purchase_order_candidates(), "пункт Purchase order")
                return self._visible_dialog(timeout_ms=20_000)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt == 0:
                    self.page.wait_for_timeout(20_000)
        raise InflowAutomationError(f"Не удалось открыть окно Purchase order: {last_error}")

    def _type_into_field(self, field: Locator, value: str, *, confirm_with_enter: bool) -> None:
        field.scroll_into_view_if_needed()
        field.click()
        success = False
        try:
            field.fill(value)
            success = True
        except Exception:  # noqa: BLE001
            pass

        if not success:
            try:
                field.evaluate(
                    "(el, v) => {"
                    "  el.focus();"
                    "  if ('value' in el) { el.value = v; } else { el.textContent = v; }"
                    "  el.dispatchEvent(new Event('input', { bubbles: true }));"
                    "  el.dispatchEvent(new Event('change', { bubbles: true }));"
                    "}",
                    value,
                )
                success = True
            except Exception:  # noqa: BLE001
                pass

        if not success:
            self.page.keyboard.press("Control+A")
            self.page.keyboard.press("Backspace")
            self.page.keyboard.type(value, delay=25)

        if confirm_with_enter:
            self.page.keyboard.press("Enter")
            self.page.wait_for_timeout(200)

    def _first_visible_field(self, candidates: list[Locator], timeout_ms: int = 2_000) -> Locator | None:
        for locator in candidates:
            try:
                if locator.count() == 0:
                    continue
                field = locator.first
                field.wait_for(state="visible", timeout=timeout_ms)
                return field
            except Exception:  # noqa: BLE001
                continue
        return None

    def _dialog_editable_inputs(self, dialog: Locator) -> list[Locator]:
        editable: list[Locator] = []
        inputs = dialog.locator("input:not([type='hidden'])")
        for idx in range(inputs.count()):
            try:
                field = inputs.nth(idx)
                if not field.is_visible():
                    continue
                if field.get_attribute("disabled") is not None:
                    continue
                if field.get_attribute("readonly") is not None:
                    continue
                editable.append(field)
            except Exception:  # noqa: BLE001
                continue
        return editable

    def _fill_recipient_input(
        self,
        dialog: Locator,
        email: str,
        placeholder_pattern: str,
        label: str,
        fallback_index: int,
    ) -> None:
        candidates = [
            dialog.get_by_placeholder(re.compile(placeholder_pattern, re.I)),
            self.page.get_by_placeholder(re.compile(placeholder_pattern, re.I)),
            dialog.locator(
                "xpath=.//*[contains(translate(normalize-space(text()), "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                f"'{label.lower()}')]/following::input[1]"
            ),
            self.page.locator(
                "xpath=//*[contains(translate(normalize-space(text()), "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                f"'{label.lower()}')]/following::input[1]"
            ),
        ]
        field = self._first_visible_field(candidates)
        if field is None:
            editable_inputs = self._dialog_editable_inputs(dialog)
            if len(editable_inputs) > fallback_index:
                field = editable_inputs[fallback_index]
        if field is None:
            raise InflowAutomationError(f"Не удалось найти поле {label}.")
        self._type_into_field(field, email, confirm_with_enter=True)

    def _fill_subject_input(self, dialog: Locator, subject: str, fallback_index: int) -> None:
        candidates = [
            dialog.locator(
                "xpath=.//*[contains(translate(normalize-space(text()), "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                "'subject')]/following::input[1]"
            ),
            self.page.locator(
                "xpath=//*[contains(translate(normalize-space(text()), "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                "'subject')]/following::input[1]"
            ),
            dialog.get_by_placeholder(re.compile(r"subject", re.I)),
            self.page.get_by_placeholder(re.compile(r"subject", re.I)),
        ]
        field = self._first_visible_field(candidates)
        if field is None:
            editable_inputs = self._dialog_editable_inputs(dialog)
            if len(editable_inputs) > fallback_index:
                field = editable_inputs[fallback_index]
        if field is None:
            raise InflowAutomationError("Не удалось найти поле Subject.")
        self._type_into_field(field, subject, confirm_with_enter=False)

    def _fill_message(self, dialog: Locator, message: str) -> None:
        label_area = dialog.locator(
            "xpath=.//*[contains(translate(normalize-space(text()), "
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'document')]/following::*"
            "[@contenteditable='true' or self::textarea][1]"
        )
        if label_area.count() > 0:
            field = label_area.first
            field.wait_for(state="visible", timeout=8_000)
            self._type_into_field(field, message, confirm_with_enter=False)
            return

        textareas = dialog.locator("textarea")
        for idx in range(textareas.count()):
            try:
                field = textareas.nth(idx)
                if not field.is_visible():
                    continue
                box = field.bounding_box()
                if box and box.get("height", 0) >= 80:
                    self._type_into_field(field, message, confirm_with_enter=False)
                    return
            except Exception:  # noqa: BLE001
                continue

        editables = dialog.locator("[contenteditable='true']")
        for idx in range(editables.count()):
            try:
                field = editables.nth(idx)
                if not field.is_visible():
                    continue
                box = field.bounding_box()
                if box and box.get("height", 0) >= 80:
                    self._type_into_field(field, message, confirm_with_enter=False)
                    return
            except Exception:  # noqa: BLE001
                continue

        raise InflowAutomationError("Не удалось найти поле MESSAGE.")

    def send_purchase_order(
        self,
        to_email: str,
        cc_email: str,
        bcc_email: str,
        subject: str,
        message: str,
    ) -> None:
        dialog = self.open_purchase_order_modal()
        _write_log("Send modal opened, start filling fields.")

        self._fill_recipient_input(dialog, to_email, r"enter\s*to\s*email", "to", fallback_index=0)
        self._fill_recipient_input(dialog, cc_email, r"enter\s*cc\s*email", "cc", fallback_index=1)
        self._fill_recipient_input(dialog, bcc_email, r"enter\s*bcc\s*email", "bcc", fallback_index=2)
        self._fill_subject_input(dialog, subject, fallback_index=3)
        self._fill_message(dialog, message)
        _write_log("Fields filled, clicking Send.")

        self._click_first_available(
            [
                dialog.get_by_role("button", name=re.compile(r"^Send$", re.I)),
                dialog.locator("button:has-text('Send')"),
            ],
            "кнопка Send",
        )

        try:
            dialog.wait_for(state="hidden", timeout=20_000)
        except PlaywrightTimeoutError as exc:
            raise InflowAutomationError("Окно отправки не закрылось после нажатия Send.") from exc


def run_job(profile: Profile) -> RunStats:
    if sync_playwright is None:
        raise InflowAutomationError(
            "Модуль playwright не установлен. Выполните: pip install -r requirements.txt"
        )

    ensure_input_files()
    subject, message, emails, triplets = load_job_data()

    ads_client = AdsApiClient()
    ads_session = None
    sent_orders = 0
    failed_orders = 0
    skipped_emails = len(emails) - len(triplets) * 3
    last_error: str | None = None
    fatal_error = False

    try:
        _write_log(
            "Job started "
            f"profile={profile.ads_profile_id}; url={profile.start_url}; "
            f"emails={len(emails)}; orders={len(triplets)}; skipped={skipped_emails}"
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

            ui = InflowUi(page)
            ui.open_page(profile.start_url or DEFAULT_INFLOW_URL)

            for to_email, cc_email, bcc_email in triplets:
                sent_current = False
                for attempt in range(3):
                    try:
                        ui.send_purchase_order(
                            to_email=to_email,
                            cc_email=cc_email,
                            bcc_email=bcc_email,
                            subject=subject,
                            message=message,
                        )
                        sent_orders += 1
                        sent_current = True
                        _write_log(f"Order sent: TO={to_email}, CC={cc_email}, BCC={bcc_email}")
                        break
                    except Exception as order_exc:  # noqa: BLE001
                        last_error = str(order_exc)
                        _write_log(
                            "Order attempt failed: "
                            f"attempt={attempt + 1}/3; TO={to_email}, CC={cc_email}, BCC={bcc_email}; "
                            f"error={order_exc}"
                        )
                        try:
                            ui.maximize_window()
                            if not ui.has_send_dialog_open():
                                ui.wait_until_ready(timeout_ms=12_000)
                        except Exception as heal_exc:  # noqa: BLE001
                            _write_log(f"Recovery wait failed: {heal_exc}")
                        self_heal_delay = 1.5 + attempt
                        time.sleep(self_heal_delay)
                if not sent_current:
                    failed_orders += 1
                time.sleep(SEND_DELAY_SECONDS)
    except AdsApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        fatal_error = True
        last_error = str(exc)
        raise InflowAutomationError(str(exc)) from exc
    finally:
        if ads_session is not None and not (fatal_error or failed_orders > 0):
            ads_client.stop_browser(ads_session.profile_id)
        elif ads_session is not None:
            _write_log("Browser left open because there were automation errors.")
        _write_log(
            f"Job finished: total_emails={len(emails)}, total_orders={len(triplets)}, "
            f"sent_orders={sent_orders}, failed_orders={failed_orders}, skipped={skipped_emails}"
        )

    return RunStats(
        total_emails=len(emails),
        total_orders=len(triplets),
        sent_orders=sent_orders,
        failed_orders=failed_orders,
        skipped_emails=skipped_emails,
        last_error=last_error,
    )


def _write_log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_file = LOGS_DIR / "automation.log"
    with log_file.open("a", encoding="utf-8") as file:
        file.write(f"[{timestamp}] {message}\n")
