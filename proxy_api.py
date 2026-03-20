from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests


class ProxyApiError(RuntimeError):
    pass


@dataclass
class ProxyCandidate:
    proxy_soft: str
    proxy_type: str
    proxy_host: str
    proxy_port: str
    proxy_username: str = ""
    proxy_password: str = ""
    ping_ms: float | None = None

    def to_ads_payload(self) -> dict[str, str]:
        payload = {
            "proxy_soft": self.proxy_soft,
            "proxy_type": self.proxy_type,
            "proxy_host": self.proxy_host,
            "proxy_port": self.proxy_port,
        }
        if self.proxy_username:
            payload["proxy_username"] = self.proxy_username
        if self.proxy_password:
            payload["proxy_password"] = self.proxy_password
        return payload


class ProxyApiClient:
    def __init__(self, api_url: str, timeout_seconds: int = 15, desired_candidates: int = 10) -> None:
        self.api_url = api_url.strip()
        self.timeout_seconds = timeout_seconds
        self.desired_candidates = max(1, desired_candidates)

    def _candidate_urls(self) -> list[str]:
        if not self.api_url:
            return []

        parsed = urlparse(self.api_url)
        query_items = dict(parse_qsl(parsed.query, keep_blank_values=True))
        urls = [self.api_url]

        num_value = query_items.get("num")
        if num_value is not None:
            try:
                current_num = int(num_value)
            except ValueError:
                current_num = 1
            if current_num < self.desired_candidates:
                query_items["num"] = str(self.desired_candidates)
                enlarged_query = urlencode(query_items, doseq=True)
                enlarged_url = urlunparse(
                    (parsed.scheme, parsed.netloc, parsed.path, parsed.params, enlarged_query, parsed.fragment)
                )
                # Prefer larger pool first to choose the best ping.
                return [enlarged_url, self.api_url]
        return urls

    @staticmethod
    def _list_from_payload(payload: Any) -> list[Any]:
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []
        for key in ("data", "list", "items", "rows", "proxies", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = ProxyApiClient._list_from_payload(value)
                if nested:
                    return nested
        return [payload]

    @staticmethod
    def _to_float(value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            raw = value.strip().lower().replace("ms", "")
            try:
                return float(raw)
            except ValueError:
                return None
        return None

    @staticmethod
    def _parse_proxy_string(raw: str) -> tuple[str, str, str, str] | None:
        parts = [p.strip() for p in raw.split(":")]
        if len(parts) < 2:
            return None
        host = parts[0]
        port = parts[1]
        username = parts[2] if len(parts) >= 3 else ""
        password = parts[3] if len(parts) >= 4 else ""
        return host, port, username, password

    @staticmethod
    def _normalize_candidate(item: Any) -> ProxyCandidate | None:
        if isinstance(item, str):
            parsed = ProxyApiClient._parse_proxy_string(item)
            if not parsed:
                return None
            host, port, username, password = parsed
            return ProxyCandidate(
                proxy_soft="other",
                proxy_type="http",
                proxy_host=host,
                proxy_port=port,
                proxy_username=username,
                proxy_password=password,
            )

        if not isinstance(item, dict):
            return None

        proxy_type = str(
            item.get("proxy_type")
            or item.get("type")
            or item.get("scheme")
            or item.get("protocol")
            or "http"
        ).strip()
        proxy_soft = str(item.get("proxy_soft") or item.get("soft") or item.get("source") or "other").strip()

        host = str(item.get("proxy_host") or item.get("host") or item.get("ip") or "").strip()
        port = str(item.get("proxy_port") or item.get("port") or "").strip()
        username = str(
            item.get("proxy_username")
            or item.get("username")
            or item.get("user")
            or item.get("login")
            or ""
        ).strip()
        password = str(item.get("proxy_password") or item.get("password") or item.get("pass") or "").strip()

        if (not host or not port) and isinstance(item.get("proxy"), str):
            parsed_proxy = ProxyApiClient._parse_proxy_string(str(item["proxy"]))
            if parsed_proxy:
                host, port, username, password = parsed_proxy

        if not host or not port:
            return None

        ping = None
        for key in ("ping", "latency", "delay", "response_time", "ms", "time"):
            ping = ProxyApiClient._to_float(item.get(key))
            if ping is not None:
                break

        return ProxyCandidate(
            proxy_soft=proxy_soft or "other",
            proxy_type=proxy_type or "http",
            proxy_host=host,
            proxy_port=port,
            proxy_username=username,
            proxy_password=password,
            ping_ms=ping,
        )

    def fetch_best_proxy(self) -> ProxyCandidate:
        urls = self._candidate_urls()
        if not urls:
            raise ProxyApiError("Proxy API URL is empty.")

        errors: list[str] = []
        for url in urls:
            try:
                response = requests.get(url, timeout=self.timeout_seconds)
                response.raise_for_status()
                payload = response.json()
                raw_items = self._list_from_payload(payload)
                candidates = [self._normalize_candidate(item) for item in raw_items]
                valid = [candidate for candidate in candidates if candidate is not None]
                if not valid:
                    errors.append(f"No valid proxy entries in response for url={url}")
                    continue

                with_ping = [item for item in valid if item.ping_ms is not None]
                if with_ping:
                    return min(with_ping, key=lambda c: c.ping_ms if c.ping_ms is not None else 10**9)
                return valid[0]
            except Exception as exc:  # noqa: BLE001
                errors.append(f"url={url}: {exc}")

        raise ProxyApiError("Unable to get proxy from API. " + " | ".join(errors))
