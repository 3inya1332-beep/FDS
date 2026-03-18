from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

import config
from ads_api import AdsBrowserClient, AdsBrowserSession
from pcloud_client import PCloudApiError, PCloudClient, ShareAttempt
from storage import Profile


@dataclass(slots=True)
class PCloudAuthContext:
    auth_token: str
    location_id: int
    current_url: str


@dataclass(slots=True)
class PreparedProfile:
    ads_session: AdsBrowserSession
    auth: PCloudAuthContext
    userinfo: dict[str, Any]


@dataclass(slots=True)
class WorkflowSummary:
    profile_title: str
    account_email: str
    folder_name: str
    folder_id: int
    total_emails: int
    success_count: int
    skipped_count: int
    failed_count: int
    attempts: list[ShareAttempt]


def prepare_profile(profile: Profile, logger: logging.Logger) -> PreparedProfile:
    ads_client = AdsBrowserClient()
    logger.info("Starting ADS profile %s (%s)", profile.title, profile.ads_profile_id)
    ads_session = ads_client.start_profile(profile.ads_profile_id)
    auth = extract_pcloud_auth(
        ads_session=ads_session,
        start_url=profile.start_url,
        logger=logger,
    )
    pcloud, userinfo = build_validated_pcloud_client(
        auth_token=auth.auth_token,
        preferred_location_id=auth.location_id,
        logger=logger,
    )
    auth.location_id = pcloud.location_id
    return PreparedProfile(
        ads_session=ads_session,
        auth=auth,
        userinfo=userinfo,
    )


def run_pcloud_workflow(profile: Profile, logger: logging.Logger) -> WorkflowSummary:
    prepared = prepare_profile(profile, logger)
    folder_name = read_required_text(config.FOLDER_NAME_FILE, "folder name file")
    message_text = read_text(config.MESSAGE_FILE).strip()
    emails = load_emails(config.EMAILS_FILE)

    pcloud = PCloudClient(
        auth_token=prepared.auth.auth_token,
        location_id=prepared.auth.location_id,
    )
    folder_payload = pcloud.create_folder(
        folder_name=folder_name,
        parent_folder_id=profile.parent_folder_id,
    )
    folder_metadata = folder_payload.get("metadata") or {}
    folder_id = int(folder_metadata["folderid"])

    logger.info(
        "Using pCloud account %s, folder %s (%s), email count=%s",
        prepared.userinfo.get("email", ""),
        folder_name,
        folder_id,
        len(emails),
    )

    attempts: list[ShareAttempt] = []
    total_batches = (len(emails) + config.DEFAULT_BATCH_SIZE - 1) // config.DEFAULT_BATCH_SIZE
    for batch_index, batch in enumerate(chunked(emails, config.DEFAULT_BATCH_SIZE), start=1):
        logger.info("Processing batch %s/%s (%s emails)", batch_index, total_batches, len(batch))
        attempts.extend(
            share_batch(
                auth_token=prepared.auth.auth_token,
                location_id=prepared.auth.location_id,
                folder_id=folder_id,
                permissions=profile.permission_level,
                message_text=message_text,
                share_name=folder_name,
                emails=batch,
                logger=logger,
            )
        )

    success_count = sum(1 for attempt in attempts if attempt.status == "success")
    skipped_count = sum(1 for attempt in attempts if attempt.status == "skipped")
    failed_count = sum(1 for attempt in attempts if attempt.status == "failed")

    return WorkflowSummary(
        profile_title=profile.title,
        account_email=str(prepared.userinfo.get("email", "")),
        folder_name=folder_name,
        folder_id=folder_id,
        total_emails=len(emails),
        success_count=success_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
        attempts=attempts,
    )


def extract_pcloud_auth(
    ads_session: AdsBrowserSession,
    start_url: str,
    logger: logging.Logger,
) -> PCloudAuthContext:
    endpoint = ads_session.puppeteer_endpoint
    if not endpoint and ads_session.debug_port:
        endpoint = f"http://127.0.0.1:{ads_session.debug_port}"
    if not endpoint:
        raise RuntimeError("ADS Browser did not return a CDP endpoint.")

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(endpoint)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()

        if config.OPEN_START_URL_AFTER_CONNECT:
            safe_goto(page, start_url, logger)

        auth_context = read_auth_from_browser_context(context, page, logger)
        if auth_context.auth_token:
            return auth_context

        if config.RETRY_AUTH_AFTER_MANUAL_LOGIN:
            print(
                "\nNo active pCloud session was found in ADS Browser.\n"
                "If the account is not logged in right now, complete login in the profile window,\n"
                "then press Enter here to retry.\n"
            )
            input("Press Enter after manual login: ")
            safe_goto(page, start_url, logger)
            auth_context = read_auth_from_browser_context(context, page, logger)
            if auth_context.auth_token:
                return auth_context

    raise RuntimeError(
        "Could not extract pCloud auth token from ADS Browser. "
        "Make sure the profile is logged into https://my.pcloud.com/."
    )


def build_validated_pcloud_client(
    auth_token: str,
    preferred_location_id: int,
    logger: logging.Logger,
) -> tuple[PCloudClient, dict[str, Any]]:
    location_candidates = [preferred_location_id]
    for location_id in config.PCLOUD_API_HOSTS:
        if location_id not in location_candidates:
            location_candidates.append(location_id)

    last_error: Exception | None = None
    for location_id in location_candidates:
        client = PCloudClient(auth_token=auth_token, location_id=location_id)
        try:
            userinfo = client.userinfo()
            logger.info("Validated pCloud auth on location %s (%s)", location_id, client.host)
            return client, userinfo
        except Exception as exc:  # pragma: no cover - network/runtime fallback
            last_error = exc
            logger.warning("pCloud auth validation failed on location %s: %s", location_id, exc)

    raise RuntimeError(f"Could not validate pCloud auth token. Last error: {last_error}")


def safe_goto(page: Any, start_url: str, logger: logging.Logger) -> None:
    try:
        page.goto(start_url, wait_until="domcontentloaded", timeout=30_000)
    except PlaywrightTimeoutError:
        logger.warning("Timed out while opening %s, continuing with current page.", start_url)


def read_auth_from_browser_context(context: Any, page: Any, logger: logging.Logger) -> PCloudAuthContext:
    cookies = context.cookies()
    cookie_map = {cookie["name"]: cookie["value"] for cookie in cookies}
    logger.info("Collected %s cookies from ADS Browser context", len(cookie_map))

    auth_token = (
        cookie_map.get("pcauth")
        or cookie_map.get("auth")
        or cookie_map.get("authtoken")
        or ""
    )
    location_id = parse_location_id(cookie_map.get("locationid"))

    try:
        page_data = page.evaluate(
            """
            () => {
              const storageToObject = (storage) => {
                const result = {};
                for (let index = 0; index < storage.length; index += 1) {
                  const key = storage.key(index);
                  result[key] = storage.getItem(key);
                }
                return result;
              };

              return {
                href: window.location.href,
                cookie: document.cookie,
                pcloudAuth: window.__pcloudAuth || null,
                localStorage: storageToObject(window.localStorage),
                sessionStorage: storageToObject(window.sessionStorage)
              };
            }
            """
        )
    except Exception as exc:  # pragma: no cover - browser-specific fallback
        logger.warning("Could not evaluate page context: %s", exc)
        page_data = {"href": "", "cookie": "", "pcloudAuth": {}, "localStorage": {}, "sessionStorage": {}}

    document_cookies = parse_cookie_header(page_data.get("cookie", ""))
    if not auth_token:
        auth_token = (
            document_cookies.get("pcauth")
            or (page_data.get("pcloudAuth") or {}).get("authtoken")
            or (page_data.get("pcloudAuth") or {}).get("auth")
            or find_auth_token_in_storage(page_data.get("localStorage") or {})
            or find_auth_token_in_storage(page_data.get("sessionStorage") or {})
            or ""
        )
    if location_id == config.DEFAULT_LOCATION_ID:
        location_id = parse_location_id(
            document_cookies.get("locationid")
            or (page_data.get("pcloudAuth") or {}).get("locationid")
        )

    return PCloudAuthContext(
        auth_token=auth_token,
        location_id=location_id,
        current_url=str(page_data.get("href", "")),
    )


def parse_location_id(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return config.DEFAULT_LOCATION_ID


def parse_cookie_header(raw_cookie_header: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for chunk in raw_cookie_header.split(";"):
        if "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        result[name.strip()] = value.strip()
    return result


def find_auth_token_in_storage(storage: dict[str, str]) -> str:
    candidate_keys = (
        "pcauth",
        "auth",
        "authtoken",
        "token",
    )
    for key, value in storage.items():
        key_lower = key.lower()
        if any(candidate in key_lower for candidate in candidate_keys) and value:
            return value
    return ""


def share_batch(
    auth_token: str,
    location_id: int,
    folder_id: int,
    permissions: int,
    message_text: str,
    share_name: str,
    emails: list[str],
    logger: logging.Logger,
) -> list[ShareAttempt]:
    worker_count = max(1, min(config.MAX_WORKERS, len(emails)))
    if worker_count == 1:
        return [
            share_single_email(
                auth_token=auth_token,
                location_id=location_id,
                folder_id=folder_id,
                permissions=permissions,
                message_text=message_text,
                share_name=share_name,
                email=email,
                logger=logger,
            )
            for email in emails
        ]

    attempts: list[ShareAttempt] = []
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(
                share_single_email,
                auth_token,
                location_id,
                folder_id,
                permissions,
                message_text,
                share_name,
                email,
                logger,
            ): email
            for email in emails
        }
        for future in as_completed(future_map):
            attempts.append(future.result())
    return attempts


def share_single_email(
    auth_token: str,
    location_id: int,
    folder_id: int,
    permissions: int,
    message_text: str,
    share_name: str,
    email: str,
    logger: logging.Logger,
) -> ShareAttempt:
    if config.REQUEST_DELAY_SECONDS > 0:
        time.sleep(config.REQUEST_DELAY_SECONDS)

    client = PCloudClient(auth_token=auth_token, location_id=location_id)
    try:
        attempt = client.share_folder(
            folder_id=folder_id,
            email=email,
            permissions=permissions,
            message=message_text,
            share_name=share_name,
        )
        logger.info("[%s] %s", attempt.status.upper(), email)
        return attempt
    except PCloudApiError as exc:
        logger.error("[FAILED] %s -> %s (%s)", email, exc, exc.result_code)
        return ShareAttempt(
            email=email,
            status="failed",
            detail=str(exc),
            result_code=exc.result_code,
        )
    except Exception as exc:  # pragma: no cover - network/runtime fallback
        logger.exception("Unexpected error while sending invite to %s", email)
        return ShareAttempt(
            email=email,
            status="failed",
            detail=str(exc),
            result_code=-1,
        )


def read_required_text(path: Path, label: str) -> str:
    content = read_text(path).strip()
    if not content:
        raise ValueError(f"{label.capitalize()} is empty: {path}")
    return content


def read_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Required file not found: {path}")
    return path.read_text(encoding="utf-8").strip()


def load_emails(path: Path) -> list[str]:
    raw_content = read_required_text(path, "email file")
    result: list[str] = []
    seen: set[str] = set()
    for line in raw_content.splitlines():
        email = line.strip()
        if not email or email.startswith("#"):
            continue
        normalized = email.lower()
        if "@" not in normalized:
            raise ValueError(f"Invalid email entry: {email}")
        if normalized not in seen:
            seen.add(normalized)
            result.append(email)
    if not result:
        raise ValueError(f"No emails found in {path}")
    return result


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]
