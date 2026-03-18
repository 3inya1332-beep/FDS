from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

import config


class PCloudApiError(RuntimeError):
    def __init__(self, result_code: int, message: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.result_code = result_code
        self.payload = payload or {}


@dataclass(slots=True)
class ShareAttempt:
    email: str
    status: str
    detail: str
    result_code: int = 0


PERMISSION_PRESETS = {
    "view": 0,
    "read": 0,
    "edit": 3,
    "manage": 7,
}

SKIPPED_SHARE_CODES = {
    2019,  # Share request already exists
    2024,  # User already has access
}


def parse_permission_level(value: str | int) -> int:
    if isinstance(value, int):
        return value

    normalized = value.strip().lower()
    if normalized in PERMISSION_PRESETS:
        return PERMISSION_PRESETS[normalized]

    return int(normalized)


class PCloudClient:
    def __init__(
        self,
        auth_token: str,
        location_id: int | None = None,
        timeout: int | None = None,
    ) -> None:
        self.auth_token = auth_token.strip()
        self.location_id = int(location_id or config.DEFAULT_LOCATION_ID)
        self.timeout = timeout or config.REQUEST_TIMEOUT
        self.host = config.PCLOUD_API_HOSTS.get(self.location_id, config.PCLOUD_API_HOSTS[config.DEFAULT_LOCATION_ID])
        self.base_url = f"https://{self.host}"

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        request_params = {"auth": self.auth_token, **params}
        response = requests.get(
            f"{self.base_url}/{method}",
            params=request_params,
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        result = int(payload.get("result", 0))
        if result != 0:
            message = str(payload.get("error") or payload.get("message") or f"pCloud error {result}")
            raise PCloudApiError(result, message, payload)
        return payload

    def userinfo(self) -> dict[str, Any]:
        return self.call("userinfo")

    def list_folder(self, folder_id: int) -> dict[str, Any]:
        return self.call("listfolder", folderid=int(folder_id))

    def create_folder(self, folder_name: str, parent_folder_id: int = 0) -> dict[str, Any]:
        try:
            return self.call(
                "createfolderifnotexists",
                folderid=int(parent_folder_id),
                name=folder_name,
            )
        except PCloudApiError:
            pass

        try:
            return self.call(
                "createfolder",
                folderid=int(parent_folder_id),
                name=folder_name,
            )
        except PCloudApiError as exc:
            if exc.result_code != 2004:
                raise

        listing = self.list_folder(parent_folder_id)
        metadata = listing.get("metadata") or {}
        contents = metadata.get("contents") or []
        for item in contents:
            if item.get("isfolder") and item.get("name") == folder_name:
                return {"result": 0, "metadata": item}

        raise RuntimeError(f"Folder {folder_name!r} already exists but could not be resolved.")

    def share_folder(
        self,
        folder_id: int,
        email: str,
        permissions: int,
        message: str = "",
        share_name: str | None = None,
    ) -> ShareAttempt:
        params: dict[str, Any] = {
            "folderid": int(folder_id),
            "mail": email,
            "permissions": int(permissions),
        }
        if message:
            params["message"] = message
        if share_name:
            params["name"] = share_name

        try:
            self.call("sharefolder", **params)
        except PCloudApiError as exc:
            if exc.result_code in SKIPPED_SHARE_CODES:
                return ShareAttempt(
                    email=email,
                    status="skipped",
                    detail=str(exc),
                    result_code=exc.result_code,
                )
            raise

        return ShareAttempt(
            email=email,
            status="success",
            detail="Invitation sent.",
            result_code=0,
        )
