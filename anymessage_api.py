from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests

from config import (
    ANYMESSAGE_API_BASE,
    ANYMESSAGE_DOMAIN,
    ANYMESSAGE_MAX_WAIT_SECONDS,
    ANYMESSAGE_POLL_SECONDS,
    ANYMESSAGE_SITE,
    ANYMESSAGE_TOKEN,
)


class AnyMessageApiError(RuntimeError):
    pass


@dataclass
class AnyMessageOrder:
    activation_id: str
    email: str


class AnyMessageClient:
    def __init__(
        self,
        base_url: str = ANYMESSAGE_API_BASE,
        token: str = ANYMESSAGE_TOKEN,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        if not self.token:
            raise AnyMessageApiError("ANYMESSAGE_TOKEN is empty in config.py")

    def _request(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> Any:
        url = f"{self.base_url}{path}"
        query = {"token": self.token}
        if params:
            query.update(params)

        response = requests.get(url, params=query, timeout=timeout)
        response.raise_for_status()

        content_type = response.headers.get("content-type", "").lower()
        if "application/json" in content_type:
            payload = response.json()
            self._raise_if_error(payload)
            return payload

        text = response.text.strip()
        # Some AnyMessage endpoints can return plain text or HTML preview.
        if text.startswith("{") and text.endswith("}"):
            try:
                payload = response.json()
                self._raise_if_error(payload)
                return payload
            except Exception:  # noqa: BLE001
                pass
        return text

    @staticmethod
    def _raise_if_error(payload: Any) -> None:
        if not isinstance(payload, dict):
            return

        # Different response formats are tolerated.
        status = payload.get("status")
        success = payload.get("success")
        error = payload.get("error") or payload.get("message")
        if isinstance(status, str) and status.lower() in {"error", "fail", "failed"}:
            raise AnyMessageApiError(str(error or payload))
        if success is False:
            raise AnyMessageApiError(str(error or payload))
        if isinstance(payload.get("code"), int) and int(payload["code"]) not in {0, 200}:
            raise AnyMessageApiError(str(error or payload))

    @staticmethod
    def _extract_id_and_email(payload: Any) -> AnyMessageOrder:
        if isinstance(payload, str):
            # Frequently returned format: "<id>:<email>".
            if ":" in payload and "@" in payload:
                activation_id, email = payload.split(":", 1)
                return AnyMessageOrder(activation_id=activation_id.strip(), email=email.strip())
            raise AnyMessageApiError(f"Unexpected AnyMessage order response: {payload}")

        if not isinstance(payload, dict):
            raise AnyMessageApiError(f"Unexpected AnyMessage order response: {payload!r}")

        activation_id = (
            payload.get("id")
            or payload.get("activation_id")
            or payload.get("activationId")
            or payload.get("mail_id")
        )
        email = payload.get("mail") or payload.get("email") or payload.get("address")

        # Sometimes data is nested.
        if (not activation_id or not email) and isinstance(payload.get("data"), dict):
            data = payload["data"]
            activation_id = activation_id or data.get("id") or data.get("activation_id")
            email = email or data.get("mail") or data.get("email")

        if not activation_id or not email:
            raise AnyMessageApiError(f"AnyMessage order response missing id/email: {payload}")
        return AnyMessageOrder(activation_id=str(activation_id), email=str(email))

    @staticmethod
    def _extract_message_text(payload: Any) -> str:
        if isinstance(payload, str):
            return payload
        if not isinstance(payload, dict):
            return ""

        candidates: list[str] = []
        for key in ("html", "message", "body", "text"):
            value = payload.get(key)
            if isinstance(value, str):
                candidates.append(value)

        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("html", "message", "body", "text"):
                value = data.get(key)
                if isinstance(value, str):
                    candidates.append(value)

        return "\n".join(candidates)

    def order_email(
        self,
        *,
        site: str = ANYMESSAGE_SITE,
        domain: str = ANYMESSAGE_DOMAIN,
    ) -> AnyMessageOrder:
        payload = self._request(
            "/email/order",
            params={
                "site": site,
                "domain": domain,
            },
        )
        return self._extract_id_and_email(payload)

    def get_message(self, activation_id: str, preview: bool = True) -> Any:
        params: dict[str, Any] = {"id": activation_id}
        if preview:
            params["preview"] = 1
        return self._request("/email/getmessage", params=params)

    def build_message_link(self, activation_id: str) -> str:
        query = urlencode({"token": self.token, "id": activation_id})
        return f"{self.base_url}/email/getmessage?{query}"

    def wait_for_confirmation_link(
        self,
        activation_id: str,
        *,
        timeout_seconds: int = ANYMESSAGE_MAX_WAIT_SECONDS,
        poll_seconds: int = ANYMESSAGE_POLL_SECONDS,
    ) -> str:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            payload = self.get_message(activation_id, preview=True)
            message = html.unescape(self._extract_message_text(payload))
            match = re.search(
                r"https://calendly\.com/users/confirmation\?confirmation_token=[A-Za-z0-9._\-]+",
                message,
                flags=re.I,
            )
            if match:
                return match.group(0)
            time.sleep(poll_seconds)

        raise AnyMessageApiError(
            f"Confirmation link not found for activation id={activation_id} in {timeout_seconds}s."
        )
