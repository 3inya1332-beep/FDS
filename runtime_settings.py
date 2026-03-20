from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from config import (
    ANYMESSAGE_API_TOKEN,
    API_KEY,
    BASE_DIR,
)


SETTINGS_PATH = BASE_DIR / "runtime_settings.json"


@dataclass
class RuntimeSettings:
    ads_api_key: str = API_KEY
    anymessage_api_token: str = ANYMESSAGE_API_TOKEN
    proxy_api_url: str = "http://127.0.0.1:10101/api/proxy?t=2&num=1"


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
