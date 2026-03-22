from __future__ import annotations

import re
import secrets
import string
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
from anymessage_api import AnyMessageApiError, AnyMessageClient
from config import (
    ADS_HEADLESS,
    ADS_OPEN_TABS,
    AUTO_REREGISTER_ON_LIMIT,
    AUTO_REREGISTER_TRY_LIMIT,
    DEFAULT_INFLOW_URL,
    EDIT_DIR_CANDIDATES,
    EMAIL_DIR_CANDIDATES,
    EMAIL_FILENAMES,
    INFLOW_SIGNUP_URL,
    LOCAL_API_BASE,
    LOGS_DIR,
    MESSAGE_FILENAME,
    ANYMESSAGE_API_BASE,
    ANYMESSAGE_SERVICE,
    PROXY_FORCE_HOST,
    PROXY_FORCE_PORT,
    PROXY_API_DEFAULT_URL,
    SEND_DELAY_SECONDS,
    SUBJECT_FILENAME,
    UK_PHONE_DIGITS,
)
from profile_store import Profile, ProfileStore
from proxy_api import ProxyApiClient, ProxyApiError
from runtime_settings import RuntimeSettings, RuntimeSettingsStore
from sent_store import SentEmailStore


class InflowAutomationError(RuntimeError):
    pass


class MaximumEmailsExceededError(InflowAutomationError):
    pass


@dataclass
class RunStats:
    total_input_emails: int
    already_sent_emails: int
    processable_emails: int
    sent_emails: int
    failed_emails: int
    skipped_emails: int
    last_error: str | None


@dataclass
class DashboardStats:
    total_input_emails: int
    already_sent_emails: int
    unsent_emails: int


@dataclass
class RotationResult:
    new_profile_id: str
    new_start_url: str


def _exc_text(exc: BaseException) -> str:
    message = str(exc).strip()
    if message:
        return message
    return f"{exc.__class__.__name__}: {exc!r}"


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", text.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "step"


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
    SentEmailStore().ensure_ready()


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


def load_message_data() -> tuple[str, str]:
    edit_dir = _resolve_or_create_dir(EDIT_DIR_CANDIDATES)

    subject = (edit_dir / SUBJECT_FILENAME).read_text(encoding="utf-8").strip()
    if not subject:
        raise InflowAutomationError(f"Файл {SUBJECT_FILENAME} пуст.")

    message = (edit_dir / MESSAGE_FILENAME).read_text(encoding="utf-8").strip()
    if not message:
        raise InflowAutomationError(f"Файл {MESSAGE_FILENAME} пуст.")

    return subject, message


def load_job_data() -> tuple[str, str, list[str], list[tuple[str, str, str]], int]:
    subject, message = load_message_data()
    email_dir = _resolve_or_create_dir(EMAIL_DIR_CANDIDATES)

    email_file = _find_email_file(email_dir)
    emails_all = _read_non_empty_lines(email_file)
    if len(emails_all) < 3:
        raise InflowAutomationError("Нужно минимум 3 email для заполнения TO/CC/BCC.")

    sent_store = SentEmailStore()
    sent_set = sent_store.get_all()
    pending_emails = [email for email in emails_all if email.strip().lower() not in sent_set]
    already_sent = len(emails_all) - len(pending_emails)

    if len(pending_emails) < 3:
        raise InflowAutomationError(
            "Недостаточно новых email для отправки (минимум 3). Пополните email файл новыми адресами."
        )

    triplets = _group_email_triplets(pending_emails)
    if not triplets:
        raise InflowAutomationError("Нет полной тройки email для TO/CC/BCC.")

    processable = len(triplets) * 3
    return subject, message, emails_all, triplets, already_sent + (len(pending_emails) - processable)


def get_dashboard_stats() -> DashboardStats:
    try:
        email_dir = _resolve_or_create_dir(EMAIL_DIR_CANDIDATES)
        email_file = _find_email_file(email_dir)
        emails_all = _read_non_empty_lines(email_file)
    except Exception:  # noqa: BLE001
        emails_all = []

    sent_store = SentEmailStore()
    sent_set = sent_store.get_all()
    already_sent = sum(1 for email in emails_all if email.strip().lower() in sent_set)
    unsent = max(0, len(emails_all) - already_sent)

    return DashboardStats(
        total_input_emails=len(emails_all),
        already_sent_emails=already_sent,
        unsent_emails=unsent,
    )


def _load_runtime_settings(runtime_settings: RuntimeSettings | None) -> RuntimeSettings:
    if runtime_settings is not None:
        return runtime_settings
    return RuntimeSettingsStore().load()


def _build_ads_client(settings: RuntimeSettings) -> AdsApiClient:
    return AdsApiClient(base_url=LOCAL_API_BASE, api_key=settings.ads_api_key)


def _build_anymessage_client(settings: RuntimeSettings) -> AnyMessageClient:
    return AnyMessageClient(
        api_base=ANYMESSAGE_API_BASE,
        api_token=settings.anymessage_api_token,
        service=ANYMESSAGE_SERVICE,
    )


def _resolve_best_proxy(settings: RuntimeSettings) -> dict[str, str] | None:
    # Enforce user-requested proxy source URL.
    proxy_url = PROXY_API_DEFAULT_URL
    if not proxy_url:
        return None
    client = ProxyApiClient(api_url=proxy_url)
    candidate = client.fetch_best_proxy()
    if candidate.ping_ms is not None:
        _write_log(
            f"Proxy selected by best ping: {candidate.proxy_host}:{candidate.proxy_port} "
            f"({candidate.ping_ms:.2f}ms)"
        )
    else:
        _write_log(f"Proxy selected: {candidate.proxy_host}:{candidate.proxy_port} (ping not provided)")
    payload = candidate.to_ads_payload()
    payload["proxy_type"] = "http"
    payload["proxy_host"] = PROXY_FORCE_HOST
    payload["proxy_port"] = PROXY_FORCE_PORT
    payload["proxy_user"] = ""
    payload["proxy_username"] = ""
    payload["proxy_password"] = ""
    _write_log(f"Proxy endpoint forced to local gateway: {PROXY_FORCE_HOST}:{PROXY_FORCE_PORT}")
    return payload


def _random_password(length: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    if length < 10:
        length = 10
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _random_word(length: int = 8) -> str:
    alphabet = string.ascii_lowercase
    return "".join(secrets.choice(alphabet) for _ in range(max(4, length)))


def _random_uk_phone(digits: int = UK_PHONE_DIGITS) -> str:
    digits = max(10, digits)
    # Typical UK mobile format starts with 07 + 9 digits.
    return "07" + "".join(secrets.choice(string.digits) for _ in range(digits - 2))


class InflowUi:
    def __init__(self, page: Page) -> None:
        self.page = page
        self._window_maximized_once = False

    def _email_button_candidates(self) -> list[Locator]:
        return [
            self.page.get_by_role("button", name=re.compile(r"^Email$", re.I)),
            self.page.get_by_role("link", name=re.compile(r"^Email$", re.I)),
            self.page.locator("[title='Email'], [aria-label='Email']"),
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

    def _send_modal_candidates(self) -> list[Locator]:
        return [
            self.page.locator(
                "xpath=//div[.//*[contains(translate(normalize-space(text()), "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'send purchase order')] "
                "and .//button[contains(translate(normalize-space(text()), "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'send')]]"
            ),
            self.page.locator(
                "xpath=//*[contains(translate(normalize-space(text()), "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'send purchase order')]/"
                "ancestor::*[@role='dialog' or contains(@class,'modal')][1]"
            ),
            self.page.locator(
                "xpath=//input[contains(translate(@placeholder, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                "'enter to email')]/ancestor::div[.//button[contains(translate(normalize-space(text()), "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'send')]][1]"
            ),
        ]

    def maximize_window(self) -> None:
        # Do not spam fullscreen hotkeys (F11 can toggle and "shake" UI).
        # Keep this method idempotent and non-intrusive.
        if self._window_maximized_once:
            try:
                self.page.set_viewport_size({"width": 1920, "height": 1080})
            except Exception:  # noqa: BLE001
                pass
            return

        try:
            self.page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass

        # Prefer native window maximize via CDP.
        try:
            cdp = self.page.context.new_cdp_session(self.page)
            window_info = cdp.send("Browser.getWindowForTarget")
            window_id = window_info.get("windowId")
            if isinstance(window_id, int):
                try:
                    cdp.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": {"windowState": "maximized"}})
                except Exception:  # noqa: BLE001
                    cdp.send(
                        "Browser.setWindowBounds",
                        {"windowId": window_id, "bounds": {"left": 0, "top": 0, "width": 1920, "height": 1080}},
                    )
        except Exception:  # noqa: BLE001
            pass

        try:
            self.page.set_viewport_size({"width": 1920, "height": 1080})
        except Exception:  # noqa: BLE001
            pass
        try:
            self.page.evaluate(
                "() => {"
                "  try {"
                "    window.moveTo(0, 0);"
                "    window.resizeTo(screen.availWidth, screen.availHeight);"
                "  } catch (e) {}"
                "}"
            )
        except Exception:  # noqa: BLE001
            pass

        self._window_maximized_once = True

    def capture_debug_screenshot(self, tag: str) -> str | None:
        screenshots_dir = LOGS_DIR / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{time.strftime('%Y%m%d_%H%M%S')}_{_slug(tag)}.png"
        file_path = screenshots_dir / filename
        try:
            self.page.screenshot(path=str(file_path), full_page=True, timeout=8_000)
            _write_log(f"AUTO-SCREENSHOT: {file_path}")
            return str(file_path)
        except Exception:  # noqa: BLE001
            try:
                self.page.screenshot(path=str(file_path), timeout=8_000)
                _write_log(f"AUTO-SCREENSHOT: {file_path}")
                return str(file_path)
            except Exception as exc:  # noqa: BLE001
                _write_log(f"Auto-screenshot failed for {tag}: {exc}")
                return None

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

    def _recover_navigation_page(self) -> None:
        try:
            if self.page.is_closed():
                self.page = self.page.context.new_page()
                return
        except Exception:  # noqa: BLE001
            pass

        try:
            self.page.goto("about:blank", wait_until="commit", timeout=5_000)
            return
        except Exception:  # noqa: BLE001
            pass

        try:
            new_page = self.page.context.new_page()
            try:
                self.page.close()
            except Exception:  # noqa: BLE001
                pass
            self.page = new_page
        except Exception:  # noqa: BLE001
            pass

    def _goto_with_retries(
        self,
        url: str,
        *,
        description: str,
        attempts: int = 4,
        wait_until: str = "domcontentloaded",
        timeout_ms: int = 45_000,
    ) -> None:
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                self.maximize_window()
                self.page.goto(url, wait_until=wait_until, timeout=timeout_ms)
                self.maximize_window()
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                _write_log(
                    f"Navigation failed: {description}; attempt={attempt}/{attempts}; "
                    f"url={url}; error={exc}"
                )
                self._recover_navigation_page()
                self.page.wait_for_timeout(700 + attempt * 150)
        screenshot_path = self.capture_debug_screenshot(f"nav_fail_{description}")
        screenshot_note = f" | screenshot: {screenshot_path}" if screenshot_path else ""
        raise InflowAutomationError(
            f"Не удалось открыть {description} после {attempts} попыток. "
            f"Последняя ошибка: {last_error}{screenshot_note}"
        )

    def open_page(self, url: str) -> None:
        self._goto_with_retries(url, description="рабочую страницу", attempts=4, wait_until="domcontentloaded")
        try:
            self.page.wait_for_load_state("networkidle", timeout=12_000)
        except Exception:  # noqa: BLE001
            pass
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
        candidates = [*self._send_modal_candidates(), self.page.locator('div[role="dialog"]:visible').last]
        for dialog in candidates:
            try:
                if dialog.count() == 0:
                    continue
                target = dialog.first
                target.wait_for(state="visible", timeout=timeout_ms)
                return target
            except Exception:  # noqa: BLE001
                continue
        raise InflowAutomationError("Не удалось найти открытое окно отправки письма.")

    def _visible_dialog_or_none(self) -> Locator | None:
        try:
            return self._visible_dialog(timeout_ms=800)
        except Exception:  # noqa: BLE001
            return None

    def has_send_dialog_open(self) -> bool:
        quick_fields = [
            self.page.get_by_placeholder(re.compile(r"enter\s*to\s*email", re.I)),
            self.page.get_by_placeholder(re.compile(r"enter\s*cc\s*email", re.I)),
            self.page.get_by_placeholder(re.compile(r"enter\s*bcc\s*email", re.I)),
        ]
        for field in quick_fields:
            try:
                if field.count() > 0 and field.first.is_visible():
                    return True
            except Exception:  # noqa: BLE001
                continue
        return self._visible_dialog_or_none() is not None

    def open_purchase_order_modal(self) -> Locator:
        already_open = self._visible_dialog_or_none()
        if already_open is not None:
            _write_log("Send modal already open, skip Email click.")
            return already_open
        if self.has_send_dialog_open():
            _write_log("Send modal detected by placeholders, skip Email click.")
            return self.page.locator("body").first

        last_error: Exception | None = None
        # Fast open flow: avoid long waits between retries.
        for attempt in range(2):
            try:
                self.wait_until_ready(timeout_ms=8_000)
                self._click_first_available(self._email_button_candidates(), "кнопка Email")
                self._click_first_available(self._purchase_order_candidates(), "пункт Purchase order")
                for _ in range(24):
                    opened = self._visible_dialog_or_none()
                    if opened is not None or self.has_send_dialog_open():
                        return opened if opened is not None else self.page.locator("body").first
                    self.page.wait_for_timeout(250)
                last_error = InflowAutomationError("Окно не появилось после выбора Purchase order.")
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                existing = self._visible_dialog_or_none()
                if existing is not None:
                    _write_log("Send modal became visible after click failure, continue.")
                    return existing
                if self.has_send_dialog_open():
                    _write_log("Send modal detected after click failure, continue.")
                    return self.page.locator("body").first
                if attempt == 0:
                    self.page.wait_for_timeout(1_000)
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

    def _first_visible_field(self, candidates: list[Locator], timeout_ms: int = 350) -> Locator | None:
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

    def _click_okay_if_present(self, timeout_ms: int = 5_000) -> bool:
        deadline = time.time() + (timeout_ms / 1000)
        while time.time() < deadline:
            candidates = [
                self.page.get_by_role("button", name=re.compile(r"^(okay|ok)$", re.I)),
                self.page.locator("button:has-text('Okay')"),
                self.page.locator("button:has-text('OK')"),
            ]
            for locator in candidates:
                try:
                    if locator.count() == 0:
                        continue
                    button = locator.first
                    if not button.is_visible():
                        continue
                    button.click()
                    self.page.wait_for_timeout(250)
                    return True
                except Exception:  # noqa: BLE001
                    continue
            self.page.wait_for_timeout(200)
        return False

    def _is_maximum_limit_visible(self) -> bool:
        candidates = [
            self.page.get_by_text(re.compile(r"maximum emails exceeded", re.I)),
            self.page.locator("text=/Maximum emails exceeded/i"),
            self.page.locator("text=/You have reached the limit of emails you can send/i"),
        ]
        for locator in candidates:
            try:
                if locator.count() > 0 and locator.first.is_visible():
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _wait_for_max_limit_notice(self, timeout_ms: int = 8_000) -> bool:
        deadline = time.time() + (timeout_ms / 1000)
        while time.time() < deadline:
            if self._is_maximum_limit_visible():
                return True
            self.page.wait_for_timeout(200)
        return False

    def _click_by_text_patterns(self, text_patterns: list[str], timeout_ms: int = 15_000) -> None:
        deadline = time.time() + (timeout_ms / 1000)
        while time.time() < deadline:
            for pattern in text_patterns:
                compiled = re.compile(pattern, re.I)
                candidates = [
                    self.page.get_by_role("button", name=compiled),
                    self.page.get_by_role("link", name=compiled),
                    self.page.get_by_text(compiled),
                    self.page.locator(f"text=/{pattern}/i"),
                ]
                for locator in candidates:
                    try:
                        if locator.count() == 0:
                            continue
                        target = locator.first
                        if not target.is_visible():
                            continue
                        target.click()
                        self.page.wait_for_timeout(250)
                        return
                    except Exception:  # noqa: BLE001
                        continue
            self.page.wait_for_timeout(220)
        raise InflowAutomationError(f"Не удалось нажать кнопку по шаблонам: {text_patterns}")

    def _fill_first_visible_input(self, value: str, label_patterns: list[str], *, timeout_ms: int = 15_000) -> None:
        deadline = time.time() + (timeout_ms / 1000)
        while time.time() < deadline:
            for label_pattern in label_patterns:
                candidates = [
                    self.page.get_by_label(re.compile(label_pattern, re.I)),
                    self.page.get_by_placeholder(re.compile(label_pattern, re.I)),
                    self.page.locator(f"input[name*='{label_pattern}' i]"),
                    self.page.locator(f"input[id*='{label_pattern}' i]"),
                    self.page.locator(
                        "xpath=//*[contains(translate(normalize-space(text()), "
                        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                        f"'{label_pattern.lower()}')]/following::input[1]"
                    ),
                ]
                field = self._first_visible_field(candidates)
                if field is not None:
                    self._type_into_field(field, value, confirm_with_enter=False)
                    return
            self.page.wait_for_timeout(180)
        raise InflowAutomationError(f"Не удалось найти поле ввода: {label_patterns}")

    def _is_signup_verification_failed(self) -> bool:
        candidates = [
            self.page.get_by_text(re.compile(r"verification failed", re.I)),
            self.page.locator("text=/cloudflare/i"),
            self.page.locator("iframe[src*='turnstile'], iframe[title*='challenge' i]"),
        ]
        for locator in candidates:
            try:
                if locator.count() > 0 and locator.first.is_visible():
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _wait_signup_continue_ready(self, timeout_ms: int = 15_000) -> Locator:
        deadline = time.time() + (timeout_ms / 1000)
        candidates = [
            self.page.get_by_role("button", name=re.compile(r"^continue$", re.I)),
            self.page.locator("button[type='submit']"),
            self.page.locator("button:has-text('Continue')"),
            self.page.locator("input[type='submit']"),
        ]
        while time.time() < deadline:
            if self._is_signup_verification_failed():
                raise InflowAutomationError("Cloudflare verification failed on signup page.")
            for locator in candidates:
                try:
                    if locator.count() == 0:
                        continue
                    button = locator.first
                    if not button.is_visible():
                        continue
                    try:
                        if button.is_disabled():
                            continue
                    except Exception:  # noqa: BLE001
                        pass
                    return button
                except Exception:  # noqa: BLE001
                    continue
            self.page.wait_for_timeout(220)
        raise InflowAutomationError("Кнопка Continue не стала активной на signup странице.")

    def _wait_signup_details_step(self, timeout_ms: int = 45_000) -> None:
        deadline = time.time() + (timeout_ms / 1000)
        while time.time() < deadline:
            if self._is_signup_verification_failed():
                screenshot_path = self.capture_debug_screenshot("cloudflare_verification_failed")
                note = f" | screenshot: {screenshot_path}" if screenshot_path else ""
                raise InflowAutomationError(f"Cloudflare verification failed on signup page.{note}")

            details_candidates = [
                self.page.locator("input[type='password']"),
                self.page.locator("input[type='tel']"),
                self.page.get_by_label(re.compile(r"full\\s*name|first\\s*name|name", re.I)),
                self.page.get_by_placeholder(re.compile(r"full\\s*name|first\\s*name|name", re.I)),
                self.page.locator("input[name*='name' i]"),
                self.page.locator("input[id*='name' i]"),
            ]
            for locator in details_candidates:
                try:
                    if locator.count() == 0:
                        continue
                    if locator.first.is_visible():
                        return
                except Exception:  # noqa: BLE001
                    continue

            self.page.wait_for_timeout(350)

        raise InflowAutomationError("После ввода email не загрузилась форма с Name/Phone/Password.")

    def _fill_signup_name(self, registration_name: str) -> None:
        try:
            self._fill_first_visible_input(
                registration_name,
                [r"full name", r"first name", r"your name", r"name"],
                timeout_ms=15_000,
            )
            return
        except Exception:  # noqa: BLE001
            pass

        # Structural fallback: first visible editable text input that is not email/tel/password.
        text_inputs = self.page.locator("input:not([type='hidden'])")
        for idx in range(text_inputs.count()):
            try:
                field = text_inputs.nth(idx)
                if not field.is_visible():
                    continue
                input_type = (field.get_attribute("type") or "text").strip().lower()
                if input_type in {"email", "tel", "password"}:
                    continue
                if field.get_attribute("readonly") is not None or field.get_attribute("disabled") is not None:
                    continue
                self._type_into_field(field, registration_name, confirm_with_enter=False)
                return
            except Exception:  # noqa: BLE001
                continue
        raise InflowAutomationError("Не удалось заполнить поле Name на signup шаге.")

    def _wait_signup_email_input(self, timeout_ms: int = 20_000) -> Locator:
        deadline = time.time() + (timeout_ms / 1000)
        candidates = [
            self.page.locator("input[type='email']"),
            self.page.locator("input[name*='email' i]"),
            self.page.locator("input[id*='email' i]"),
            self.page.locator("input[autocomplete='email']"),
            self.page.get_by_placeholder(re.compile(r"email", re.I)),
            self.page.get_by_label(re.compile(r"work\\s*email|email", re.I)),
        ]
        while time.time() < deadline:
            for locator in candidates:
                try:
                    if locator.count() == 0:
                        continue
                    field = locator.first
                    if field.is_visible():
                        return field
                except Exception:  # noqa: BLE001
                    continue
            self.page.wait_for_timeout(160)
        raise InflowAutomationError("Не удалось найти поле Work email на странице signup.")

    def _signup_fill_email_and_continue(self, email: str) -> None:
        field = self._wait_signup_email_input(timeout_ms=25_000)
        self._type_into_field(field, email, confirm_with_enter=False)
        # Let client-side validators finish before interacting with Continue.
        self.page.wait_for_timeout(1_200)
        continue_button = self._wait_signup_continue_ready(timeout_ms=18_000)
        _write_log("Signup: page loaded, clicking Continue once.")
        try:
            continue_button.click(timeout=5_000)
        except Exception:  # noqa: BLE001
            # Single fallback only; avoid spam clicks that can trigger CAPTCHA.
            field.press("Enter")
        self.page.wait_for_timeout(900)

    def _wait_url_contains(self, expected_substring: str, timeout_ms: int = 20_000) -> None:
        deadline = time.time() + (timeout_ms / 1000)
        needle = expected_substring.lower()
        while time.time() < deadline:
            if needle in self.page.url.lower():
                return
            self.page.wait_for_timeout(250)
        raise InflowAutomationError(f"Ожидание URL с '{expected_substring}' превысило timeout.")

    def _create_purchase_order(self, *, vendor_required: bool) -> str:
        self._goto_with_retries(
            DEFAULT_INFLOW_URL,
            description="страницу Purchase Orders",
            attempts=4,
            wait_until="domcontentloaded",
        )
        try:
            self.page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:  # noqa: BLE001
            pass

        self._click_by_text_patterns([r"new\s*purchase\s*order"], timeout_ms=18_000)

        if vendor_required:
            self._click_by_text_patterns([r"start with new vendor", r"new vendor"], timeout_ms=12_000)

        random_value = _random_word()
        self._fill_first_visible_input(random_value, [r"vendor", r"name", r"item", r"product"], timeout_ms=10_000)
        self._click_by_text_patterns([r"create"], timeout_ms=12_000)

        self._wait_url_contains("/purchase-orders/", timeout_ms=25_000)
        created_url = self.page.url
        _write_log(f"Purchase order created: {created_url}")
        return created_url

    def _resolve_company_switch_dialogs(self) -> None:
        if self._is_maximum_limit_visible():
            return

        try:
            self._click_by_text_patterns([r"switch to your company"], timeout_ms=7_000)
        except Exception:  # noqa: BLE001
            pass
        for _ in range(2):
            try:
                self._click_by_text_patterns([r"continue"], timeout_ms=6_000)
            except Exception:  # noqa: BLE001
                break

    def register_new_inflow_account(self, registration_name: str, anymessage_client: AnyMessageClient) -> str:
        try:
            _write_log("Шаг 1/8: Покупка новой почты через AnyMessage.")
            mailbox = anymessage_client.buy_gmail()
            password = _random_password()
            phone = _random_uk_phone()
            _write_log(f"Bought gmail via AnyMessage: {mailbox.email}")

            _write_log("Шаг 2/8: Открываю страницу регистрации Inflow.")
            self._goto_with_retries(
                INFLOW_SIGNUP_URL,
                description="страницу регистрации Inflow",
                attempts=5,
                wait_until="commit",
                timeout_ms=35_000,
            )
            # Avoid long hangs on networkidle (tracking/telemetry can keep connections open).
            try:
                self.page.wait_for_load_state("load", timeout=12_000)
            except Exception:  # noqa: BLE001
                pass
            self.maximize_window()

            _write_log("Шаг 3/8: Ввожу рабочую почту и продолжаю.")
            self._signup_fill_email_and_continue(mailbox.email)
            self.maximize_window()
            self._wait_signup_details_step(timeout_ms=50_000)

            _write_log("Шаг 4/8: Заполняю имя, телефон и пароль.")
            self._fill_signup_name(registration_name)
            self._fill_first_visible_input(phone, [r"phone", r"mobile", r"tel"], timeout_ms=20_000)
            self._fill_first_visible_input(password, [r"password"], timeout_ms=15_000)
            self._click_by_text_patterns([r"continue", r"create", r"sign up"], timeout_ms=20_000)

            _write_log("Шаг 5/8: Прохожу onboarding и активирую trial.")
            self._click_by_text_patterns([r"inflow inventory.*start.*trial", r"start.*trial"], timeout_ms=30_000)
            self._click_by_text_patterns([r"continue"], timeout_ms=20_000)
            self._click_by_text_patterns([r"skip this step", r"skip"], timeout_ms=20_000)
            self._click_by_text_patterns([r"start your free trial.*14", r"start.*free.*trial"], timeout_ms=40_000)

            _write_log("Шаг 6/8: Создаю первый Purchase Order.")
            first_purchase_url = self._create_purchase_order(vendor_required=False)

            _write_log("Шаг 7/8: Жду письмо подтверждения и подтверждаю почту.")
            confirm_url = anymessage_client.wait_inflow_confirmation_link(mailbox)
            _write_log(f"Inflow confirmation URL found: {confirm_url}")
            self._goto_with_retries(
                confirm_url,
                description="ссылку подтверждения почты",
                attempts=4,
                wait_until="domcontentloaded",
            )
            try:
                self.page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception:  # noqa: BLE001
                pass

            self._goto_with_retries(
                first_purchase_url,
                description="первый Purchase Order после подтверждения",
                attempts=4,
                wait_until="domcontentloaded",
            )
            try:
                self.page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception:  # noqa: BLE001
                pass
            try:
                self.open_purchase_order_modal()
                self._resolve_company_switch_dialogs()
            except Exception as exc:  # noqa: BLE001
                _write_log(f"Company switch flow was not required or failed softly: {exc}")

            _write_log("Шаг 8/8: Создаю финальный Purchase Order для дальнейшей отправки.")
            second_purchase_url = self._create_purchase_order(vendor_required=True)
            return second_purchase_url
        except Exception as exc:  # noqa: BLE001
            screenshot_path = self.capture_debug_screenshot("registration_flow_error")
            screenshot_note = f" | screenshot: {screenshot_path}" if screenshot_path else ""
            raise InflowAutomationError(f"{_exc_text(exc)}{screenshot_note}") from exc

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
        if cc_email.strip():
            self._fill_recipient_input(dialog, cc_email, r"enter\s*cc\s*email", "cc", fallback_index=1)
        if bcc_email.strip():
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

        if self._wait_for_max_limit_notice(timeout_ms=5_000):
            raise MaximumEmailsExceededError(
                "Maximum emails exceeded: You have reached the limit of emails you can send."
            )

        # After sending, Inflow may show a success popup with "Okay" button.
        # The script must acknowledge it, otherwise next iteration stalls.
        if self._click_okay_if_present(timeout_ms=8_000):
            _write_log("Success popup confirmed with Okay.")

        try:
            dialog.wait_for(state="hidden", timeout=12_000)
        except PlaywrightTimeoutError:
            # Some states keep overlays visible; try one more Okay click.
            if self._click_okay_if_present(timeout_ms=3_000):
                _write_log("Second success popup confirmation performed.")
            try:
                dialog.wait_for(state="hidden", timeout=6_000)
            except PlaywrightTimeoutError as exc:
                raise InflowAutomationError(
                    "Окно отправки не закрылось после Send/Okay."
                ) from exc


def _rotate_profile_on_limit(
    *,
    current_profile: Profile,
    registration_name: str,
    ads_client: AdsApiClient,
    playwright: Any,
    profile_store: ProfileStore | None,
    runtime_settings: RuntimeSettings,
    delete_old_profile: bool = True,
) -> RotationResult:
    if not AUTO_REREGISTER_ON_LIMIT:
        raise InflowAutomationError("AUTO_REREGISTER_ON_LIMIT disabled in config.")

    new_ads_name = f"{registration_name.strip() or current_profile.name.strip() or 'inflow'}-{_random_word(6)}"
    proxy_payload = _resolve_best_proxy(runtime_settings)
    new_profile_id = ads_client.create_profile(new_ads_name, proxy_config=proxy_payload)
    _write_log(f"New ADS profile created: {new_profile_id}")

    anymessage_client = _build_anymessage_client(runtime_settings)
    _write_log(f"Starting ADS browser for new profile: {new_profile_id}")
    new_session = ads_client.start_browser(
        profile_id=new_profile_id,
        headless=ADS_HEADLESS,
        open_tabs=ADS_OPEN_TABS,
    )
    _write_log(f"ADS browser started for new profile: {new_profile_id}; cdp={new_session.cdp_url}")
    registration_done = False
    try:
        _write_log("Connecting Playwright to new ADS browser session.")
        new_browser = playwright.chromium.connect_over_cdp(new_session.cdp_url)
        new_context = new_browser.contexts[0] if new_browser.contexts else new_browser.new_context()
        new_page = new_context.pages[0] if new_context.pages else new_context.new_page()
        new_ui = InflowUi(new_page)
        _write_log("Running new Inflow account registration flow.")
        new_start_url = new_ui.register_new_inflow_account(
            registration_name=registration_name,
            anymessage_client=anymessage_client,
        )
        registration_done = True
    finally:
        if registration_done:
            ads_client.stop_browser(new_session.profile_id)
        else:
            _write_log(
                f"Registration did not finish. Leaving ADS browser open for profile {new_session.profile_id}."
            )

    _write_log(f"New Inflow account prepared with purchase URL: {new_start_url}")

    if delete_old_profile:
        ads_client.delete_profile(current_profile.ads_profile_id)
        _write_log(f"Old ADS profile deleted: {current_profile.ads_profile_id}")

    if profile_store is not None:
        updated = profile_store.update_profile_credentials(
            local_id=current_profile.local_id,
            ads_profile_id=new_profile_id,
            start_url=new_start_url,
        )
        if not updated:
            raise InflowAutomationError("Failed to update profile credentials in local DB.")

    return RotationResult(new_profile_id=new_profile_id, new_start_url=new_start_url)


def run_job(
    profile: Profile,
    registration_name: str | None = None,
    profile_store: ProfileStore | None = None,
    runtime_settings: RuntimeSettings | None = None,
    register_before_send: bool = False,
) -> RunStats:
    if sync_playwright is None:
        raise InflowAutomationError(
            "Модуль playwright не установлен. Выполните: pip install -r requirements.txt"
        )

    ensure_input_files()
    subject, message, emails_all, triplets, already_sent_emails = load_job_data()

    active_settings = _load_runtime_settings(runtime_settings)
    ads_client = _build_ads_client(active_settings)
    sent_store = SentEmailStore()
    sent_emails = 0
    failed_emails = 0
    processable_emails = len(triplets) * 3
    skipped_emails = len(emails_all) - already_sent_emails - processable_emails
    last_error: str | None = None
    current_profile_id = profile.ads_profile_id
    current_start_url = profile.start_url or DEFAULT_INFLOW_URL
    registration_name = (registration_name or profile.name).strip() or "Inflow User"
    limit_rotations = 0

    try:
        _write_log(
            "Job started "
            f"profile={profile.ads_profile_id}; url={profile.start_url}; registration_name={registration_name}; "
            f"emails={len(emails_all)}; processable={processable_emails}; "
            f"already_sent={already_sent_emails}; skipped={skipped_emails}"
        )

        with sync_playwright() as playwright:
            if register_before_send:
                _write_log("Initial registration requested before sending.")
                rotated = _rotate_profile_on_limit(
                    current_profile=Profile(
                        local_id=profile.local_id,
                        name=profile.name,
                        ads_profile_id=current_profile_id,
                        start_url=current_start_url,
                        created_at=profile.created_at,
                    ),
                    registration_name=registration_name,
                    ads_client=ads_client,
                    playwright=playwright,
                    profile_store=profile_store,
                    runtime_settings=active_settings,
                    delete_old_profile=True,
                )
                current_profile_id = rotated.new_profile_id
                current_start_url = rotated.new_start_url
                _write_log(f"Initial registration complete. profile={current_profile_id}, url={current_start_url}")

            index = 0
            while index < len(triplets):
                to_email, cc_email, bcc_email = triplets[index]
                ads_session = ads_client.start_browser(
                    profile_id=current_profile_id,
                    headless=ADS_HEADLESS,
                    open_tabs=ADS_OPEN_TABS,
                )
                session_stopped = False
                try:
                    browser = playwright.chromium.connect_over_cdp(ads_session.cdp_url)
                    context = browser.contexts[0] if browser.contexts else browser.new_context()
                    page = context.pages[0] if context.pages else context.new_page()
                    ui = InflowUi(page)
                    ui.open_page(current_start_url)

                    sent_current = False
                    rotate_requested = False
                    for attempt in range(2):
                        try:
                            ui.send_purchase_order(
                                to_email=to_email,
                                cc_email=cc_email,
                                bcc_email=bcc_email,
                                subject=subject,
                                message=message,
                            )
                            sent_emails += 3
                            sent_current = True
                            _write_log(f"Order sent: TO={to_email}, CC={cc_email}, BCC={bcc_email}")
                            break
                        except MaximumEmailsExceededError as limit_exc:
                            last_error = str(limit_exc)
                            _write_log(
                                "Send blocked by account limit. "
                                f"profile={current_profile_id}; TO={to_email}; attempt={attempt + 1}/2"
                            )
                            rotate_requested = True
                            break
                        except Exception as order_exc:  # noqa: BLE001
                            last_error = str(order_exc)
                            _write_log(
                                "Order attempt failed: "
                                f"attempt={attempt + 1}/2; TO={to_email}, CC={cc_email}, BCC={bcc_email}; "
                                f"error={order_exc}"
                            )
                            if attempt == 1:
                                ui.capture_debug_screenshot("send_order_attempt_failed")
                            try:
                                ui.maximize_window()
                                if not ui.has_send_dialog_open():
                                    ui.wait_until_ready(timeout_ms=4_000)
                            except Exception as heal_exc:  # noqa: BLE001
                                _write_log(f"Recovery wait failed: {heal_exc}")
                            self_heal_delay = 0.7 + attempt * 0.3
                            time.sleep(self_heal_delay)

                    if rotate_requested:
                        if limit_rotations >= AUTO_REREGISTER_TRY_LIMIT:
                            raise InflowAutomationError(
                                f"Reached AUTO_REREGISTER_TRY_LIMIT={AUTO_REREGISTER_TRY_LIMIT}"
                            )
                        limit_rotations += 1
                        ads_client.stop_browser(ads_session.profile_id)
                        session_stopped = True
                        rotated = _rotate_profile_on_limit(
                            current_profile=Profile(
                                local_id=profile.local_id,
                                name=profile.name,
                                ads_profile_id=current_profile_id,
                                start_url=current_start_url,
                                created_at=profile.created_at,
                            ),
                            registration_name=registration_name,
                            ads_client=ads_client,
                            playwright=playwright,
                            profile_store=profile_store,
                            runtime_settings=active_settings,
                        )
                        current_profile_id = rotated.new_profile_id
                        current_start_url = rotated.new_start_url
                        time.sleep(1.0)
                        continue

                    if sent_current:
                        sent_store.mark_many([to_email, cc_email, bcc_email])
                    else:
                        failed_emails += 3
                    index += 1
                    time.sleep(SEND_DELAY_SECONDS)
                finally:
                    if not session_stopped:
                        try:
                            ads_client.stop_browser(ads_session.profile_id)
                        except Exception:  # noqa: BLE001
                            pass
    except AdsApiError:
        raise
    except ProxyApiError as exc:
        last_error = _exc_text(exc)
        raise InflowAutomationError(_exc_text(exc)) from exc
    except AnyMessageApiError as exc:
        last_error = _exc_text(exc)
        raise InflowAutomationError(_exc_text(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        last_error = _exc_text(exc)
        raise InflowAutomationError(_exc_text(exc)) from exc
    finally:
        _write_log(
            f"Job finished: total_emails={len(emails_all)}, total_orders={len(triplets)}, "
            f"sent_emails={sent_emails}, failed_emails={failed_emails}, skipped={skipped_emails}"
        )

    return RunStats(
        total_input_emails=len(emails_all),
        already_sent_emails=already_sent_emails,
        processable_emails=processable_emails,
        sent_emails=sent_emails,
        failed_emails=failed_emails,
        skipped_emails=skipped_emails,
        last_error=last_error,
    )


def register_new_account_for_profile(
    profile: Profile,
    registration_name: str | None = None,
    profile_store: ProfileStore | None = None,
    runtime_settings: RuntimeSettings | None = None,
) -> RotationResult:
    if sync_playwright is None:
        raise InflowAutomationError(
            "Модуль playwright не установлен. Выполните: pip install -r requirements.txt"
        )

    active_settings = _load_runtime_settings(runtime_settings)
    ads_client = _build_ads_client(active_settings)
    registration_name = (registration_name or profile.name).strip() or "Inflow User"

    try:
        with sync_playwright() as playwright:
            return _rotate_profile_on_limit(
                current_profile=profile,
                registration_name=registration_name,
                ads_client=ads_client,
                playwright=playwright,
                profile_store=profile_store,
                runtime_settings=active_settings,
                delete_old_profile=True,
            )
    except (ProxyApiError, AnyMessageApiError) as exc:
        raise InflowAutomationError(_exc_text(exc)) from exc


def run_inbox_check(profile: Profile, target_email: str) -> RunStats:
    if sync_playwright is None:
        raise InflowAutomationError(
            "Модуль playwright не установлен. Выполните: pip install -r requirements.txt"
        )

    target_email = target_email.strip()
    if "@" not in target_email:
        raise InflowAutomationError("Неверный email для проверки инбокса.")

    ensure_input_files()
    subject, message = load_message_data()

    ads_client = _build_ads_client(RuntimeSettingsStore().load())
    ads_session = None
    last_error: str | None = None

    try:
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
            ui.send_purchase_order(
                to_email=target_email,
                cc_email="",
                bcc_email="",
                subject=subject,
                message=message,
            )
    except Exception as exc:  # noqa: BLE001
        last_error = _exc_text(exc)
        raise InflowAutomationError(_exc_text(exc)) from exc
    finally:
        if ads_session is not None:
            ads_client.stop_browser(ads_session.profile_id)

    return RunStats(
        total_input_emails=1,
        already_sent_emails=0,
        processable_emails=1,
        sent_emails=1,
        failed_emails=0,
        skipped_emails=0,
        last_error=last_error,
    )


def _write_log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_file = LOGS_DIR / "automation.log"
    with log_file.open("a", encoding="utf-8") as file:
        file.write(f"[{timestamp}] {message}\n")
    print(f"[{timestamp}] {message}", flush=True)
