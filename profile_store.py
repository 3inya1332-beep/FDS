from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from config import PROFILE_DB_PATH


@dataclass
class Profile:
    local_id: int
    name: str
    ads_profile_id: str
    start_url: str
    created_at: str


class ProfileStore:
    def __init__(self, db_path: Path = PROFILE_DB_PATH) -> None:
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    ads_profile_id TEXT NOT NULL UNIQUE,
                    start_url TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def add_profile(self, name: str, ads_profile_id: str, start_url: str) -> None:
        created_at = datetime.utcnow().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO profiles(name, ads_profile_id, start_url, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (name.strip(), ads_profile_id.strip(), start_url.strip(), created_at),
            )
            conn.commit()

    def list_profiles(self) -> Iterable[Profile]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM profiles ORDER BY id ASC").fetchall()
        return [
            Profile(
                local_id=int(row["id"]),
                name=str(row["name"]),
                ads_profile_id=str(row["ads_profile_id"]),
                start_url=str(row["start_url"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def get_profile(self, local_id: int) -> Profile | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM profiles WHERE id = ?",
                (local_id,),
            ).fetchone()
        if row is None:
            return None
        return Profile(
            local_id=int(row["id"]),
            name=str(row["name"]),
            ads_profile_id=str(row["ads_profile_id"]),
            start_url=str(row["start_url"]),
            created_at=str(row["created_at"]),
        )

    def delete_profile(self, local_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM profiles WHERE id = ?", (local_id,))
            conn.commit()
        return cursor.rowcount > 0

    def update_profile_credentials(self, local_id: int, ads_profile_id: str, start_url: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE profiles
                SET ads_profile_id = ?, start_url = ?
                WHERE id = ?
                """,
                (ads_profile_id.strip(), start_url.strip(), local_id),
            )
            conn.commit()
        return cursor.rowcount > 0
