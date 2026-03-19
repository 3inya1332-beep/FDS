from __future__ import annotations

import json
import random
import re
import secrets
import string
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    from playwright.sync_api import (
        Browser,
        BrowserContext,
        Locator,
        Page,
        TimeoutError as PlaywrightTimeoutError,
        sync_playwright,
    )
except ModuleNotFoundError:  # pragma: no cover - runtime dependency check
    Browser = Any  # type: ignore[assignment]
    BrowserContext = Any  # type: ignore[assignment]
    Locator = Any  # type: ignore[assignment]
    Page = Any  # type: ignore[assignment]
    PlaywrightTimeoutError = TimeoutError
    sync_playwright = None

from ads_api import AdsApiClient
from anymessage_api import AnyMessageApiError, AnyMessageClient
from config import (
    ADS_HEADLESS,
    ADS_OPEN_TABS,
    ACTION_SPEED_MULTIPLIER,
    ANYMESSAGE_MAX_WAIT_SECONDS,
    ANYMESSAGE_POLL_SECONDS,
    BODY_FILENAME,
    BOOKING_GUESTS_PER_EVENT,
    BOOKING_NAME_PREFIX,
    CALENDLY_MEETING_TYPES_URL,
    CALENDLY_SIGNUP_URL,
    CAPTCHA_MANUAL_TIMEOUT_SECONDS,
    CAPTCHA_MODE,
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


def _progress(message: str) -> None:
    print(f"[INFO] {message}", flush=True)
    _write_log(message)


def _scaled_ms(milliseconds: int) -> int:
    factor = ACTION_SPEED_MULTIPLIER if ACTION_SPEED_MULTIPLIER > 0 else 1.0
    return max(int(milliseconds * factor), 1)


def _pause(page: Page, milliseconds: int) -> None:
    page.wait_for_timeout(_scaled_ms(milliseconds))


def _maximize_ads_window(browser: Browser, page: Page) -> None:
    # Try real browser window maximize first (works on many Chromium+CDP setups).
    try:
        browser_cdp = browser.new_browser_cdp_session()
        info = browser_cdp.send("Browser.getWindowForTarget")
        window_id = info.get("windowId")
        if window_id:
            browser_cdp.send(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"windowState": "maximized"}},
            )
    except Exception:  # noqa: BLE001
        pass

    # Try page-level CDP session as fallback for ADS windows.
    try:
        page_cdp = page.context.new_cdp_session(page)
        info = page_cdp.send("Browser.getWindowForTarget")
        window_id = info.get("windowId")
        if window_id:
            page_cdp.send(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"windowState": "maximized"}},
            )
    except Exception:  # noqa: BLE001
        pass

    # Fallback to large viewport.
    try:
        page.set_viewport_size({"width": 2560, "height": 1440})
    except Exception:  # noqa: BLE001
        pass

    # JS fallback for non-standard window managers.
    try:
        page.evaluate(
            """
            () => {
              try {
                window.moveTo(0, 0);
                window.resizeTo(screen.availWidth, screen.availHeight);
              } catch (e) {}
            }
            """
        )
    except Exception:  # noqa: BLE001
        pass


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
    _progress(f"Starting ADS profile {ads_profile_id}")
    session = ads_client.start_browser(
        profile_id=ads_profile_id,
        headless=ADS_HEADLESS,
        open_tabs=ADS_OPEN_TABS,
    )
    browser: Browser | None = None
    try:
        with sync_playwright() as playwright:
            _progress(f"Connecting to ADS CDP {session.cdp_url}")
            browser = playwright.chromium.connect_over_cdp(session.cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            # Always use a fresh tab for deterministic automation.
            page = context.new_page()
            _maximize_ads_window(browser, page)
            page.set_default_timeout(8_000)
            page.set_default_navigation_timeout(45_000)
            page.bring_to_front()
            try:
                page.goto("about:blank", wait_until="domcontentloaded", timeout=20_000)
            except Exception:  # noqa: BLE001
                pass
            _maximize_ads_window(browser, page)
            yield page, context
            browser.close()
    finally:
        ads_client.stop_browser(session.profile_id)
        _progress(f"ADS profile {ads_profile_id} stopped")


def _click_first(page: Page, selectors: list[str], timeout: int = 3_000) -> bool:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=timeout)
            try:
                locator.click(timeout=timeout)
            except Exception:  # noqa: BLE001
                locator.click(timeout=timeout, force=True)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _fill_first(page: Page, selectors: list[str], value: str, timeout: int = 3_000) -> bool:
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
    recaptcha_selectors = [
        "iframe[src*='google.com/recaptcha']",
        "iframe[src*='recaptcha']",
        "div.g-recaptcha",
        "textarea[name='g-recaptcha-response']",
        "text=/i am not a robot/i",
    ]
    other_captcha_selectors = [
        "iframe[src*='captcha']",
        "iframe[src*='hcaptcha']",
        "iframe[title*='hcaptcha' i]",
        "text=/verify.*human/i",
    ]
    captcha_selectors = recaptcha_selectors + other_captcha_selectors

    def captcha_is_visible() -> bool:
        for selector in captcha_selectors:
            locator = page.locator(selector).first
            try:
                if locator.count() > 0 and locator.is_visible():
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    if not captcha_is_visible():
        return

    is_recaptcha = False
    for selector in recaptcha_selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() > 0 and locator.is_visible():
                is_recaptcha = True
                break
        except Exception:  # noqa: BLE001
            continue

    _progress("reCAPTCHA detected on page." if is_recaptcha else "Captcha detected on page.")
    mode = (CAPTCHA_MODE or "manual").strip().lower()
    if mode == "manual":
        _progress(
            "Manual captcha mode: solve captcha in ADS window, then return to console and press Enter."
        )
        deadline = time.time() + CAPTCHA_MANUAL_TIMEOUT_SECONDS
        while time.time() < deadline:
            remaining = int(deadline - time.time())
            try:
                if sys.stdin.isatty():
                    input(
                        f"[CAPTCHA] Решите капчу в ADS и нажмите Enter (осталось ~{remaining} сек)..."
                    )
                else:
                    # Non-interactive terminal fallback.
                    _pause(page, 4_000)
            except EOFError:
                _pause(page, 4_000)

            # Try to continue flow in case submit is still pending.
            _safe_click_next(page)
            _pause(page, 700)

            if not captcha_is_visible():
                _progress("Captcha solved, continuing registration.")
                return

            # Sometimes page already moved but iframe still exists hidden elsewhere.
            if "/signup" not in page.url.lower():
                _progress("Signup page changed after captcha; continuing.")
                return

            _progress("Captcha still visible. Solve it and press Enter again.")

        raise CaptchaSolveError("Captcha manual timeout exceeded.")

    _progress("Auto captcha wait mode: waiting before re-check.")
    _pause(page, CAPTCHA_WAIT_SECONDS * 1_000)
    if captcha_is_visible():
        raise CaptchaSolveError("Captcha still present after auto wait.")


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
    _pause(page, 400)

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
    _pause(page, 500)

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
        button = page.get_by_role("button", name=re.compile(re.escape(label), re.I)).first
        try:
            button.wait_for(state="visible", timeout=3_000)
            button.click()
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _click_label_option(page: Page, label: str) -> bool:
    selectors = [
        f"button:has-text('{label}')",
        f"[role='button']:has-text('{label}')",
        f"label:has-text('{label}')",
        f"div:has-text('{label}')",
    ]
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=2_500)
            locator.scroll_into_view_if_needed()
            try:
                locator.click(timeout=2_500)
            except Exception:  # noqa: BLE001
                locator.click(timeout=2_500, force=True)
            return True
        except Exception:  # noqa: BLE001
            continue

    # Additional fallback by plain text node.
    text_node = page.get_by_text(re.compile(re.escape(label), re.I)).first
    try:
        text_node.wait_for(state="visible", timeout=2_000)
        text_node.click()
        return True
    except Exception:  # noqa: BLE001
        pass

    # Last fallback: JS click nearest interactive element by text.
    try:
        clicked = page.evaluate(
            """
            (labelText) => {
              const nodes = Array.from(document.querySelectorAll('button, [role="button"], label, div, span'));
              const visible = nodes.filter((el) => {
                const txt = (el.innerText || el.textContent || '').trim();
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return txt.toLowerCase().includes(labelText.toLowerCase())
                  && style.visibility !== 'hidden'
                  && style.display !== 'none'
                  && rect.width > 20
                  && rect.height > 10;
              });
              for (const node of visible) {
                const clickable = node.closest('button, [role="button"], label, a, div');
                if (clickable) {
                  clickable.click();
                  return true;
                }
              }
              return false;
            }
            """,
            label,
        )
        return bool(clicked)
    except Exception:  # noqa: BLE001
        return False


def _choose_random_labels(page: Page, labels: list[str], min_clicks: int = 1, max_clicks: int = 1) -> int:
    shuffled = labels[:]
    random.shuffle(shuffled)
    target_clicks = min(len(shuffled), max(min_clicks, random.randint(min_clicks, max_clicks)))
    clicked = 0
    for label in shuffled:
        if clicked >= target_clicks:
            break
        if _click_label_option(page, label):
            clicked += 1
            _pause(page, 60)
    return clicked


def _click_next_until_progress(page: Page, retries: int = 4) -> bool:
    before = page.url
    for _ in range(retries):
        if _safe_click_next(page):
            _pause(page, 350)
            if page.url != before:
                return True
        else:
            _pause(page, 200)
    return page.url != before


def _configure_weekly_hours(page: Page) -> None:
    # Requirement: enable first+last unavailable day with plus buttons,
    # then set all day ranges to 12:00am - 12:00pm.
    _progress("Applying weekly hours: 12:00am - 12:00pm for all days.")

    plus_candidates = [
        "button[aria-label*='add' i]",
        "button[title*='add' i]",
        "xpath=(//div[normalize-space()='S']/following::button[1])[1]",
        "xpath=(//div[normalize-space()='S']/following::button[1])[last()]",
    ]
    for selector in plus_candidates:
        try:
            locator = page.locator(selector)
            count = locator.count()
            if count == 0:
                continue
            if count == 1:
                locator.first.click(timeout=1_500)
                _pause(page, 80)
            else:
                locator.first.click(timeout=1_500)
                _pause(page, 80)
                locator.last.click(timeout=1_500)
                _pause(page, 80)
        except Exception:  # noqa: BLE001
            continue

    # Fill all visible time inputs in start/end pairs.
    time_inputs = page.locator("input[value*=':'], input[placeholder*=':'], input[aria-label*='time' i]")
    count = time_inputs.count()
    if count < 2:
        return

    def fill_time(input_locator: Locator, value: str) -> None:
        input_locator.click(timeout=1_500)
        page.keyboard.press("Control+A")
        page.keyboard.type(value, delay=0)
        page.keyboard.press("Enter")

    max_inputs = min(count, 14)  # 7 days * 2 columns
    for index in range(max_inputs):
        try:
            if index % 2 == 0:
                fill_time(time_inputs.nth(index), "12:00am")
            else:
                fill_time(time_inputs.nth(index), "12:00pm")
            _pause(page, 40)
        except Exception:  # noqa: BLE001
            continue


def _complete_onboarding(page: Page) -> None:
    _progress("Onboarding flow started.")
    for _ in range(20):
        if "/app/scheduling/meeting_types/" in page.url:
            _progress("Onboarding completed.")
            return

        if "accounts.google.com" in page.url:
            _progress("Google auth page detected, going back.")
            page.go_back(wait_until="domcontentloaded")
            _pause(page, 120)
            _click_next_until_progress(page, retries=2)
            continue

        if page.locator("text=/How do you plan on using Calendly/i").count() > 0 or "/app/intro/team" in page.url:
            _progress("Selecting random options on team/use page.")
            top_clicked = _choose_random_labels(page, ["On my own", "With my team"], min_clicks=1, max_clicks=1)
            help_clicked = _choose_random_labels(
                page,
                [
                    "Meet with multiple attendees",
                    "Schedule meetings",
                    "Collect payment",
                    "Automate pre/post meeting emails",
                    "Record and transcribe meetings",
                    "Manage contact records",
                ],
                min_clicks=1,
                max_clicks=2,
            )
            if top_clicked == 0 or help_clicked == 0:
                raise CalendlyAutomationError("Could not select onboarding options on /app/intro/team")
            _click_next_until_progress(page, retries=5)
            continue

        if page.locator("text=/What is your role\\?/i").count() > 0 or "/app/intro/role" in page.url:
            _progress("Selecting random role.")
            role_clicked = _choose_random_labels(
                page,
                ["Finance", "Sales", "Customer success", "Recruiting", "Marketing", "Education", "Consulting", "Other"],
                min_clicks=1,
                max_clicks=1,
            )
            if role_clicked == 0:
                raise CalendlyAutomationError("Could not select role on onboarding page.")
            _click_next_until_progress(page, retries=5)
            continue

        if (
            page.locator("text=/Set up how your Google calendar will be used/i").count() > 0
            or page.locator("text=/Google calendar/i").count() > 0
            or "/app/intro/calendar" in page.url
        ):
            _progress("Calendar connect step detected, skipping via Next.")
            _click_next_until_progress(page, retries=5)
            continue

        if (
            page.locator("text=/When are you available to meet with people/i").count() > 0
            or page.locator("text=/Weekly hours/i").count() > 0
            or "/app/intro/availability" in page.url
        ):
            _progress("Configuring weekly hours.")
            _configure_weekly_hours(page)
            _click_next_until_progress(page, retries=6)
            continue

        # Generic fallback for other intro steps.
        _safe_click_next(page)
        _pause(page, 120)

    _progress("Onboarding did not auto-finish in time, opening meeting types URL directly.")
    page.goto(CALENDLY_MEETING_TYPES_URL, wait_until="domcontentloaded", timeout=90_000)


def _wait_confirmation_with_captcha_support(
    page: Page,
    anymessage: AnyMessageClient,
    activation_id: str,
) -> str:
    _progress("Waiting for confirmation email link from AnyMessage")
    deadline = time.time() + ANYMESSAGE_MAX_WAIT_SECONDS
    poll_seconds = max(1, int(ANYMESSAGE_POLL_SECONDS * ACTION_SPEED_MULTIPLIER))

    while time.time() < deadline:
        # Keep page open and allow manual captcha solve before polling mail.
        _wait_for_possible_captcha(page)
        _safe_click_next(page)

        try:
            link = anymessage.find_confirmation_link(activation_id)
        except AnyMessageApiError as exc:
            # AnyMessage may return temporary "message not found" before first email arrives.
            _progress(f"AnyMessage pending: {exc}")
            link = None
        if link:
            _progress("Confirmation link received.")
            return link

        remaining = int(deadline - time.time())
        _progress(f"No confirmation email yet. Waiting... (~{remaining}s left)")
        time.sleep(poll_seconds)

    raise AnyMessageApiError(
        f"Confirmation link not found for activation id={activation_id} in {ANYMESSAGE_MAX_WAIT_SECONDS}s."
    )


def register_calendly_account(account_name: str, ads_profile_id: str) -> RegistrationResult:
    ensure_input_files()
    proxy_client = NineProxyClient()
    anymessage = AnyMessageClient()
    last_error: Exception | None = None

    for attempt in range(1, REGISTRATION_MAX_ATTEMPTS + 1):
        _progress(f"Registration attempt {attempt}/{REGISTRATION_MAX_ATTEMPTS}")
        password = _generate_password()
        _progress("Ordering email via AnyMessage API")
        order = anymessage.order_email()
        _progress(f"Got email {order.email} (id={order.activation_id})")

        try:
            with open_ads_page(ads_profile_id) as (page, context):
                _progress(f"Opening signup page: {CALENDLY_SIGNUP_URL}")
                page.goto(CALENDLY_SIGNUP_URL, wait_until="domcontentloaded", timeout=90_000)
                _progress("Filling signup form")
                _fill_signup_form(page, account_name=account_name, password=password, email=order.email)
                _progress("Checking captcha status")
                _wait_for_possible_captcha(page)

                confirmation_url = _wait_confirmation_with_captcha_support(
                    page=page,
                    anymessage=anymessage,
                    activation_id=order.activation_id,
                )
                _progress("Opening confirmation link")
                _finish_email_confirmation(page, confirmation_url, password=password)
                _progress("Completing Calendly onboarding")
                _complete_onboarding(page)

                cookie_file = _save_cookies(context, account_name)
                _progress(f"Registration successful for {order.email}")
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
            _progress(f"Recoverable registration error on attempt {attempt}: {exc}")
            if attempt < REGISTRATION_MAX_ATTEMPTS:
                proxy_client.rotate()
                time.sleep(2)
                continue
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            _progress(f"Fatal registration error on attempt {attempt}: {exc}")
            if attempt < REGISTRATION_MAX_ATTEMPTS:
                proxy_client.rotate()
                time.sleep(2)
                continue
            break

    raise CalendlyAutomationError(f"Registration failed after retries: {last_error}")


def _dismiss_cookie_banner(page: Page) -> None:
    _click_first(
        page,
        [
            "button:has-text('Accept')",
            "button:has-text('Allow all')",
            "button:has-text('Got it')",
            "button[aria-label*='close' i]",
        ],
        timeout=1_500,
    )


def _open_event_editor_panel(page: Page) -> bool:
    _dismiss_cookie_banner(page)
    _pause(page, 120)

    # Fast path: click the first event card title.
    event_title = page.get_by_text(re.compile(r"\d+\s*Minute Meeting|Meeting", re.I)).first
    try:
        event_title.wait_for(state="visible", timeout=4_000)
        event_title.click()
        _pause(page, 250)
        if page.locator("text=/Event type/i").count() > 0 or page.locator("text=/More options/i").count() > 0:
            return True
    except Exception:  # noqa: BLE001
        pass

    # Fallback: hover card and use three dots -> Edit.
    try:
        event_title.hover(timeout=2_000)
    except Exception:  # noqa: BLE001
        pass

    if _click_first(
        page,
        [
            "button:right-of(:text('Copy link'))",
            "button[aria-label*='more' i]",
            "button[aria-haspopup='menu']",
        ],
        timeout=4_000,
    ):
        if _click_first(
            page,
            [
                "role=menuitem[name=/Edit/i]",
                "text=Edit",
                "button:has-text('Edit')",
            ],
            timeout=4_000,
        ):
            _pause(page, 250)
            return True

    return page.locator("text=/Event type/i").count() > 0 or page.locator("text=/More options/i").count() > 0


def _open_email_confirmation_editor(page: Page) -> bool:
    # Enter detailed settings panel.
    _click_first(page, ["button:has-text('More options')", "text=More options"], timeout=4_000)
    _click_first(
        page,
        [
            "button:has-text('Notifications and workflows')",
            "text=Notifications and workflows",
        ],
        timeout=4_000,
    )

    # Try opening row menu for "Email confirmation".
    if _click_first(
        page,
        [
            "text=Email confirmation >> xpath=following::button[1]",
            "div:has-text('Email confirmation') button[aria-haspopup='menu']",
            "text=Email confirmation",
        ],
        timeout=4_000,
    ):
        if _click_first(page, ["role=menuitem[name=/Edit/i]", "text=Edit", "button:has-text('Edit')"], timeout=4_000):
            return True

    # Sometimes clicking the row opens editor directly.
    return page.locator("text=/Subject/i").count() > 0 and page.locator("text=/Body/i").count() > 0


def configure_account_notifications(profile: Profile) -> str:
    ensure_input_files()
    subject, body = load_templates()

    with open_ads_page(profile.ads_profile_id) as (page, context):
        _load_cookies_if_present(context, profile.cookie_file)
        page.goto(profile.main_page_url or CALENDLY_MEETING_TYPES_URL, wait_until="domcontentloaded", timeout=90_000)
        _pause(page, 250)

        if not _open_event_editor_panel(page):
            raise CalendlyAutomationError("Could not open event editor panel (three dots -> Edit).")

        if not _open_email_confirmation_editor(page):
            raise CalendlyAutomationError("Could not open Email confirmation editor.")

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
            timeout=6_000,
        ):
            subj_box = page.locator("text=Subject").first.locator("xpath=following::div[@contenteditable='true'][1]")
            subj_box.click()
            page.keyboard.press("Control+A")
            page.keyboard.type(subject, delay=0)

        if not _fill_first(
            page,
            selectors=[
                "textarea[aria-label*='body' i]",
                "label:has-text('Body') + div textarea",
                "div[aria-label*='Body' i][contenteditable='true']",
            ],
            value=body,
            timeout=6_000,
        ):
            body_box = page.locator("text=Body").first.locator("xpath=following::div[@contenteditable='true'][1]")
            body_box.click()
            page.keyboard.press("Control+A")
            page.keyboard.type(body, delay=0)

        _click_first(page, ["button:has-text('Save and close')", "button:has-text('Save')"], timeout=5_000)
        _click_first(page, ["button:has-text('Save changes')", "button:has-text('Save')"], timeout=5_000)
        _pause(page, 150)

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
            _pause(page, 180)
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
            _pause(page, 500)

            if not _select_first_available_time(page):
                if fallback_url and target_url != fallback_url:
                    page.goto(fallback_url, wait_until="domcontentloaded", timeout=90_000)
                    _pause(page, 500)
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
            _pause(page, 700)

        cookie_file = _save_cookies(context, profile.name)
        _write_log(f"Updated cookie file after booking: {cookie_file}")

    return SendStats(
        total_input_emails=total_input,
        scheduled_events=scheduled,
        consumed_emails=consumed,
        remaining_emails=len(email_pool),
    )
