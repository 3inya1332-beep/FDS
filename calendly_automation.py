from __future__ import annotations

import json
import random
import re
import secrets
import string
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    from playwright.sync_api import Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright
except ModuleNotFoundError:  # pragma: no cover - runtime dependency check
    Browser = Any  # type: ignore[assignment]
    BrowserContext = Any  # type: ignore[assignment]
    Page = Any  # type: ignore[assignment]
    PlaywrightTimeoutError = TimeoutError
    sync_playwright = None

from ads_api import AdsApiClient
from anymessage_api import AnyMessageApiError, AnyMessageClient
from config import (
    ADS_HEADLESS,
    ADS_OPEN_TABS,
    BODY_FILENAME,
    BOOKING_GUESTS_PER_EVENT,
    BOOKING_NAME_PREFIX,
    CALENDLY_MEETING_TYPES_URL,
    CALENDLY_SIGNUP_URL,
    CAPTCHA_WAIT_SECONDS,
    COOKIE_DIR_CANDIDATES,
    EDIT_DIR_CANDIDATES,
    EMAIL_DIR_CANDIDATES,
    EMAIL_FILENAME,
    LOGS_DIR,
    REGISTRATION_MAX_ATTEMPTS,
    SUBJECT_FILENAME,
)
from profile_store import Profile
from proxy_manager import NineProxyClient, ProxyRotationError


class CalendlyAutomationError(RuntimeError):
    pass


class CaptchaSolveError(CalendlyAutomationError):
    pass


class NoAvailableSlotError(CalendlyAutomationError):
    pass


@dataclass
class RegistrationResult:
    account_name: str
    login_email: str
    password: str
    activation_id: str
    cookie_file: str
    main_page_url: str


@dataclass
class SendStats:
    total_input_emails: int
    scheduled_events: int
    consumed_emails: int
    remaining_emails: int


def _resolve_or_create_dir(candidates: list[Path]) -> Path:
    for folder in candidates:
        if folder.exists():
            return folder
    candidates[0].mkdir(parents=True, exist_ok=True)
    return candidates[0]


def ensure_input_files() -> None:
    edit_dir = _resolve_or_create_dir(EDIT_DIR_CANDIDATES)
    email_dir = _resolve_or_create_dir(EMAIL_DIR_CANDIDATES)
    _resolve_or_create_dir(COOKIE_DIR_CANDIDATES)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    subject_path = edit_dir / SUBJECT_FILENAME
    if not subject_path.exists():
        subject_path.write_text("Confirmed: {{Event Name}} with {{My Name}} on {{Event Date}}", encoding="utf-8")

    body_path = edit_dir / BODY_FILENAME
    if not body_path.exists():
        body_path.write_text(
            "Hi {{Invitee Full Name}},\nYour {{Event Name}} with {{My Name}} at {{Event Time}} on {{Event Date}} is scheduled.",
            encoding="utf-8",
        )

    email_path = email_dir / EMAIL_FILENAME
    if not email_path.exists():
        email_path.write_text("first@example.com\nsecond@example.com\n", encoding="utf-8")


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", value.strip())
    return slug.strip("_") or "account"


def _generate_password(min_len: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits + "@#$%&*-_"
    password = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
        secrets.choice("@#$%&*-_"),
    ]
    password.extend(secrets.choice(alphabet) for _ in range(max(min_len - 4, 8)))
    random.shuffle(password)
    return "".join(password)


def _read_non_empty_lines(path: Path) -> list[str]:
    if not path.exists():
        raise CalendlyAutomationError(f"File not found: {path}")
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line]


def _get_email_file() -> Path:
    email_dir = _resolve_or_create_dir(EMAIL_DIR_CANDIDATES)
    file_path = email_dir / EMAIL_FILENAME
    if file_path.exists():
        return file_path
    txt_files = sorted(email_dir.glob("*.txt"))
    if txt_files:
        return txt_files[0]
    raise CalendlyAutomationError(f"No txt file with emails found in: {email_dir}")


def load_email_pool() -> list[str]:
    file_path = _get_email_file()
    raw = _read_non_empty_lines(file_path)
    unique: list[str] = []
    seen: set[str] = set()
    for email in raw:
        lowered = email.lower()
        if lowered not in seen:
            unique.append(email)
            seen.add(lowered)
    return unique


def save_email_pool(emails: Iterable[str]) -> None:
    file_path = _get_email_file()
    content = "\n".join(emails).strip()
    file_path.write_text(content + ("\n" if content else ""), encoding="utf-8")


def load_templates() -> tuple[str, str]:
    edit_dir = _resolve_or_create_dir(EDIT_DIR_CANDIDATES)
    subject = (edit_dir / SUBJECT_FILENAME).read_text(encoding="utf-8").strip()
    body = (edit_dir / BODY_FILENAME).read_text(encoding="utf-8").strip()
    if not subject:
        raise CalendlyAutomationError(f"{SUBJECT_FILENAME} is empty.")
    if not body:
        raise CalendlyAutomationError(f"{BODY_FILENAME} is empty.")
    return subject, body


def _write_log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_file = LOGS_DIR / "automation.log"
    with log_file.open("a", encoding="utf-8") as file:
        file.write(f"[{timestamp}] {message}\n")


@contextmanager
def open_ads_page(ads_profile_id: str) -> Iterable[tuple[Page, BrowserContext]]:
    if sync_playwright is None:
        raise CalendlyAutomationError("playwright is not installed. Run: pip install -r requirements.txt")

    ads_client = AdsApiClient()
    session = ads_client.start_browser(
        profile_id=ads_profile_id,
        headless=ADS_HEADLESS,
        open_tabs=ADS_OPEN_TABS,
    )
    browser: Browser | None = None
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(session.cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            yield page, context
            browser.close()
    finally:
        ads_client.stop_browser(session.profile_id)


def _click_first(page: Page, selectors: list[str], timeout: int = 5_000) -> bool:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=timeout)
            locator.click()
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _fill_first(page: Page, selectors: list[str], value: str, timeout: int = 5_000) -> bool:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=timeout)
            locator.fill(value)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _wait_for_possible_captcha(page: Page) -> None:
    captcha_selectors = [
        "iframe[src*='captcha']",
        "iframe[src*='recaptcha']",
        "iframe[src*='hcaptcha']",
        "text=/verify.*human/i",
    ]
    detected = False
    for selector in captcha_selectors:
        if page.locator(selector).count() > 0:
            detected = True
            break
    if not detected:
        return

    _write_log("Captcha detected, waiting for auto/manual solve.")
    page.wait_for_timeout(CAPTCHA_WAIT_SECONDS * 1_000)

    for selector in captcha_selectors:
        if page.locator(selector).count() > 0:
            raise CaptchaSolveError("Captcha still present after waiting.")


def _safe_click_next(page: Page) -> bool:
    return _click_first(
        page,
        selectors=[
            "button:has-text('Next')",
            "button:has-text('Continue')",
            "button[type='submit']",
        ],
        timeout=6_000,
    )


def _save_cookies(context: BrowserContext, account_name: str) -> str:
    cookie_dir = _resolve_or_create_dir(COOKIE_DIR_CANDIDATES)
    filename = f"{_slugify(account_name)}_{int(time.time())}.json"
    path = cookie_dir / filename
    context.storage_state(path=str(path))
    return str(path)


def _load_cookies_if_present(context: BrowserContext, cookie_file: str) -> None:
    if not cookie_file:
        return
    path = Path(cookie_file)
    if not path.exists():
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    cookies = payload.get("cookies")
    if isinstance(cookies, list) and cookies:
        context.add_cookies(cookies)


def _click_create_with_password(page: Page) -> None:
    clicked = _click_first(
        page,
        selectors=[
            "a:has-text('Click here')",
            "button:has-text('Click here')",
            "text=Prefer to create an account with a password?",
        ],
        timeout=10_000,
    )
    if not clicked:
        raise CalendlyAutomationError("Could not switch signup flow to password mode.")


def _fill_signup_form(page: Page, account_name: str, password: str, email: str) -> None:
    if not _fill_first(
        page,
        [
            "input[name='email']",
            "input[type='email']",
            "input[autocomplete='email']",
        ],
        email,
        timeout=20_000,
    ):
        raise CalendlyAutomationError("Email input not found on signup page.")

    # On initial signup page email often triggers next state automatically.
    _safe_click_next(page)
    page.wait_for_timeout(1_000)

    _click_create_with_password(page)

    if not _fill_first(
        page,
        [
            "input[name='name']",
            "input[autocomplete='name']",
            "input[placeholder*='full name' i]",
        ],
        account_name,
        timeout=20_000,
    ):
        raise CalendlyAutomationError("Full name input not found.")

    if not _fill_first(
        page,
        [
            "input[name='password']",
            "input[type='password']",
        ],
        password,
        timeout=20_000,
    ):
        raise CalendlyAutomationError("Password input not found.")

    # Mark checkboxes required on signup screen.
    checkbox_count = page.locator("input[type='checkbox']").count()
    for idx in range(checkbox_count):
        checkbox = page.locator("input[type='checkbox']").nth(idx)
        try:
            if not checkbox.is_checked():
                checkbox.check(force=True)
        except Exception:  # noqa: BLE001
            continue

    if not _safe_click_next(page):
        raise CalendlyAutomationError("Continue button not found on signup form.")


def _finish_email_confirmation(page: Page, confirmation_url: str, password: str) -> None:
    page.goto(confirmation_url, wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(1_200)

    # If sign-in page appears, fill password and continue.
    _fill_first(
        page,
        [
            "input[type='password']",
            "input[name='password']",
        ],
        password,
        timeout=7_000,
    )
    _safe_click_next(page)


def _choose_random_option(page: Page, labels: list[str]) -> bool:
    random.shuffle(labels)
    for label in labels:
        button = page.get_by_role("button", name=re.compile(rf"^{re.escape(label)}$", re.I)).first
        try:
            button.wait_for(state="visible", timeout=3_000)
            button.click()
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _configure_weekly_hours(page: Page) -> None:
    # Set full day availability for all visible day rows: 12:00am - 11:00pm
    rows = page.locator("div:has(input[aria-label*='start' i]), div:has(input[value*=':'])")
    row_count = rows.count()
    if row_count == 0:
        # fallback by generic time inputs
        inputs = page.locator("input").all()
        for index, input_node in enumerate(inputs):
            try:
                if index % 2 == 0:
                    input_node.fill("12:00am")
                else:
                    input_node.fill("11:00pm")
            except Exception:  # noqa: BLE001
                continue
        return

    for idx in range(row_count):
        row = rows.nth(idx)
        time_inputs = row.locator("input")
        if time_inputs.count() < 2:
            continue
        try:
            time_inputs.nth(0).fill("12:00am")
            time_inputs.nth(1).fill("11:00pm")
        except Exception:  # noqa: BLE001
            continue


def _complete_onboarding(page: Page) -> None:
    page.wait_for_timeout(2_000)

    _choose_random_option(page, ["On my own", "With my team"])
    _choose_random_option(
        page,
        [
            "Meet with multiple attendees",
            "Schedule meetings",
            "Collect payment",
            "Automate pre/post meeting emails",
            "Record and transcribe meetings",
            "Manage contact records",
        ],
    )
    _safe_click_next(page)
    page.wait_for_timeout(1_200)

    _choose_random_option(
        page,
        ["Finance", "Sales", "Customer success", "Recruiting", "Marketing", "Education", "Consulting", "Other"],
    )
    _safe_click_next(page)
    page.wait_for_timeout(1_200)

    # Calendar connection page: click Next, if redirected to Google auth - go back.
    _safe_click_next(page)
    page.wait_for_timeout(2_000)
    if "accounts.google.com" in page.url:
        page.go_back(wait_until="domcontentloaded")
        page.wait_for_timeout(1_000)
        _safe_click_next(page)

    page.wait_for_timeout(1_500)
    _configure_weekly_hours(page)
    _safe_click_next(page)
    page.wait_for_timeout(1_000)
    _safe_click_next(page)

    try:
        page.wait_for_url("**/app/scheduling/meeting_types/**", timeout=90_000)
    except Exception:  # noqa: BLE001
        page.goto(CALENDLY_MEETING_TYPES_URL, wait_until="domcontentloaded", timeout=90_000)


def register_calendly_account(account_name: str, ads_profile_id: str) -> RegistrationResult:
    ensure_input_files()
    proxy_client = NineProxyClient()
    anymessage = AnyMessageClient()
    last_error: Exception | None = None

    for attempt in range(1, REGISTRATION_MAX_ATTEMPTS + 1):
        password = _generate_password()
        order = anymessage.order_email()
        _write_log(f"Registration attempt={attempt}; email={order.email}; activation_id={order.activation_id}")

        try:
            with open_ads_page(ads_profile_id) as (page, context):
                page.goto(CALENDLY_SIGNUP_URL, wait_until="domcontentloaded", timeout=90_000)
                _fill_signup_form(page, account_name=account_name, password=password, email=order.email)
                _wait_for_possible_captcha(page)

                confirmation_url = anymessage.wait_for_confirmation_link(order.activation_id)
                _finish_email_confirmation(page, confirmation_url, password=password)
                _complete_onboarding(page)

                cookie_file = _save_cookies(context, account_name)
                _write_log(f"Registration successful for {order.email}")
                return RegistrationResult(
                    account_name=account_name,
                    login_email=order.email,
                    password=password,
                    activation_id=order.activation_id,
                    cookie_file=cookie_file,
                    main_page_url=CALENDLY_MEETING_TYPES_URL,
                )

        except (CaptchaSolveError, AnyMessageApiError, ProxyRotationError, PlaywrightTimeoutError) as exc:
            last_error = exc
            _write_log(f"Registration recoverable error on attempt={attempt}: {exc}")
            if attempt < REGISTRATION_MAX_ATTEMPTS:
                proxy_client.rotate()
                time.sleep(2)
                continue
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            _write_log(f"Registration fatal error on attempt={attempt}: {exc}")
            if attempt < REGISTRATION_MAX_ATTEMPTS:
                proxy_client.rotate()
                time.sleep(2)
                continue
            break

    raise CalendlyAutomationError(f"Registration failed after retries: {last_error}")


def configure_account_notifications(profile: Profile) -> str:
    ensure_input_files()
    subject, body = load_templates()

    with open_ads_page(profile.ads_profile_id) as (page, context):
        _load_cookies_if_present(context, profile.cookie_file)
        page.goto(profile.main_page_url or CALENDLY_MEETING_TYPES_URL, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(2_000)

        # Open event type card menu -> Edit.
        _click_first(
            page,
            selectors=[
                "button:has-text('Copy link') + button",
                "button[aria-label*='more' i]",
                "button[aria-haspopup='menu']",
            ],
            timeout=12_000,
        )
        if not _click_first(page, ["text=Edit", "button:has-text('Edit')"], timeout=8_000):
            # If menu interaction fails, try opening by direct first edit button.
            _click_first(page, ["a:has-text('Edit')", "button:has-text('Edit')"], timeout=8_000)

        page.wait_for_timeout(1_500)
        _click_first(page, ["button:has-text('More options')", "text=More options"], timeout=12_000)
        _click_first(
            page,
            [
                "button:has-text('Notifications and workflows')",
                "text=Notifications and workflows",
            ],
            timeout=10_000,
        )

        # Open email confirmation editor.
        _click_first(
            page,
            selectors=[
                "div:has-text('Email confirmation') button[aria-haspopup='menu']",
                "text=Email confirmation",
            ],
            timeout=10_000,
        )
        _click_first(page, ["text=Edit", "button:has-text('Edit')"], timeout=8_000)

        # Subject contenteditable / textarea.
        if not _fill_first(
            page,
            selectors=[
                "textarea[aria-label*='subject' i]",
                "label:has-text('Subject') + div textarea",
                "div[aria-label*='Subject' i][contenteditable='true']",
                "div[role='textbox'][contenteditable='true']",
            ],
            value=subject,
            timeout=10_000,
        ):
            # Rich editor fallback.
            subj_box = page.locator("text=Subject").first.locator("xpath=following::div[@contenteditable='true'][1]")
            subj_box.click()
            page.keyboard.press("Control+A")
            page.keyboard.type(subject)

        if not _fill_first(
            page,
            selectors=[
                "textarea[aria-label*='body' i]",
                "label:has-text('Body') + div textarea",
                "div[aria-label*='Body' i][contenteditable='true']",
            ],
            value=body,
            timeout=10_000,
        ):
            body_box = page.locator("text=Body").first.locator("xpath=following::div[@contenteditable='true'][1]")
            body_box.click()
            page.keyboard.press("Control+A")
            page.keyboard.type(body)

        _click_first(page, ["button:has-text('Save and close')", "button:has-text('Save')"], timeout=10_000)
        _click_first(page, ["button:has-text('Save changes')", "button:has-text('Save')"], timeout=10_000)
        page.wait_for_timeout(1_200)

        cookie_file = _save_cookies(context, profile.name)
        return cookie_file


def _select_first_available_time(page: Page) -> bool:
    # Try already visible times first.
    time_button = page.locator("button", has_text=re.compile(r"^\d{1,2}:\d{2}(am|pm)$", re.I)).first
    if time_button.count() > 0:
        try:
            time_button.click()
            # Some booking pages require second click on "Next"/"Confirm".
            _click_first(page, ["button:has-text('Next')", "button:has-text('Confirm')"], timeout=4_000)
            return True
        except Exception:  # noqa: BLE001
            pass

    # Pick first available date then first time.
    date_buttons = page.locator("button[aria-label*='available' i], button:not([disabled])")
    date_count = date_buttons.count()
    for idx in range(min(date_count, 30)):
        try:
            date_buttons.nth(idx).click()
            page.wait_for_timeout(600)
            time_button = page.locator("button", has_text=re.compile(r"^\d{1,2}:\d{2}(am|pm)$", re.I)).first
            if time_button.count() > 0:
                time_button.click()
                _click_first(page, ["button:has-text('Next')", "button:has-text('Confirm')"], timeout=4_000)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _fill_booking_form(page: Page, invitee_name: str, invitee_email: str, guest_emails: list[str]) -> None:
    if not _fill_first(
        page,
        [
            "input[name='name']",
            "input[aria-label*='Name' i]",
        ],
        invitee_name,
        timeout=20_000,
    ):
        raise CalendlyAutomationError("Invitee Name input not found.")

    if not _fill_first(
        page,
        [
            "input[name='email']",
            "input[type='email']",
            "input[aria-label*='Email' i]",
        ],
        invitee_email,
        timeout=20_000,
    ):
        raise CalendlyAutomationError("Invitee Email input not found.")

    if guest_emails:
        guest_value = ", ".join(guest_emails)
        _fill_first(
            page,
            [
                "textarea[name*='guest' i]",
                "input[name*='guest' i]",
                "textarea[aria-label*='Guest' i]",
            ],
            guest_value,
            timeout=8_000,
        )

    if not _click_first(
        page,
        selectors=[
            "button:has-text('Schedule Event')",
            "button:has-text('Schedule')",
            "button[type='submit']",
        ],
        timeout=10_000,
    ):
        raise CalendlyAutomationError("Schedule button not found.")

    try:
        page.wait_for_selector("text=/scheduled|confirmed|you are scheduled/i", timeout=25_000)
    except Exception as exc:  # noqa: BLE001
        raise CalendlyAutomationError("Booking confirmation was not detected.") from exc


def run_booking_sender(
    profile: Profile,
    *,
    date_page_url: str,
    fallback_booking_url: str | None = None,
) -> SendStats:
    ensure_input_files()
    email_pool = load_email_pool()
    if not email_pool:
        raise CalendlyAutomationError("Email list is empty.")
    total_input = len(email_pool)

    fallback_url = (fallback_booking_url or profile.booking_url or "").strip()
    if not date_page_url and not fallback_url:
        raise CalendlyAutomationError("Need a date page URL or account booking URL.")

    scheduled = 0
    consumed = 0
    primary_url = date_page_url.strip() or fallback_url

    with open_ads_page(profile.ads_profile_id) as (page, context):
        _load_cookies_if_present(context, profile.cookie_file)

        while email_pool:
            target_url = primary_url if scheduled == 0 else (fallback_url or primary_url)
            page.goto(target_url, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(1_200)

            if not _select_first_available_time(page):
                if fallback_url and target_url != fallback_url:
                    page.goto(fallback_url, wait_until="domcontentloaded", timeout=90_000)
                    page.wait_for_timeout(1_200)
                    if not _select_first_available_time(page):
                        raise NoAvailableSlotError("No available slot on date page and fallback booking page.")
                else:
                    raise NoAvailableSlotError("No available slot found.")

            invitee_email = email_pool[0]
            guests_limit = min(BOOKING_GUESTS_PER_EVENT, max(len(email_pool) - 1, 0))
            guest_emails = email_pool[1 : 1 + guests_limit]
            invitee_name = f"{BOOKING_NAME_PREFIX}{random.randint(100, 9999)}"

            _fill_booking_form(
                page,
                invitee_name=invitee_name,
                invitee_email=invitee_email,
                guest_emails=guest_emails,
            )

            used = 1 + len(guest_emails)
            consumed += used
            scheduled += 1
            email_pool = email_pool[used:]
            save_email_pool(email_pool)
            _write_log(
                f"Booking scheduled profile={profile.ads_profile_id}; invitee={invitee_email}; guests={len(guest_emails)}"
            )
            page.wait_for_timeout(1_500)

        cookie_file = _save_cookies(context, profile.name)
        _write_log(f"Updated cookie file after booking: {cookie_file}")

    return SendStats(
        total_input_emails=total_input,
        scheduled_events=scheduled,
        consumed_emails=consumed,
        remaining_emails=len(email_pool),
    )
