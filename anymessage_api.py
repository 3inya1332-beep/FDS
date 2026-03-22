from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import requests

from config import (
    ANYMESSAGE_API_BASE,
    ANYMESSAGE_DOMAIN,
    ANYMESSAGE_SITE,
    ANYMESSAGE_API_TOKEN,
    ANYMESSAGE_CONFIRM_TIMEOUT_SECONDS,
    ANYMESSAGE_MESSAGES_PATHS,
    ANYMESSAGE_POLL_INTERVAL_SECONDS,
    ANYMESSAGE_PURCHASE_PATHS,
    ANYMESSAGE_SERVICE,
    ANYMESSAGE_TIMEOUT_SECONDS,
)


class AnyMessageApiError(RuntimeError):
    pass


@dataclass
class AnyMessageMailbox:
    email: str
    order_id: str | None


class AnyMessageClient:
    def __init__(
        self,
        api_base: str = ANYMESSAGE_API_BASE,
        api_token: str = ANYMESSAGE_API_TOKEN,
        timeout_seconds: int = ANYMESSAGE_TIMEOUT_SECONDS,
        service: str = ANYMESSAGE_SERVICE,
        site: str = ANYMESSAGE_SITE,
        domain: str = ANYMESSAGE_DOMAIN,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.api_token = api_token.strip()
        self.timeout_seconds = timeout_seconds
        self.service = service
        self.site = site.strip()
        self.domain = domain.strip()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
            headers["X-API-KEY"] = self.api_token
            headers["api-key"] = self.api_token
        return headers

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{self.api_base}{path}"
        response = requests.request(
            method=method.upper(),
            url=url,
            headers=self._headers(),
            timeout=self.timeout_seconds,
            **kwargs,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise AnyMessageApiError(f"Unexpected AnyMessage payload: {payload!r}")
        return payload

    @staticmethod
    def _is_success(payload: dict[str, Any]) -> bool:
        status = str(payload.get("status", "")).strip().lower()
        if status:
            return status == "success"
        return bool(payload.get("success", False))

    @staticmethod
    def _error_value(payload: dict[str, Any]) -> str:
        for key in ("value", "message", "msg", "error"):
            value = payload.get(key)
            if value is not None:
                return str(value)
        return str(payload)

    @staticmethod
    def _extract_data(payload: dict[str, Any]) -> dict[str, Any]:
        if isinstance(payload.get("data"), dict):
            return payload["data"]
        return payload

    @staticmethod
    def _extract_email_info(payload: dict[str, Any]) -> AnyMessageMailbox | None:
        data = AnyMessageClient._extract_data(payload)
        keys = ("email", "mail", "gmail", "address", "mailbox")
        email: str | None = None
        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and "@" in value:
                email = value.strip()
                break
        if email is None and isinstance(data.get("account"), dict):
            account = data["account"]
            value = account.get("email")
            if isinstance(value, str) and "@" in value:
                email = value.strip()
        if email is None:
            return None

        order_id: str | None = None
        for key in ("order_id", "orderId", "id", "mail_id"):
            value = data.get(key)
            if value is not None:
                order_id = str(value)
                break
        return AnyMessageMailbox(email=email, order_id=order_id)

    def buy_gmail(self) -> AnyMessageMailbox:
        if not self.api_token:
            raise AnyMessageApiError("AnyMessage token is empty. Fill ANYMESSAGE_API_TOKEN in config.py")

        # Primary mode for anymessage.shop API (token from Telegram bot profile).
        try:
            payload = self._request(
                "GET",
                "/email/order",
                params={"token": self.api_token, "site": self.site, "domain": self.domain},
            )
            if not self._is_success(payload):
                raise AnyMessageApiError(f"AnyMessage order error: {self._error_value(payload)}")
            mailbox = self._extract_email_info(payload)
            if mailbox is not None:
                return mailbox
            emails_value = payload.get("emails")
            if isinstance(emails_value, list) and emails_value:
                first = emails_value[0]
                if isinstance(first, dict):
                    nested_mailbox = self._extract_email_info(first)
                    if nested_mailbox is not None:
                        return nested_mailbox
            raise AnyMessageApiError(f"AnyMessage order response has no email: {payload!r}")
        except AnyMessageApiError:
            raise
        except Exception:
            # Fallback for legacy/private AnyMessage API variants below.
            pass

        payload_variants = [
            {"json": {"service": self.service}},
            {"json": {"type": self.service}},
            {"json": {"mail_type": self.service}},
            {"params": {"service": self.service}},
            {"params": {"type": self.service}},
        ]

        last_error: str | None = None
        for path in ANYMESSAGE_PURCHASE_PATHS:
            for kwargs in payload_variants:
                try:
                    payload = self._request("POST", path, **kwargs)
                    mailbox = self._extract_email_info(payload)
                    if mailbox is not None:
                        return mailbox
                    last_error = f"No email in response: {payload!r}"
                except Exception as exc:  # noqa: BLE001
                    last_error = str(exc)
                    continue
        raise AnyMessageApiError(f"Unable to buy gmail via AnyMessage API. Last error: {last_error}")

    @staticmethod
    def _extract_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
        data = payload.get("data", payload)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("messages", "items", "list", "rows"):
                value = data.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _message_to_text(message: dict[str, Any]) -> str:
        chunks: list[str] = []
        for key in ("subject", "text", "body", "html", "message", "content"):
            value = message.get(key)
            if isinstance(value, str):
                chunks.append(value)
        return "\n".join(chunks)

    @staticmethod
    def _extract_inflow_confirmation_link(text: str) -> str | None:
        # Accept both accounts/app links as confirmation entrypoints.
        url_regex = re.compile(r"https?://[^\s<>\"]+", re.I)
        for link in url_regex.findall(text):
            lower_link = link.lower()
            if "inflowinventory.com" not in lower_link:
                continue
            if "verify" in lower_link or "confirm" in lower_link or "activate" in lower_link:
                return link
        return None

    def wait_inflow_confirmation_link(
        self,
        mailbox: AnyMessageMailbox,
        timeout_seconds: int = ANYMESSAGE_CONFIRM_TIMEOUT_SECONDS,
        poll_interval_seconds: int = ANYMESSAGE_POLL_INTERVAL_SECONDS,
    ) -> str:
        if not mailbox.order_id:
            raise AnyMessageApiError(
                f"AnyMessage mailbox has no order id for {mailbox.email}. Cannot poll getmessage."
            )
        deadline = time.time() + timeout_seconds
        last_error: str | None = None

        # Primary mode for anymessage.shop API.
        while time.time() < deadline:
            try:
                payload = self._request(
                    "GET",
                    "/email/getmessage",
                    params={"token": self.api_token, "id": mailbox.order_id or "", "preview": "0"},
                )
                if not self._is_success(payload):
                    value = self._error_value(payload).lower()
                    if "no activation" in value or "not found" in value or "wait" in value:
                        time.sleep(max(1, poll_interval_seconds))
                        continue
                    raise AnyMessageApiError(f"AnyMessage getmessage error: {self._error_value(payload)}")

                message_candidates: list[str] = []
                for key in ("message", "value", "text", "body", "html", "content"):
                    value = payload.get(key)
                    if isinstance(value, str):
                        message_candidates.append(value)
                joined = "\n".join(message_candidates)
                link = self._extract_inflow_confirmation_link(joined)
                if link:
                    return link
            except AnyMessageApiError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
            time.sleep(max(1, poll_interval_seconds))

        # Fallback for legacy/private API variants.
        while time.time() < deadline:
            for path in ANYMESSAGE_MESSAGES_PATHS:
                params_variants: list[dict[str, str]] = [{"email": mailbox.email}, {"mail": mailbox.email}]
                if mailbox.order_id:
                    params_variants.extend(
                        [{"order_id": mailbox.order_id}, {"id": mailbox.order_id}, {"mail_id": mailbox.order_id}]
                    )
                for params in params_variants:
                    try:
                        payload = self._request("GET", path, params=params)
                        for item in self._extract_messages(payload):
                            text = self._message_to_text(item)
                            link = self._extract_inflow_confirmation_link(text)
                            if link:
                                return link
                    except Exception as exc:  # noqa: BLE001
                        last_error = str(exc)
                        continue
            time.sleep(max(1, poll_interval_seconds))

        raise AnyMessageApiError(
            f"Confirmation email not received for {mailbox.email} within timeout. Last error: {last_error}"
        )
