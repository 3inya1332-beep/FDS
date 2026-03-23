from __future__ import annotations

import json
import random
import re
import secrets
import select
import sqlite3
import string
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

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
    ADS_PROFILE_READY_TIMEOUT_SECONDS,
    ADS_PROFILE_WARMUP_SECONDS,
    ACTION_SPEED_MULTIPLIER,
    ANYMESSAGE_MAX_WAIT_SECONDS,
    ANYMESSAGE_POLL_SECONDS,
    BODY_FILENAME,
    CALENDLY_MEETING_TYPES_URL,
    CALENDLY_SIGNUP_URL,
    CAPTCHA_MANUAL_TIMEOUT_SECONDS,
    CAPTCHA_MODE,
    CAPTCHA_WAIT_SECONDS,
    COOKIE_DIR_CANDIDATES,
    EDIT_DIR_CANDIDATES,
    EMAIL_DIR_CANDIDATES,
    EMAIL_FILENAME,
    KEEP_PROFILE_OPEN_AFTER_REGISTRATION,
    LOGS_DIR,
    PROFILE_DB_PATH,
    REGISTRATION_MAX_ATTEMPTS,
    SCHEDULE_CONFIRM_EXTRA_WAIT_SECONDS,
    SEND_BETWEEN_BOOKINGS_SECONDS,
    SENDER_CAPTCHA_DESKTOP_OS,
    SENDER_CAPTCHA_DESKTOP_SCREEN,
    SENDER_CAPTCHA_DESKTOP_UA,
    SENDER_CAPTCHA_PROFILE_NAME_PREFIX,
    SENDER_CAPTCHA_PROXY_HOST,
    SENDER_CAPTCHA_PROXY_PASSWORD,
    SENDER_CAPTCHA_PROXY_PING_TEST_URL,
    SENDER_CAPTCHA_PROXY_PING_TIMEOUT_SECONDS,
    SENDER_CAPTCHA_PROXY_ACCEPTABLE_PING_MS,
    SENDER_CAPTCHA_PROXY_PORT,
    SENDER_CAPTCHA_PROXY_ROTATE_ATTEMPTS,
    SENDER_CAPTCHA_PROXY_ROTATE_TIMEOUT_SECONDS,
    SENDER_CAPTCHA_PROXY_ROTATE_URL,
    SENDER_CAPTCHA_PROXY_TYPE,
    SENDER_CAPTCHA_PROXY_USER,
    SENT_EMAILS_DIR,
    SENT_EMAILS_FILENAME,
    SIGNUP_INITIAL_DELAY_SECONDS,
    SIGNUP_LOAD_CHECK_TIMEOUT_SECONDS,
    SIGNUP_LOAD_MAX_RELOADS,
    SIGNUP_PASSWORD_SWITCH_TIMEOUT_SECONDS,
    SIGNUP_POST_EMAIL_WAIT_SECONDS,
    SIGNUP_WAIT_FOREVER_IF_NOT_READY,
    SUBJECT_FILENAME,
)
from profile_store import Profile
from proxy_manager import NineProxyClient, ProxyRotationError


class CalendlyAutomationError(RuntimeError):
    pass


class CaptchaSolveError(CalendlyAutomationError):
    pass


class SenderCaptchaDetected(CalendlyAutomationError):
    pass


class NoAvailableSlotError(CalendlyAutomationError):
    pass


class ConfirmationPasswordStepError(CalendlyAutomationError):
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


@dataclass
class ManualSessionResult:
    cookie_file: str
    last_url: str


@dataclass
class SenderRuntimeControls:
    enabled: bool
    paused: bool = False


def _progress(message: str) -> None:
    print(f"[INFO] {message}", flush=True)
    _write_log(message)


def _scaled_ms(milliseconds: int) -> int:
    factor = ACTION_SPEED_MULTIPLIER if ACTION_SPEED_MULTIPLIER > 0 else 1.0
    return max(int(milliseconds * factor), 1)


def _poll_sender_runtime_controls(state: SenderRuntimeControls) -> None:
    if not state.enabled:
        return
    try:
        ready, _, _ = select.select([sys.stdin], [], [], 0)
    except Exception:  # noqa: BLE001
        return
    if not ready:
        return
    try:
        command = sys.stdin.readline().strip().lower()
    except Exception:  # noqa: BLE001
        return
    if command in {"p", "pause"}:
        state.paused = True
        _progress("Sender paused. Type 'r' + Enter to resume.")
    elif command in {"r", "resume"}:
        state.paused = False
        _progress("Sender resumed.")


def _sender_runtime_checkpoint(state: SenderRuntimeControls) -> None:
    _poll_sender_runtime_controls(state)
    while state.paused:
        time.sleep(0.15)
        _poll_sender_runtime_controls(state)


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


def _wait_for_ads_profile_ready(context: BrowserContext, page: Page) -> None:
    _progress("Waiting ADS profile to fully load.")
    deadline = time.time() + ADS_PROFILE_READY_TIMEOUT_SECONDS
    last_error: Exception | None = None

    while time.time() < deadline:
        try:
            if len(context.pages) == 0:
                _pause(page, 120)
                continue

            # Health checks for CDP/page lifecycle.
            page.wait_for_load_state("domcontentloaded", timeout=2_000)
            ready_state = page.evaluate("document.readyState")
            ua = page.evaluate("navigator.userAgent")
            if ready_state in {"interactive", "complete"} and isinstance(ua, str) and ua:
                # Small warmup for profile scripts/extensions/cookies.
                time.sleep(max(0, ADS_PROFILE_WARMUP_SECONDS))
                _progress("ADS profile ready.")
                return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            _pause(page, 120)

    raise CalendlyAutomationError(f"ADS profile did not become ready in time: {last_error}")


def _safe_goto_calendly(page: Page, url: str, *, timeout_ms: int = 45_000) -> None:
    """
    Navigate to Calendly URL with tolerant fallback for transient proxy/socket failures.
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        return
    except Exception as first_exc:  # noqa: BLE001
        message = str(first_exc)
        _progress(f"Navigation warning for {url}: {message}")

    # Faster fallback, less strict load state.
    try:
        page.goto(url, wait_until="commit", timeout=max(15_000, timeout_ms // 2))
        return
    except Exception as second_exc:  # noqa: BLE001
        message = str(second_exc)
        _progress(f"Navigation fallback warning for {url}: {message}")
        # If we're already on Calendly domain, continue flow instead of hard fail.
        if "calendly.com" in page.url:
            _progress(f"Continuing on current Calendly page: {page.url}")
            return
        raise CalendlyAutomationError(f"Failed to open page {url}: {second_exc}") from second_exc


def _extract_date_from_booking_url(url: str) -> str | None:
    match = re.search(r"[?&]date=(\d{4}-\d{2}-\d{2})", url)
    if not match:
        return None
    return match.group(1)


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
    SENT_EMAILS_DIR.mkdir(parents=True, exist_ok=True)

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

    sent_emails_file = SENT_EMAILS_DIR / SENT_EMAILS_FILENAME
    if not sent_emails_file.exists():
        sent_emails_file.write_text("", encoding="utf-8")


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


_REAL_FIRST_NAMES = [
    "Liam",
    "Noah",
    "Oliver",
    "Elijah",
    "James",
    "William",
    "Benjamin",
    "Lucas",
    "Henry",
    "Theodore",
    "Jack",
    "Levi",
    "Alexander",
    "Jackson",
    "Mateo",
    "Daniel",
    "Michael",
    "Mason",
    "Sebastian",
    "Ethan",
    "Aiden",
    "Logan",
    "Jacob",
    "Samuel",
    "David",
    "Joseph",
    "John",
    "Owen",
    "Wyatt",
    "Matthew",
    "Luke",
    "Asher",
    "Carter",
    "Julian",
    "Grayson",
    "Leo",
    "Jayden",
    "Gabriel",
    "Isaac",
    "Lincoln",
    "Anthony",
    "Hudson",
    "Dylan",
    "Ezra",
    "Thomas",
    "Charles",
    "Christopher",
    "Jaxon",
    "Maverick",
    "Josiah",
]

_REAL_LAST_NAMES = [
    "Smith",
    "Johnson",
    "Williams",
    "Brown",
    "Jones",
    "Garcia",
    "Miller",
    "Davis",
    "Rodriguez",
    "Martinez",
    "Hernandez",
    "Lopez",
    "Gonzalez",
    "Wilson",
    "Anderson",
    "Thomas",
    "Taylor",
    "Moore",
    "Jackson",
    "Martin",
    "Lee",
    "Perez",
    "Thompson",
    "White",
    "Harris",
    "Sanchez",
    "Clark",
    "Ramirez",
    "Lewis",
    "Robinson",
    "Walker",
    "Young",
    "Allen",
    "King",
    "Wright",
    "Scott",
    "Torres",
    "Nguyen",
    "Hill",
    "Flores",
    "Green",
    "Adams",
    "Nelson",
    "Baker",
    "Hall",
    "Rivera",
    "Campbell",
    "Mitchell",
    "Carter",
    "Roberts",
]


def _generate_real_invitee_name(used_names: set[str] | None = None) -> str:
    for _ in range(30):
        candidate = f"{random.choice(_REAL_FIRST_NAMES)} {random.choice(_REAL_LAST_NAMES)}"
        if used_names is None or candidate not in used_names:
            if used_names is not None:
                used_names.add(candidate)
            return candidate
    # Rare fallback when many names already used in one run.
    candidate = f"{random.choice(_REAL_FIRST_NAMES)} {random.choice(_REAL_LAST_NAMES)} {random.randint(10, 99)}"
    if used_names is not None:
        used_names.add(candidate)
    return candidate


def _fill_fast_input(target: Locator, value: str) -> bool:
    try:
        target.click(timeout=1_500)
        target.press("Control+A")
        target.press("Delete")
        target.fill(value)
        return True
    except Exception:  # noqa: BLE001
        try:
            target.fill(value)
            return True
        except Exception:  # noqa: BLE001
            return False


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


def load_unsent_email_pool() -> list[str]:
    return _filter_unsent_emails(load_email_pool())


def mark_emails_as_sent(emails: Iterable[str]) -> None:
    _append_sent_emails_file(emails)
    _save_sent_emails_db(emails)


def remove_emails_from_pool(emails_to_remove: Iterable[str]) -> None:
    removal = {email.strip().lower() for email in emails_to_remove if email.strip()}
    if not removal:
        return
    current = load_email_pool()
    remaining = [email for email in current if email.strip().lower() not in removal]
    save_email_pool(remaining)


def save_email_pool(emails: Iterable[str]) -> None:
    file_path = _get_email_file()
    content = "\n".join(emails).strip()
    file_path.write_text(content + ("\n" if content else ""), encoding="utf-8")


def _sent_emails_file_path() -> Path:
    SENT_EMAILS_DIR.mkdir(parents=True, exist_ok=True)
    return SENT_EMAILS_DIR / SENT_EMAILS_FILENAME


def _load_sent_emails_file() -> set[str]:
    path = _sent_emails_file_path()
    if not path.exists():
        return set()
    lines = [line.strip().lower() for line in path.read_text(encoding="utf-8").splitlines()]
    return {line for line in lines if line}


def _append_sent_emails_file(emails: Iterable[str]) -> None:
    existing = _load_sent_emails_file()
    new_items = []
    for email in emails:
        lowered = email.strip().lower()
        if lowered and lowered not in existing:
            new_items.append(lowered)
            existing.add(lowered)
    if not new_items:
        return
    path = _sent_emails_file_path()
    with path.open("a", encoding="utf-8") as handle:
        for email in new_items:
            handle.write(email + "\n")


def _init_sent_emails_db() -> None:
    with sqlite3.connect(PROFILE_DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_emails (
                email TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def _load_sent_emails_db() -> set[str]:
    _init_sent_emails_db()
    with sqlite3.connect(PROFILE_DB_PATH) as conn:
        rows = conn.execute("SELECT email FROM sent_emails").fetchall()
    return {str(row[0]).strip().lower() for row in rows if row and row[0]}


def _save_sent_emails_db(emails: Iterable[str]) -> None:
    _init_sent_emails_db()
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    normalized = []
    for email in emails:
        lowered = email.strip().lower()
        if lowered:
            normalized.append((lowered, ts))
    if not normalized:
        return
    with sqlite3.connect(PROFILE_DB_PATH) as conn:
        conn.executemany("INSERT OR IGNORE INTO sent_emails(email, created_at) VALUES (?, ?)", normalized)
        conn.commit()


def _filter_unsent_emails(emails: Iterable[str]) -> list[str]:
    sent = _load_sent_emails_file().union(_load_sent_emails_db())
    result: list[str] = []
    for email in emails:
        lowered = email.strip().lower()
        if lowered and lowered not in sent:
            result.append(email)
    return result


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
def open_ads_page(ads_profile_id: str, *, stop_profile_on_exit: bool = True) -> Iterable[tuple[Page, BrowserContext]]:
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
            _maximize_ads_window(browser, page)
            _wait_for_ads_profile_ready(context, page)
            yield page, context
    finally:
        if stop_profile_on_exit:
            ads_client.stop_browser(session.profile_id)
            _progress(f"ADS profile {ads_profile_id} stopped")
        else:
            _progress(f"ADS profile {ads_profile_id} left open by configuration.")


def _click_first(page: Page, selectors: list[str], timeout: int = 1_500) -> bool:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() == 0:
                continue
            locator.wait_for(state="visible", timeout=timeout)
            try:
                locator.click(timeout=timeout)
            except Exception:  # noqa: BLE001
                locator.click(timeout=timeout, force=True)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _fill_first(page: Page, selectors: list[str], value: str, timeout: int = 1_500) -> bool:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() == 0:
                continue
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


def _sender_captcha_visible(page: Page) -> bool:
    selectors = [
        "iframe[src*='google.com/recaptcha']",
        "iframe[src*='recaptcha']",
        "iframe[src*='hcaptcha']",
        "iframe[title*='captcha' i]",
        "div.g-recaptcha",
        "textarea[name='g-recaptcha-response']",
        "text=/i am not a robot/i",
        "text=/verify you are human/i",
        "text=/confirm you're human/i",
        "text=/security check/i",
        "text=/captcha/i",
    ]
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() > 0 and locator.is_visible():
                return True
        except Exception:  # noqa: BLE001
            continue

    current_url = (page.url or "").lower()
    if "captcha" in current_url and "calendly.com" in current_url:
        return True
    return False


def _click_human_continue_modal(page: Page) -> bool:
    # Prioritize modal/button text from screenshot: "Confirm you're human" + "Continue"
    selectors = [
        "button:has-text('Continue')",
        "[role='dialog'] button:has-text('Continue')",
        "div[role='dialog'] button:has-text('Continue')",
        "text=/confirm you'?re human/i >> .. >> button:has-text('Continue')",
    ]
    if _click_first(page, selectors=selectors, timeout=250):
        return True
    try:
        clicked = page.evaluate(
            """
            () => {
              const dialogs = Array.from(document.querySelectorAll('[role="dialog"], div'));
              for (const root of dialogs) {
                const txt = (root.innerText || '').toLowerCase();
                if (!txt.includes("confirm you're human") && !txt.includes("confirm you’re human")) continue;
                const btn = root.querySelector('button');
                if (!btn) continue;
                const btxt = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                if (!btxt.includes('continue')) continue;
                btn.click();
                return true;
              }
              return false;
            }
            """
        )
        return bool(clicked)
    except Exception:  # noqa: BLE001
        return False


def _try_instant_sender_captcha_click(page: Page) -> bool:
    """
    Best-effort instant click on captcha checkbox/challenge entry point.
    This is intentionally fast and non-blocking.
    """
    # Fast path: JS click inside captcha-related frames with zero waiting.
    for frame in page.frames:
        frame_url = (frame.url or "").lower()
        if "captcha" not in frame_url and "recaptcha" not in frame_url and "hcaptcha" not in frame_url:
            continue
        try:
            clicked = frame.evaluate(
                """
                () => {
                  const nodes = [
                    document.querySelector('#recaptcha-anchor'),
                    document.querySelector('.recaptcha-checkbox-border'),
                    document.querySelector('div[role="checkbox"]'),
                    document.querySelector('input[type="checkbox"]'),
                    document.querySelector('#checkbox'),
                  ].filter(Boolean);
                  if (!nodes.length) return false;
                  const target = nodes[0];
                  target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                  target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                  target.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                  return true;
                }
                """
            )
            if bool(clicked):
                return True
        except Exception:  # noqa: BLE001
            continue

    # Fallback: immediate mouse click at iframe center.
    for selector in [
        "iframe[src*='recaptcha']",
        "iframe[src*='hcaptcha']",
        "iframe[title*='captcha' i]",
    ]:
        try:
            frame_box = page.locator(selector).first.bounding_box()
            if frame_box:
                page.mouse.click(
                    frame_box["x"] + frame_box["width"] / 2,
                    frame_box["y"] + frame_box["height"] / 2,
                )
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _parse_screen_resolution(value: str) -> tuple[int, int]:
    raw = (value or "").strip().lower().replace("*", "x")
    match = re.match(r"^\s*(\d{3,5})\s*x\s*(\d{3,5})\s*$", raw)
    if not match:
        return 1920, 1080
    width = max(1280, int(match.group(1)))
    height = max(720, int(match.group(2)))
    return width, height


def _measure_sender_proxy_latency_ms(proxy_port: int) -> float | None:
    proxy_uri = f"socks5h://{SENDER_CAPTCHA_PROXY_HOST}:{int(proxy_port)}"
    start = time.perf_counter()
    try:
        response = requests.get(
            SENDER_CAPTCHA_PROXY_PING_TEST_URL,
            proxies={"http": proxy_uri, "https": proxy_uri},
            timeout=max(1, int(SENDER_CAPTCHA_PROXY_PING_TIMEOUT_SECONDS)),
            allow_redirects=False,
        )
        if response.status_code >= 500:
            return None
        return (time.perf_counter() - start) * 1000.0
    except Exception:
        return None


def _rotate_sender_proxy_for_recovery(proxy_port: int) -> None:
    url = (SENDER_CAPTCHA_PROXY_ROTATE_URL or "").strip()
    if not url:
        return
    rotate_url = url
    if "port=" not in rotate_url.lower():
        separator = "&" if "?" in rotate_url else "?"
        rotate_url = f"{rotate_url}{separator}port={int(proxy_port)}"
    _progress(
        f"Sender captcha recovery: rotating proxy via API (same profile port {int(proxy_port)})."
    )
    attempts = max(1, int(SENDER_CAPTCHA_PROXY_ROTATE_ATTEMPTS))
    acceptable_ms = max(150, int(SENDER_CAPTCHA_PROXY_ACCEPTABLE_PING_MS))
    best_ping: float | None = None

    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(rotate_url, timeout=max(1, int(SENDER_CAPTCHA_PROXY_ROTATE_TIMEOUT_SECONDS)))
            payload = (response.text or "").strip()
            if response.ok:
                _progress(f"Proxy rotate API response ({attempt}/{attempts}): {payload[:160]}")
            else:
                _progress(f"Proxy rotate API HTTP {response.status_code} ({attempt}/{attempts}): {payload[:160]}")
        except Exception as exc:  # noqa: BLE001
            _progress(f"Proxy rotate API warning ({attempt}/{attempts}): {exc}")
            continue

        latency_ms = _measure_sender_proxy_latency_ms(proxy_port)
        if latency_ms is None:
            _progress("Proxy latency test failed; using this rotated proxy without extra rotations.")
            return

        if best_ping is None or latency_ms < best_ping:
            best_ping = latency_ms
        _progress(f"Proxy latency after rotate ({attempt}/{attempts}): {latency_ms:.0f}ms")
        if latency_ms <= acceptable_ms:
            _progress(f"Proxy accepted by ping threshold: {latency_ms:.0f}ms <= {acceptable_ms}ms")
            return
        if attempt == attempts:
            _progress("Proxy ping is higher than threshold, but max rotate attempts reached.")
            return

    if best_ping is not None:
        _progress(f"Using best observed proxy latency after rotations: {best_ping:.0f}ms")
    else:
        _progress("Could not verify proxy latency; continuing with latest rotated proxy.")


def _raise_sender_captcha(page: Page, stage: str) -> None:
    if not _sender_captcha_visible(page):
        return

    # User-requested behavior: instantly click "Continue" on human-check modal.
    if _click_human_continue_modal(page):
        _progress(f"Human-check modal detected ({stage}), Continue clicked instantly.")
        try:
            page.wait_for_timeout(20)
        except Exception:  # noqa: BLE001
            pass
        return

    clicked = _try_instant_sender_captcha_click(page)
    if clicked:
        _progress(f"Sender captcha detected ({stage}), instant one-click sent.")
        try:
            page.wait_for_timeout(25)
        except Exception:  # noqa: BLE001
            pass
        if not _sender_captcha_visible(page):
            _progress("Captcha disappeared after instant click. Continuing sender.")
            return

    raise SenderCaptchaDetected(f"Captcha detected during sender ({stage}).")


def _safe_click_next(page: Page) -> bool:
    return _click_first(
        page,
        selectors=[
            "button:has-text('Next')",
            "button:has-text('Continue')",
            "button[type='submit']",
        ],
        timeout=1_200,
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


def _click_create_with_password(page: Page) -> bool:
    # Preferred path: exact link/button with "Click here".
    if _click_first(
        page,
        selectors=[
            "role=link[name=/^click here$/i]",
            "a:has-text('Click here')",
            "button:has-text('Click here')",
        ],
        timeout=1_200,
    ):
        _pause(page, 120)
        return True

    # JS fallback: click exact "click here" text and nearest interactive parent.
    try:
        clicked = page.evaluate(
            """
            () => {
              const isVisible = (el) => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 5 && r.height > 5 && s.display !== 'none' && s.visibility !== 'hidden';
              };
              const nodes = Array.from(document.querySelectorAll('a, button, span, div'));
              for (const node of nodes) {
                const txt = (node.textContent || '').trim().toLowerCase();
                if (txt !== 'click here') continue;
                if (!isVisible(node)) continue;
                const clickable = node.closest('a,button,[role="button"]') || node;
                clickable.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                clickable.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                clickable.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                return true;
              }
              return false;
            }
            """
        )
        if clicked:
            _pause(page, 120)
            return True
    except Exception:  # noqa: BLE001
        pass

    return False


def _in_password_signup_mode(page: Page) -> bool:
    return (
        page.locator("input[name='password']").count() > 0
        or page.locator("input[type='password']").count() > 0
        or page.locator("text=/Choose a password/i").count() > 0
    )


def _wait_for_password_mode(page: Page, timeout_seconds: int) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if _in_password_signup_mode(page):
            return True
        _pause(page, 120)
    return _in_password_signup_mode(page)


def _signup_page_has_interactive_controls(page: Page) -> bool:
    selectors = [
        "input[name='email']",
        "input[type='email']",
        "button:has-text('Continue')",
        "button:has-text('Sign up with Google')",
        "text=Continue with Google",
        "a:has-text('Click here')",
        "button:has-text('Click here')",
        "iframe[src*='recaptcha']",
        "iframe[src*='hcaptcha']",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible():
                return True
        except Exception:  # noqa: BLE001
            continue
    return _in_password_signup_mode(page)


def _wait_signup_state_after_email(page: Page, timeout_seconds: int) -> None:
    """
    After clicking continue on email step, Calendly can show loading skeleton for a while.
    Wait for any actionable signup state before moving on.
    """
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if _signup_page_has_interactive_controls(page):
            return
        _pause(page, 120)


def _open_signup_page_with_recovery(page: Page) -> None:
    _progress(f"Opening signup page: {CALENDLY_SIGNUP_URL}")
    _safe_goto_calendly(page, CALENDLY_SIGNUP_URL, timeout_ms=45_000)
    start = time.time()
    while True:
        if _signup_page_has_interactive_controls(page):
            _progress("Signup page ready.")
            return
        waited = int(time.time() - start)
        if not SIGNUP_WAIT_FOREVER_IF_NOT_READY and waited >= SIGNUP_LOAD_CHECK_TIMEOUT_SECONDS:
            raise CalendlyAutomationError("Signup page did not become interactive in time.")
        if waited > 0 and waited % 10 == 0:
            _progress(f"Signup still loading... waited {waited}s")
        _pause(page, 80)


def _fill_signup_form(page: Page, account_name: str, password: str, email: str) -> None:
    _progress("Signup: switching to password flow.")
    if not _in_password_signup_mode(page):
        # Case 1: initial page with email input.
        email_filled = _fill_first(
            page,
            [
                "input[name='email']",
                "input[type='email']",
                "input[autocomplete='email']",
            ],
            email,
            timeout=4_000,
        )
        if email_filled:
            _safe_click_next(page)
            _wait_signup_state_after_email(page, SIGNUP_POST_EMAIL_WAIT_SECONDS)
        # Case 2: email already pre-filled page (as in your screenshot) - go directly via Click here.
        deadline = time.time() + SIGNUP_PASSWORD_SWITCH_TIMEOUT_SECONDS
        while not _in_password_signup_mode(page) and time.time() < deadline:
            _click_create_with_password(page)
            if _wait_for_password_mode(page, 1):
                break
            _pause(page, 200)
        if not _in_password_signup_mode(page):
            _progress("Auto click on 'Click here' failed. Manual fallback: click it in browser, then press Enter.")
            try:
                if sys.stdin.isatty():
                    input("[SIGNUP] Нажмите 'Click here' вручную и нажмите Enter...")
            except EOFError:
                pass
            if not _wait_for_password_mode(page, 20):
                raise CalendlyAutomationError("Could not switch signup flow to password mode.")

    if not _fill_first(
        page,
        [
            "input[name='name']",
            "input[autocomplete='name']",
            "input[placeholder*='full name' i]",
        ],
        account_name,
        timeout=8_000,
    ):
        raise CalendlyAutomationError("Full name input not found.")

    if not _fill_first(
        page,
        [
            "input[name='password']",
            "input[type='password']",
        ],
        password,
        timeout=8_000,
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
    _safe_goto_calendly(page, confirmation_url, timeout_ms=90_000)
    _pause(page, 500)

    password_selectors = [
        "input[type='password']",
        "input[name='password']",
        "input[autocomplete='current-password']",
        "input[autocomplete='new-password']",
    ]

    # Explicitly require password input after email confirmation.
    password_filled = _fill_first(
        page,
        password_selectors,
        password,
        timeout=10_000,
    )
    if not password_filled:
        # JS fallback for stubborn password screens.
        try:
            password_filled = bool(
                page.evaluate(
                    """
                    (value) => {
                      const selectors = [
                        "input[type='password']",
                        "input[name='password']",
                        "input[autocomplete='current-password']",
                        "input[autocomplete='new-password']",
                      ];
                      for (const selector of selectors) {
                        const el = document.querySelector(selector);
                        if (!el) continue;
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        if (rect.width < 20 || rect.height < 10) continue;
                        if (style.display === "none" || style.visibility === "hidden") continue;
                        el.focus();
                        el.value = value;
                        el.dispatchEvent(new Event("input", { bubbles: true }));
                        el.dispatchEvent(new Event("change", { bubbles: true }));
                        return true;
                      }
                      return false;
                    }
                    """,
                    password,
                )
            )
        except Exception:  # noqa: BLE001
            password_filled = False

    if not password_filled:
        # Retry hard without user interaction: click field and type password.
        for _ in range(6):
            for selector in password_selectors:
                field = page.locator(selector).first
                try:
                    if field.count() == 0 or not field.is_visible():
                        continue
                    field.click(timeout=1_200)
                    field.press("Control+A")
                    field.type(password, delay=0)
                    field.press("Tab")
                    _pause(page, 40)
                    password_filled = True
                    break
                except Exception:  # noqa: BLE001
                    continue
            if password_filled:
                break

            # One more JS set on each retry.
            try:
                password_filled = bool(
                    page.evaluate(
                        """
                        (value) => {
                          const el = document.querySelector(
                            "input[type='password'], input[name='password'], input[autocomplete='current-password']"
                          );
                          if (!el) return false;
                          el.focus();
                          el.value = value;
                          el.dispatchEvent(new Event("input", { bubbles: true }));
                          el.dispatchEvent(new Event("change", { bubbles: true }));
                          return true;
                        }
                        """,
                        password,
                    )
                )
            except Exception:  # noqa: BLE001
                password_filled = False
            if password_filled:
                break
            _pause(page, 50)
    if not password_filled:
        raise ConfirmationPasswordStepError("Password input not found after email confirmation.")

    # Ensure typed value exists before continue.
    try:
        has_value = bool(
            page.evaluate(
                """
                () => {
                  const el = document.querySelector("input[type='password'], input[name='password']");
                  return !!(el && (el.value || "").length >= 6);
                }
                """
            )
        )
    except Exception:  # noqa: BLE001
        has_value = True
    if not has_value:
        raise ConfirmationPasswordStepError("Password value did not persist on confirmation step.")

    if not _safe_click_next(page):
        raise ConfirmationPasswordStepError("Continue button not found after confirmation password input.")
    _pause(page, 80)


def _next_dated_booking_url(current_url: str) -> str | None:
    try:
        parsed = urlparse(current_url)
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        current_date_raw = params.get("date")
        if not current_date_raw:
            return None
        current_date = datetime.strptime(current_date_raw, "%Y-%m-%d").date()
        next_date = current_date + timedelta(days=1)
        params["date"] = next_date.isoformat()
        params["month"] = f"{next_date.year:04d}-{next_date.month:02d}"
        new_query = urlencode(params)
        return urlunparse(parsed._replace(query=new_query))
    except Exception:  # noqa: BLE001
        return None


def _is_date_booking_url(url: str) -> bool:
    lower = (url or "").lower()
    return "date=" in lower and "month=" in lower


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
            if locator.count() == 0:
                continue
            if not locator.is_visible():
                continue
            locator.scroll_into_view_if_needed()
            try:
                locator.click(timeout=700)
            except Exception:  # noqa: BLE001
                locator.click(timeout=700, force=True)
            return True
        except Exception:  # noqa: BLE001
            continue

    # Additional fallback by plain text node.
    text_node = page.get_by_text(re.compile(re.escape(label), re.I)).first
    try:
        if text_node.count() == 0:
            raise RuntimeError("not found")
        if not text_node.is_visible():
            raise RuntimeError("not visible")
        text_node.click(timeout=700)
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


def _force_pick_intro_team(page: Page) -> tuple[int, int]:
    """JS fallback for /app/intro/team where normal clicks fail."""
    try:
        picked = page.evaluate(
            """
            () => {
              const clickByText = (targets, maxClicks) => {
                let clicks = 0;
                for (const target of targets.sort(() => Math.random() - 0.5)) {
                  if (clicks >= maxClicks) break;
                  const nodes = Array.from(document.querySelectorAll('button, [role="button"], label, div, span'));
                  const node = nodes.find((el) => {
                    const txt = (el.innerText || el.textContent || '').toLowerCase();
                    if (!txt.includes(target.toLowerCase())) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 20 && rect.height > 10 && style.display !== 'none' && style.visibility !== 'hidden';
                  });
                  if (node) {
                    node.click();
                    clicks += 1;
                  }
                }
                return clicks;
              };

              const top = clickByText(['On my own', 'With my team'], 1);
              const help = clickByText(
                [
                  'Meet with multiple attendees',
                  'Schedule meetings',
                  'Collect payment',
                  'Automate pre/post meeting emails',
                  'Record and transcribe meetings',
                  'Manage contact records'
                ],
                2
              );
              return { top, help };
            }
            """
        )
        if isinstance(picked, dict):
            return int(picked.get("top", 0)), int(picked.get("help", 0))
    except Exception:  # noqa: BLE001
        pass
    return 0, 0


def _force_pick_intro_role(page: Page) -> int:
    try:
        picked = page.evaluate(
            """
            () => {
              const targets = ['Finance','Sales','Customer success','Recruiting','Marketing','Education','Consulting','Other'];
              for (const target of targets.sort(() => Math.random() - 0.5)) {
                const nodes = Array.from(document.querySelectorAll('button, [role="button"], label, div, span'));
                const node = nodes.find((el) => {
                  const txt = (el.innerText || el.textContent || '').toLowerCase();
                  if (!txt.includes(target.toLowerCase())) return false;
                  const rect = el.getBoundingClientRect();
                  const style = window.getComputedStyle(el);
                  return rect.width > 20 && rect.height > 10 && style.display !== 'none' && style.visibility !== 'hidden';
                });
                if (node) {
                  node.click();
                  return 1;
                }
              }
              return 0;
            }
            """
        )
        return int(picked) if isinstance(picked, int) else 0
    except Exception:  # noqa: BLE001
        return 0


def _click_next_until_progress(page: Page, retries: int = 4) -> bool:
    before = page.url
    clicked_any = False
    for _ in range(retries):
        if _safe_click_next(page):
            clicked_any = True
            _pause(page, 120)
            if page.url != before:
                return True
        else:
            _pause(page, 80)
    return page.url != before or clicked_any


def _configure_weekly_hours(page: Page) -> None:
    # Requirement: enable first+last unavailable day with plus-buttons,
    # then set all day ranges to 12:00am - 12:00pm.
    _progress("Applying weekly hours: 12:00am - 12:00pm for all days.")

    # Try to click plus on first and last unavailable rows.
    try:
        unavailable_row_buttons = page.locator(
            "xpath=//*[contains(translate(normalize-space(.),'UNAVAILABLE','unavailable'),'unavailable')]/ancestor::*[self::div or self::li][1]//button"
        )
        cnt = unavailable_row_buttons.count()
        if cnt >= 1:
            unavailable_row_buttons.first.click(timeout=1_200)
            _pause(page, 50)
        if cnt >= 2:
            unavailable_row_buttons.last.click(timeout=1_200)
            _pause(page, 50)
    except Exception:  # noqa: BLE001
        pass

    # JS fallback: click first and last add-buttons on rows without time inputs.
    try:
        page.evaluate(
            """
            () => {
              const rows = Array.from(document.querySelectorAll('div, li')).filter((row) => {
                const txt = (row.innerText || '').trim();
                if (!txt) return false;
                const hasDay = /^[SMTWF]/i.test(txt) || /unavailable/i.test(txt.toLowerCase());
                const hasInput = row.querySelector('input');
                const btns = row.querySelectorAll('button');
                return hasDay && !hasInput && btns.length > 0;
              });
              if (!rows.length) return;
              const firstBtn = rows[0].querySelector('button');
              const lastBtn = rows[rows.length - 1].querySelector('button');
              if (firstBtn) firstBtn.click();
              if (lastBtn && lastBtn !== firstBtn) lastBtn.click();
            }
            """
        )
        _pause(page, 50)
    except Exception:  # noqa: BLE001
        pass

    # JS fast-fill all visible time fields in start/end pairs.
    try:
        filled_count = page.evaluate(
            """
            () => {
              const isVisible = (el) => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 30 && r.height > 18 && s.display !== 'none' && s.visibility !== 'hidden';
              };
              const inputs = Array.from(document.querySelectorAll('input')).filter((el) => {
                const val = (el.value || '').toLowerCase();
                const ph = (el.placeholder || '').toLowerCase();
                const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                return isVisible(el) && (val.includes(':') || ph.includes('time') || aria.includes('time'));
              });
              const max = Math.min(inputs.length, 14);
              for (let i = 0; i < max; i += 1) {
                const target = i % 2 === 0 ? '12:00am' : '12:00pm';
                const el = inputs[i];
                el.focus();
                el.value = target;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
              }
              return max;
            }
            """
        )
        if isinstance(filled_count, int) and filled_count >= 2:
            _progress(f"Weekly hours filled via JS inputs: {filled_count}")
            return
    except Exception:  # noqa: BLE001
        pass

    # Manual fallback.
    time_inputs = page.locator("input[value*=':'], input[placeholder*=':'], input[aria-label*='time' i]")
    count = time_inputs.count()
    if count < 2:
        return

    def fill_time(input_locator: Locator, value: str) -> None:
        input_locator.click(timeout=1_200)
        page.keyboard.press("Control+A")
        page.keyboard.type(value, delay=0)
        page.keyboard.press("Enter")

    max_inputs = min(count, 14)  # 7 days * 2 columns
    for index in range(max_inputs):
        try:
            fill_time(time_inputs.nth(index), "12:00am" if index % 2 == 0 else "12:00pm")
            _pause(page, 20)
        except Exception:  # noqa: BLE001
            continue


def _wait_for_availability_screen(page: Page, timeout_ms: int = 2_500) -> bool:
    started = time.time()
    while (time.time() - started) * 1000 < timeout_ms:
        if (
            page.locator("text=/When are you available to meet with people/i").count() > 0
            or page.locator("text=/Weekly hours/i").count() > 0
            or page.locator("input[value*='am'], input[value*='pm']").count() >= 2
            or "/app/intro/availability" in page.url
        ):
            return True
        _pause(page, 70)
    return False


def _fast_back_from_google(page: Page) -> None:
    try:
        page.go_back(wait_until="commit", timeout=4_000)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        page.evaluate("history.back()")
        _pause(page, 120)
    except Exception:  # noqa: BLE001
        pass


def _complete_onboarding(page: Page) -> None:
    _progress("Onboarding flow started.")
    availability_configured = False
    availability_forced_attempts = 0
    for _ in range(20):
        if "/app/scheduling/meeting_types/" in page.url and availability_configured:
            _progress("Onboarding completed.")
            return
        if "/app/scheduling/meeting_types/" in page.url and not availability_configured:
            if availability_forced_attempts < 1:
                availability_forced_attempts += 1
                _progress("Meeting types reached before availability setup, forcing availability page once.")
                try:
                    page.goto("https://calendly.com/app/intro/availability", wait_until="domcontentloaded", timeout=30_000)
                except Exception:  # noqa: BLE001
                    _progress("Availability force-open failed, continuing without availability setup.")
                    return
            else:
                _progress("Skipping availability setup and finishing registration.")
                return

        if "accounts.google.com" in page.url:
            _progress("Google auth page detected, going back.")
            _fast_back_from_google(page)
            _pause(page, 80)
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
                forced_top, forced_help = _force_pick_intro_team(page)
                top_clicked += forced_top
                help_clicked += forced_help
                _progress(f"Team page fallback picks: top={forced_top}, help={forced_help}")
            if top_clicked == 0 or help_clicked == 0:
                _progress("Could not select all team options yet, trying Next anyway.")
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
                role_clicked += _force_pick_intro_role(page)
            if role_clicked == 0:
                _progress("Could not select role yet, trying Next anyway.")
            _click_next_until_progress(page, retries=5)
            continue

        if (
            page.locator("text=/Set up how your Google calendar will be used/i").count() > 0
            or page.locator("text=/Google calendar/i").count() > 0
            or "/app/intro/calendar" in page.url
        ):
            _progress("Calendar connect step detected, skipping via Next.")
            _click_next_until_progress(page, retries=5)
            if _wait_for_availability_screen(page, timeout_ms=3_000):
                _progress("Availability screen reached after Google step.")
                _configure_weekly_hours(page)
                availability_configured = True
                _click_next_until_progress(page, retries=6)
            else:
                _progress("Availability screen not detected quickly; continue registration without waiting.")
            continue

        if (
            page.locator("text=/When are you available to meet with people/i").count() > 0
            or page.locator("text=/Weekly hours/i").count() > 0
            or "/app/intro/availability" in page.url
        ):
            _progress("Configuring weekly hours.")
            _configure_weekly_hours(page)
            availability_configured = True
            _click_next_until_progress(page, retries=6)
            continue

        # Generic fallback for other intro steps.
        _safe_click_next(page)
        _pause(page, 120)

    _progress("Onboarding did not auto-finish in time, opening meeting types URL directly.")
    try:
        page.goto(CALENDLY_MEETING_TYPES_URL, wait_until="domcontentloaded", timeout=90_000)
    except Exception:  # noqa: BLE001
        _progress("Meeting types fallback navigation failed; finishing anyway.")


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
            with open_ads_page(ads_profile_id, stop_profile_on_exit=not KEEP_PROFILE_OPEN_AFTER_REGISTRATION) as (
                page,
                context,
            ):
                _progress(f"Waiting {SIGNUP_INITIAL_DELAY_SECONDS}s before opening signup page.")
                time.sleep(max(0, int(SIGNUP_INITIAL_DELAY_SECONDS)))
                _open_signup_page_with_recovery(page)
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
                try:
                    _finish_email_confirmation(page, confirmation_url, password=password)
                except ConfirmationPasswordStepError as exc:
                    # User request: do not restart new registration attempt when this step fails.
                    # Persist credentials/cookies and finish current registration attempt as saved account.
                    _progress(
                        "Password after email confirmation was not auto-entered. "
                        "Saving account credentials and cookies without restarting registration."
                    )
                    cookie_file = _save_cookies(context, account_name)
                    return RegistrationResult(
                        account_name=account_name,
                        login_email=order.email,
                        password=password,
                        activation_id=order.activation_id,
                        cookie_file=cookie_file,
                        main_page_url=CALENDLY_MEETING_TYPES_URL,
                    )
                _progress("Completing Calendly onboarding")
                onboarding_ok = False
                try:
                    _complete_onboarding(page)
                    onboarding_ok = True
                except Exception as onboarding_exc:  # noqa: BLE001
                    _progress(f"Onboarding warning: {onboarding_exc}")
                    try:
                        page.goto(CALENDLY_MEETING_TYPES_URL, wait_until="domcontentloaded", timeout=60_000)
                        onboarding_ok = True
                    except Exception as fallback_exc:  # noqa: BLE001
                        _progress(f"Meeting types fallback failed: {fallback_exc}")
                if not onboarding_ok:
                    raise CalendlyAutomationError("Onboarding did not complete and fallback navigation failed.")

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

        except (
            CaptchaSolveError,
            AnyMessageApiError,
            ProxyRotationError,
            PlaywrightTimeoutError,
            ConfirmationPasswordStepError,
        ) as exc:
            last_error = exc
            _progress(f"Recoverable registration error on attempt {attempt}: {exc}")
            # Do not roll to a new registration attempt when confirmation step failed:
            # this account may already be created and only needs follow-up/manual login.
            if isinstance(exc, ConfirmationPasswordStepError):
                raise CalendlyAutomationError(
                    f"Account confirmed but password login step failed: {exc}. "
                    "Registration attempt was stopped to avoid creating a new account."
                ) from exc
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


def run_manual_ads_session(
    *,
    account_name: str,
    ads_profile_id: str,
    start_url: str = CALENDLY_SIGNUP_URL,
) -> ManualSessionResult:
    """
    Manual-assist mode:
    1) Open ADS profile and target page.
    2) User performs actions in browser manually.
    3) After Enter in console, save cookies and return current URL.
    """
    ensure_input_files()
    with open_ads_page(ads_profile_id) as (page, context):
        _safe_goto_calendly(page, start_url, timeout_ms=60_000)
        _progress(
            "Manual ADS mode active. Complete actions in browser, then press Enter in console to save cookies."
        )
        try:
            if sys.stdin.isatty():
                input("[MANUAL] Завершите действия в браузере и нажмите Enter...")
        except EOFError:
            # Non-interactive terminal: keep short wait so caller can still use function.
            _pause(page, 1_000)

        cookie_file = _save_cookies(context, account_name)
        current_url = page.url
        _progress(f"Manual session saved. URL={current_url}")
        return ManualSessionResult(cookie_file=cookie_file, last_url=current_url)


def recreate_ads_profile_after_sender_captcha(
    *,
    old_profile_id: str,
    profile_name: str,
    proxy_port: int | None = None,
) -> str:
    proxy_type = (SENDER_CAPTCHA_PROXY_TYPE or "").strip().lower()
    if proxy_type != "socks5":
        raise CalendlyAutomationError("Only socks5 proxy type is supported for sender captcha recovery.")

    ads_client = AdsApiClient()
    _progress(f"Sender captcha recovery: stopping and deleting ADS profile {old_profile_id}")
    try:
        ads_client.stop_browser(old_profile_id)
    except Exception:  # noqa: BLE001
        pass

    try:
        ads_client.delete_profile(old_profile_id)
        _progress(f"Old ADS profile deleted: {old_profile_id}")
    except Exception as exc:  # noqa: BLE001
        _progress(f"Old ADS profile delete warning: {exc}")

    effective_proxy_port = int(proxy_port) if proxy_port is not None else int(SENDER_CAPTCHA_PROXY_PORT)

    _rotate_sender_proxy_for_recovery(effective_proxy_port)

    new_profile_name = (
        f"{SENDER_CAPTCHA_PROFILE_NAME_PREFIX}-{_slugify(profile_name)}-{int(time.time())}"
    )
    screen_width, screen_height = _parse_screen_resolution(SENDER_CAPTCHA_DESKTOP_SCREEN)
    new_profile_id = ads_client.create_profile_with_socks5(
        profile_name=new_profile_name,
        proxy_host=SENDER_CAPTCHA_PROXY_HOST,
        proxy_port=effective_proxy_port,
        proxy_user=SENDER_CAPTCHA_PROXY_USER,
        proxy_password=SENDER_CAPTCHA_PROXY_PASSWORD,
        desktop_screen_width=screen_width,
        desktop_screen_height=screen_height,
        desktop_os=SENDER_CAPTCHA_DESKTOP_OS,
        desktop_user_agent=SENDER_CAPTCHA_DESKTOP_UA,
    )
    _progress(f"New ADS profile created for sender recovery: {new_profile_id}")
    return new_profile_id


def _dismiss_cookie_banner(page: Page) -> None:
    _click_first(
        page,
        [
            "button:has-text('Accept')",
            "button:has-text('Allow all')",
            "button:has-text('Got it')",
            "button[aria-label*='close' i]",
            "button:has-text('×')",
        ],
        timeout=1_500,
    )


def _open_event_editor_panel(page: Page) -> bool:
    _dismiss_cookie_banner(page)
    _pause(page, 120)

    menu_clicked = False
    # 1) Direct button by explicit tooltip/aria label.
    menu_clicked = _click_first(
        page,
        [
            "button[aria-label*='Meeting settings' i]",
            "button[aria-label*='settings' i]",
            "button[aria-haspopup='menu'][aria-label*='Minute' i]",
        ],
        timeout=2_000,
    )

    # 2) XPath fallback anchored to Copy link button in event row.
    if not menu_clicked:
        menu_clicked = _click_first(
            page,
            [
                "xpath=(//button[contains(., 'Copy link')]/following::button[1])[1]",
                "button:right-of(:text('Copy link'))",
                "button[aria-label*='more' i]",
                "button[aria-haspopup='menu']",
            ],
            timeout=3_000,
        )

    # 3) Hover row then click last menu button in row.
    if not menu_clicked:
        meeting_row = page.locator("div:has-text('Minute Meeting'), div:has-text('Meeting')").first
        try:
            meeting_row.wait_for(state="visible", timeout=3_000)
            meeting_row.scroll_into_view_if_needed()
            meeting_row.hover(timeout=1_500)
            row_menu = meeting_row.locator("button[aria-haspopup='menu'], button[aria-label*='more' i]").last
            row_menu.click(timeout=2_000)
            menu_clicked = True
        except Exception:  # noqa: BLE001
            menu_clicked = False

    if menu_clicked:
        if _click_first(
            page,
            [
                "role=menuitem[name=/Edit/i]",
                "text=Edit",
                "button:has-text('Edit')",
            ],
            timeout=3_000,
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
        _safe_goto_calendly(page, profile.main_page_url or CALENDLY_MEETING_TYPES_URL, timeout_ms=60_000)
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


def _is_booking_form_visible(page: Page) -> bool:
    try:
        if (
            page.locator("text=/Enter Details/i").count() > 0
            and page.locator("button:has-text('Schedule Event')").count() > 0
        ):
            return True
    except Exception:  # noqa: BLE001
        pass

    selectors = [
        "input[name='name']",
        "input[aria-label*='name' i]",
        "input[placeholder*='name' i]",
        "xpath=//*[contains(., 'Name')]/following::input[1]",
    ]
    has_name = False
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() > 0 and loc.is_visible():
                has_name = True
                break
        except Exception:  # noqa: BLE001
            continue

    email_selectors = [
        "input[name='email']",
        "input[type='email']",
        "input[aria-label*='email' i]",
        "input[placeholder*='email' i]",
        "xpath=//*[contains(., 'Email')]/following::input[1]",
    ]
    has_email = False
    for selector in email_selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() > 0 and loc.is_visible():
                has_email = True
                break
        except Exception:  # noqa: BLE001
            continue

    return has_name and has_email


def _count_visible_time_buttons(page: Page) -> int:
    try:
        buttons = page.locator("button")
        count = buttons.count()
    except Exception:  # noqa: BLE001
        return 0

    matched = 0
    for idx in range(min(count, 200)):
        btn = buttons.nth(idx)
        try:
            if not btn.is_visible():
                continue
            text = (btn.text_content() or "").strip().lower()
            if re.fullmatch(r"\d{1,2}:\d{2}\s?(am|pm)", text):
                matched += 1
        except Exception:  # noqa: BLE001
            continue
    return matched


def _wait_booking_calendar_ready(page: Page, timeout_seconds: int = 12) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        _raise_sender_captcha(page, "calendar loading")
        if _is_booking_form_visible(page):
            return
        if _count_visible_time_buttons(page) > 0:
            return
        if _count_clickable_date_buttons(page) > 0:
            return
        _pause(page, 40)


def _click_first_time_button(page: Page) -> bool:
    return _click_time_button_by_offset(page, 0)


def _click_time_button_by_offset(page: Page, offset: int) -> bool:
    visible_before = _count_visible_time_buttons(page)
    _progress(f"Visible time buttons: {visible_before}")
    try:
        clicked = page.evaluate(
            """
            (slotOffset) => {
              const buttons = Array.from(document.querySelectorAll('button'));
              const regex = /^\\d{1,2}:\\d{2}\\s?(am|pm)$/i;
              const matched = [];
              for (const btn of buttons) {
                const txt = (btn.textContent || '').trim();
                if (!regex.test(txt)) continue;
                if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') continue;
                const r = btn.getBoundingClientRect();
                const s = window.getComputedStyle(btn);
                if (r.width < 40 || r.height < 20) continue;
                if (s.display === 'none' || s.visibility === 'hidden') continue;
                matched.push(btn);
              }
              if (!matched.length) return false;
              const idx = Math.min(Math.max(Number(slotOffset) || 0, 0), matched.length - 1);
              matched[idx].click();
              return true;
            }
            """,
            int(max(0, offset)),
        )
        if clicked:
            _pause(page, 100)
            _click_first(page, ["button:has-text('Next')", "button:has-text('Confirm')"], timeout=1_200)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _count_clickable_date_buttons(page: Page) -> int:
    try:
        return int(
            page.evaluate(
                """
                () => {
                  const buttons = Array.from(document.querySelectorAll('button'));
                  let matched = 0;
                  for (const btn of buttons) {
                    const txt = (btn.textContent || '').trim();
                    if (!/^\\d{1,2}$/.test(txt)) continue;
                    if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') continue;
                    const aria = (btn.getAttribute('aria-label') || '').toLowerCase();
                    if (aria.includes('unavailable')) continue;
                    const r = btn.getBoundingClientRect();
                    const s = window.getComputedStyle(btn);
                    if (r.width < 18 || r.height < 18) continue;
                    if (s.display === 'none' || s.visibility === 'hidden') continue;
                    matched += 1;
                  }
                  return matched;
                }
                """
            )
        )
    except Exception:  # noqa: BLE001
        return 0


def _click_first_available_date(page: Page) -> bool:
    clickable_count = _count_clickable_date_buttons(page)
    _progress(f"Clickable day buttons: {clickable_count}")
    try:
        clicked = page.evaluate(
            """
            () => {
              const buttons = Array.from(document.querySelectorAll('button'));
              for (const btn of buttons) {
                const txt = (btn.textContent || '').trim();
                if (!/^\\d{1,2}$/.test(txt)) continue; // only day circles
                if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') continue;
                const aria = (btn.getAttribute('aria-label') || '').toLowerCase();
                if (aria.includes('unavailable')) continue;
                const r = btn.getBoundingClientRect();
                const s = window.getComputedStyle(btn);
                if (r.width < 18 || r.height < 18) continue;
                if (s.display === 'none' || s.visibility === 'hidden') continue;
                btn.click();
                return true;
              }
              return false;
            }
            """
        )
        if clicked:
            _pause(page, 100)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _go_to_next_calendar_month(page: Page) -> bool:
    return _click_first(
        page,
        [
            "button[aria-label*='Next month' i]",
            "button[aria-label*='next' i]",
            "button:has-text('Next month')",
            "button:has-text('›')",
            "button:has-text('>')",
        ],
        timeout=1_500,
    )


def _select_first_available_time(
    page: Page,
    max_months_ahead: int = 6,
    preferred_time_offset: int = 0,
    strict_date_mode: bool = False,
) -> bool:
    """
    Open booking form by picking nearest available date/time.
    """
    if _is_booking_form_visible(page):
        return True

    _wait_booking_calendar_ready(page, timeout_seconds=12)

    # Date-link mode: only pick time on current date URL, do not switch day/month via UI.
    if strict_date_mode:
        if _click_time_button_by_offset(page, preferred_time_offset):
            return _is_booking_form_visible(page)
        return _is_booking_form_visible(page)

    # Try current month first, then switch months.
    for month_idx in range(max_months_ahead + 1):
        _raise_sender_captcha(page, f"month scan {month_idx}")
        if _click_time_button_by_offset(page, preferred_time_offset):
            if _is_booking_form_visible(page):
                return True

        # If times are hidden until date chosen, pick date and retry time.
        if _click_first_available_date(page):
            if _click_time_button_by_offset(page, preferred_time_offset):
                if _is_booking_form_visible(page):
                    return True

        if month_idx < max_months_ahead and _go_to_next_calendar_month(page):
            _progress(f"No slot in current month, switching to next month ({month_idx + 1}/{max_months_ahead}).")
            _pause(page, 180)
            continue
        break

    return _is_booking_form_visible(page)


def _wait_booking_confirmation_with_captcha_guard(page: Page, timeout_ms: int = 25_000) -> None:
    deadline = time.time() + (timeout_ms / 1000)
    while time.time() < deadline:
        _raise_sender_captcha(page, "confirmation polling")
        try:
            marker = page.locator("text=/scheduled|confirmed|you are scheduled/i").first
            if marker.count() > 0 and marker.is_visible():
                return
        except Exception:  # noqa: BLE001
            pass
        _pause(page, 30)
    raise CalendlyAutomationError("Booking confirmation timeout.")


def _submit_booking_form(page: Page) -> None:
    if not _click_first(
        page,
        selectors=[
            "button:has-text('Schedule Event')",
            "button:has-text('Schedule')",
            "button[type='submit']",
        ],
        timeout=10_000,
    ):
        _raise_sender_captcha(page, "before schedule click")
        raise CalendlyAutomationError("Schedule button not found.")


def _wait_booking_confirmation(page: Page) -> None:
    _progress("Waiting booking confirmation.")
    try:
        _raise_sender_captcha(page, "after schedule click")
        _wait_booking_confirmation_with_captcha_guard(page, timeout_ms=12_000)
        time.sleep(max(0, SCHEDULE_CONFIRM_EXTRA_WAIT_SECONDS))
        _raise_sender_captcha(page, "waiting confirmation")
    except Exception as exc:  # noqa: BLE001
        _raise_sender_captcha(page, "confirmation wait failed")
        raise CalendlyAutomationError("Booking confirmation was not detected.") from exc


def _fill_booking_form(
    page: Page,
    invitee_name: str,
    invitee_email: str,
    guest_emails: list[str],
    *,
    submit_and_wait: bool = True,
) -> None:
    _progress("Filling Enter Details form.")
    _raise_sender_captcha(page, "booking form opened")

    # Try strict selectors first, with fast fill.
    name_filled = False
    for selector in [
        "input[name='name']",
        "input[aria-label*='Name' i]",
        "input[placeholder*='Name' i]",
        "xpath=//*[contains(translate(.,'NAME','name'),'name')]/following::input[1]",
    ]:
        candidate = page.locator(selector).first
        try:
            if candidate.count() == 0 or not candidate.is_visible():
                continue
            if _fill_fast_input(candidate, invitee_name):
                name_filled = True
                break
        except Exception:  # noqa: BLE001
            continue
    if not name_filled:
        # Fallback: first visible text input on form.
        inputs = page.locator("input:not([type='hidden'])")
        count = inputs.count()
        for idx in range(min(count, 8)):
            candidate = inputs.nth(idx)
            try:
                if candidate.is_visible():
                    if _fill_fast_input(candidate, invitee_name):
                        name_filled = True
                        break
            except Exception:  # noqa: BLE001
                continue
    if not name_filled:
        # Final JS fallback.
        try:
            name_filled = bool(
                page.evaluate(
                    """
                    (value) => {
                      const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"])'));
                      const visible = inputs.filter((el) => {
                        const r = el.getBoundingClientRect();
                        const s = window.getComputedStyle(el);
                        return r.width > 40 && r.height > 18 && s.display !== 'none' && s.visibility !== 'hidden';
                      });
                      if (!visible.length) return false;
                      const target = visible[0];
                      target.focus();
                      target.value = value;
                      target.dispatchEvent(new Event('input', { bubbles: true }));
                      target.dispatchEvent(new Event('change', { bubbles: true }));
                      return true;
                    }
                    """,
                    invitee_name,
                )
            )
        except Exception:  # noqa: BLE001
            name_filled = False
    if not name_filled:
        raise CalendlyAutomationError("Invitee Name input not found.")

    email_filled = False
    for selector in [
        "input[name='email']",
        "input[type='email']",
        "input[aria-label*='Email' i]",
        "input[placeholder*='Email' i]",
        "xpath=//*[contains(translate(.,'EMAIL','email'),'email')]/following::input[1]",
    ]:
        candidate = page.locator(selector).first
        try:
            if candidate.count() == 0 or not candidate.is_visible():
                continue
            if _fill_fast_input(candidate, invitee_email):
                email_filled = True
                break
        except Exception:  # noqa: BLE001
            continue
    if not email_filled:
        # Fallback: second visible text-like input on form.
        inputs = page.locator("input:not([type='hidden'])")
        count = inputs.count()
        visible_indices: list[int] = []
        for idx in range(min(count, 10)):
            try:
                if inputs.nth(idx).is_visible():
                    visible_indices.append(idx)
            except Exception:  # noqa: BLE001
                continue
        if len(visible_indices) >= 2:
            try:
                email_filled = _fill_fast_input(inputs.nth(visible_indices[1]), invitee_email)
            except Exception:  # noqa: BLE001
                pass
    if not email_filled:
        # Final JS fallback.
        try:
            email_filled = bool(
                page.evaluate(
                    """
                    (value) => {
                      const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"])'));
                      const visible = inputs.filter((el) => {
                        const r = el.getBoundingClientRect();
                        const s = window.getComputedStyle(el);
                        return r.width > 40 && r.height > 18 && s.display !== 'none' && s.visibility !== 'hidden';
                      });
                      if (visible.length < 2) return false;
                      const target = visible[1];
                      target.focus();
                      target.value = value;
                      target.dispatchEvent(new Event('input', { bubbles: true }));
                      target.dispatchEvent(new Event('change', { bubbles: true }));
                      return true;
                    }
                    """,
                    invitee_email,
                )
            )
        except Exception:  # noqa: BLE001
            email_filled = False
    if not email_filled:
        raise CalendlyAutomationError("Invitee Email input not found.")
    _raise_sender_captcha(page, "booking form filled")

    if guest_emails:
        _progress(f"Adding guests: {len(guest_emails)}")
        _click_first(
            page,
            [
                "button:has-text('Add Guests')",
                "button:has-text('Add guests')",
                "text=Add Guests",
            ],
            timeout=1_500,
        )
        guest_input = None
        for selector in [
            "input[name*='guest' i]",
            "textarea[name*='guest' i]",
            "input[aria-label*='Guest' i]",
            "textarea[aria-label*='Guest' i]",
        ]:
            candidate = page.locator(selector).first
            try:
                if candidate.count() > 0 and candidate.is_visible():
                    guest_input = candidate
                    break
            except Exception:  # noqa: BLE001
                continue

        if guest_input is not None:
            for guest in guest_emails:
                try:
                    guest_input.click(timeout=1_500)
                    if not _fill_fast_input(guest_input, guest):
                        guest_input.fill(guest)
                    guest_input.press("Enter")
                    _pause(page, 40)
                except Exception:  # noqa: BLE001
                    continue
        else:
            _progress("Guest input not found after Add Guests; continuing with main invitee email only.")

    if not submit_and_wait:
        return
    _submit_booking_form(page)
    _wait_booking_confirmation(page)


def run_booking_sender(
    profile: Profile,
    *,
    booking_url: str,
    persist_sent: bool = True,
    single_target_email: str | None = None,
    slot_preference_offset: int = 0,
    enable_runtime_controls: bool = False,
) -> SendStats:
    ensure_input_files()
    if single_target_email:
        email_pool = [single_target_email.strip()]
    else:
        email_pool = load_email_pool()
        if persist_sent:
            email_pool = _filter_unsent_emails(email_pool)
    if not email_pool:
        raise CalendlyAutomationError("Email list is empty (or all emails already sent).")
    total_input = len(email_pool)

    target_booking_url = booking_url.strip() or (profile.booking_url or "").strip()
    if not target_booking_url:
        raise CalendlyAutomationError("Нужна ссылка booking страницы.")
    booking_work_url = target_booking_url
    use_date_mode = _extract_date_from_booking_url(booking_work_url) is not None

    scheduled = 0
    consumed = 0
    used_invitee_names: set[str] = set()
    runtime = SenderRuntimeControls(enabled=enable_runtime_controls)

    with open_ads_page(profile.ads_profile_id) as (page, context):
        _load_cookies_if_present(context, profile.cookie_file)
        pages: list[Page] = [page]
        _progress("Sender mode: single tab.")

        while email_pool:
            _sender_runtime_checkpoint(runtime)
            active_count = 1
            current_batch = email_pool[:active_count]
            ready_jobs: list[tuple[int, Page, str]] = []
            stop_on_slots = False

            # Stage 1: prepare all tabs up to filled form.
            for idx, invitee_email in enumerate(current_batch):
                current_page = pages[idx]
                _progress(f"[Tab {idx + 1}/{len(pages)}] Opening booking page: {booking_work_url}")
                _safe_goto_calendly(current_page, booking_work_url, timeout_ms=60_000)
                _pause(current_page, 120)
                _dismiss_cookie_banner(current_page)
                _raise_sender_captcha(current_page, f"tab {idx + 1} booking page opened")
                _wait_booking_calendar_ready(current_page, timeout_seconds=12)
                _raise_sender_captcha(current_page, f"tab {idx + 1} calendar ready")

                _progress(f"[Tab {idx + 1}] Selecting first available date and time.")
                slot_selected = False
                for slot_attempt in range(1, 4):
                    _raise_sender_captcha(current_page, f"tab {idx + 1} slot attempt {slot_attempt} start")
                    if _select_first_available_time(
                        current_page,
                        preferred_time_offset=max(0, slot_preference_offset),
                        strict_date_mode=use_date_mode,
                    ):
                        slot_selected = True
                        break
                    _raise_sender_captcha(current_page, f"tab {idx + 1} slot attempt {slot_attempt} failed")
                    _progress(f"[Tab {idx + 1}] Slot attempt {slot_attempt}/3 failed.")
                    _pause(current_page, 200)

                if not slot_selected:
                    if use_date_mode:
                        next_url = _next_dated_booking_url(booking_work_url)
                        if next_url:
                            _progress(
                                f"[Tab {idx + 1}] No times on current date link, switching to next date URL: {next_url}"
                            )
                            booking_work_url = next_url
                            continue
                    _progress(f"[Tab {idx + 1}] No available slots found now. Sender stopped without error.")
                    stop_on_slots = True
                    break

                invitee_name = _generate_real_invitee_name(used_invitee_names)
                _fill_booking_form(
                    current_page,
                    invitee_name=invitee_name,
                    invitee_email=invitee_email,
                    guest_emails=[],
                    submit_and_wait=False,
                )
                ready_jobs.append((idx, current_page, invitee_email))

            if stop_on_slots:
                break
            if not ready_jobs:
                break

            # Stage 2: submit in current tab.
            _sender_runtime_checkpoint(runtime)
            _progress("Submitting booking in current tab.")
            submitted_jobs: list[tuple[int, Page, str]] = []
            for idx, current_page, invitee_email in ready_jobs:
                _submit_booking_form(current_page)
                submitted_jobs.append((idx, current_page, invitee_email))

            # Stage 3: wait confirmations for all submitted tabs.
            successful_batch: list[str] = []
            failed_batch: list[str] = []
            for idx, current_page, invitee_email in submitted_jobs:
                try:
                    _wait_booking_confirmation(current_page)
                    successful_batch.append(invitee_email)
                    _progress(f"[Tab {idx + 1}] Booking scheduled: invitee={invitee_email}")
                except SenderCaptchaDetected:
                    # Must be propagated to run_sender_flow for ADS profile recreate recovery.
                    raise
                except Exception as exc:  # noqa: BLE001
                    failed_batch.append(invitee_email)
                    _progress(f"[Tab {idx + 1}] Booking failed for {invitee_email}: {exc}")

            if successful_batch and persist_sent:
                _append_sent_emails_file(successful_batch)
                _save_sent_emails_db(successful_batch)

            # Keep failed emails for retry, remove only successful from current batch.
            success_set = {email.lower() for email in successful_batch}
            retriable_failed = [email for email in current_batch if email.lower() not in success_set]
            email_pool = retriable_failed + email_pool[active_count:]
            if not single_target_email:
                save_email_pool(email_pool)

            consumed += len(successful_batch)
            scheduled += len(successful_batch)
            _progress(f"📊 Прогресс рассылки: {scheduled}/{total_input} отправлено")
            if not successful_batch and failed_batch:
                _progress("No successful bookings in this wave; stopping to avoid endless retry loop.")
                break

            time.sleep(max(0, SEND_BETWEEN_BOOKINGS_SECONDS))

        cookie_file = _save_cookies(context, profile.name)
        _write_log(f"Updated cookie file after booking: {cookie_file}")

    return SendStats(
        total_input_emails=total_input,
        scheduled_events=scheduled,
        consumed_emails=consumed,
        remaining_emails=len(email_pool),
    )


def run_inbox_check_sender(profile: Profile, *, booking_url: str, target_email: str) -> SendStats:
    """
    Send one test booking to a specific email.
    Does not persist email to sent-emails storage.
    """
    return run_booking_sender(
        profile,
        booking_url=booking_url,
        persist_sent=False,
        single_target_email=target_email,
    )
