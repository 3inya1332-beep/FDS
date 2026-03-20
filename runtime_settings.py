from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from config import (
    ANYMESSAGE_API_BASE,
    ANYMESSAGE_API_TOKEN,
    ANYMESSAGE_SERVICE,
    API_KEY,
    BASE_DIR,
    LOCAL_API_BASE,
)


SETTINGS_PATH = BASE_DIR / "runtime_settings.json"


@dataclass
class RuntimeSettings:
    ads_api_base: str = LOCAL_API_BASE
    ads_api_key: str = API_KEY
    anymessage_api_base: str = ANYMESSAGE_API_BASE
    anymessage_api_token: str = ANYMESSAGE_API_TOKEN
    anymessage_service: str = ANYMESSAGE_SERVICE
    proxy_enabled: bool = False
    proxy_type: str = "http"
    proxy_host: str = ""
    proxy_port: str = ""
    proxy_username: str = ""
    proxy_password: str = ""

    def proxy_config(self) -> dict[str, str] | None:
        if not self.proxy_enabled:
            return None
        host = self.proxy_host.strip()
        port = self.proxy_port.strip()
        if not host or not port:
            return None
        config: dict[str, str] = {
            "proxy_type": (self.proxy_type or "http").strip(),
            "proxy_host": host,
            "proxy_port": port,
        }
        if self.proxy_username.strip():
            config["proxy_username"] = self.proxy_username.strip()
        if self.proxy_password.strip():
            config["proxy_password"] = self.proxy_password.strip()
        return config


class RuntimeSettingsStore:
    def __init__(self, path: Path = SETTINGS_PATH) -> None:
        self.path = path

    def load(self) -> RuntimeSettings:
        if not self.path.exists():
            return RuntimeSettings()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return RuntimeSettings()
        if not isinstance(payload, dict):
            return RuntimeSettings()

        defaults = RuntimeSettings()
        data = asdict(defaults)
        for key in data:
            if key in payload:
                data[key] = payload[key]
        try:
            return RuntimeSettings(**data)
        except Exception:  # noqa: BLE001
            return RuntimeSettings()

    def save(self, settings: RuntimeSettings) -> None:
        self.path.write_text(json.dumps(asdict(settings), ensure_ascii=True, indent=2), encoding="utf-8")

    def update(self, **kwargs: Any) -> RuntimeSettings:
        settings = self.load()
        for key, value in kwargs.items():
            if hasattr(settings, key):
                setattr(settings, key, value)
        self.save(settings)
        return settings
