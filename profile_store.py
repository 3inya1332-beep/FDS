from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from config import CALENDLY_MEETING_TYPES_URL, PROFILE_DB_PATH


@dataclass
class Profile:
    local_id: int
    name: str
    ads_profile_id: str
    login_email: str
    password: str
    main_page_url: str
    booking_url: str
    cookie_file: str
    anymessage_activation_id: str
    status: str
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
                    login_email TEXT NOT NULL DEFAULT '',
                    password TEXT NOT NULL DEFAULT '',
                    main_page_url TEXT NOT NULL DEFAULT '',
                    booking_url TEXT NOT NULL DEFAULT '',
                    cookie_file TEXT NOT NULL DEFAULT '',
                    anymessage_activation_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'new',
                    created_at TEXT NOT NULL
                )
                """
            )
            existing_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(profiles)").fetchall()
            }
            # Lightweight migration from older schema.
            migrations: list[tuple[str, str]] = [
                ("login_email", "TEXT NOT NULL DEFAULT ''"),
                ("password", "TEXT NOT NULL DEFAULT ''"),
                ("main_page_url", "TEXT NOT NULL DEFAULT ''"),
                ("booking_url", "TEXT NOT NULL DEFAULT ''"),
                ("cookie_file", "TEXT NOT NULL DEFAULT ''"),
                ("anymessage_activation_id", "TEXT NOT NULL DEFAULT ''"),
                ("status", "TEXT NOT NULL DEFAULT 'new'"),
            ]
            for column, definition in migrations:
                if column not in existing_columns:
                    conn.execute(f"ALTER TABLE profiles ADD COLUMN {column} {definition}")

            if "start_url" in existing_columns and "main_page_url" in existing_columns:
                conn.execute(
                    """
                    UPDATE profiles
                    SET main_page_url = CASE
                        WHEN TRIM(main_page_url) = '' THEN start_url
                        ELSE main_page_url
                    END
                    """
                )
            conn.execute(
                """
                UPDATE profiles
                SET main_page_url = ?
                WHERE TRIM(main_page_url) = ''
                """,
                (CALENDLY_MEETING_TYPES_URL,),
            )
            conn.commit()

    def add_profile(
        self,
        *,
        name: str,
        ads_profile_id: str,
        login_email: str = "",
        password: str = "",
        main_page_url: str = CALENDLY_MEETING_TYPES_URL,
        booking_url: str = "",
        cookie_file: str = "",
        anymessage_activation_id: str = "",
        status: str = "new",
    ) -> None:
        created_at = datetime.utcnow().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO profiles(
                    name, ads_profile_id, login_email, password, main_page_url, booking_url,
                    cookie_file, anymessage_activation_id, status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name.strip(),
                    ads_profile_id.strip(),
                    login_email.strip(),
                    password.strip(),
                    main_page_url.strip() or CALENDLY_MEETING_TYPES_URL,
                    booking_url.strip(),
                    cookie_file.strip(),
                    anymessage_activation_id.strip(),
                    status.strip() or "new",
                    created_at,
                ),
            )
            conn.commit()

    def update_profile(self, local_id: int, **fields: str) -> bool:
        if not fields:
            return False

        allowed = {
            "name",
            "ads_profile_id",
            "login_email",
            "password",
            "main_page_url",
            "booking_url",
            "cookie_file",
            "anymessage_activation_id",
            "status",
        }
        filtered = {key: value for key, value in fields.items() if key in allowed}
        if not filtered:
            return False

        columns = ", ".join([f"{key} = ?" for key in filtered])
        values = [str(value).strip() for value in filtered.values()]
        values.append(local_id)

        with self._connect() as conn:
            cursor = conn.execute(f"UPDATE profiles SET {columns} WHERE id = ?", values)
            conn.commit()
        return cursor.rowcount > 0

    def list_profiles(self) -> Iterable[Profile]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM profiles ORDER BY id ASC").fetchall()
        return [
            Profile(
                local_id=int(row["id"]),
                name=str(row["name"]),
                ads_profile_id=str(row["ads_profile_id"]),
                login_email=str(row["login_email"]),
                password=str(row["password"]),
                main_page_url=str(row["main_page_url"]),
                booking_url=str(row["booking_url"]),
                cookie_file=str(row["cookie_file"]),
                anymessage_activation_id=str(row["anymessage_activation_id"]),
                status=str(row["status"]),
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
            login_email=str(row["login_email"]),
            password=str(row["password"]),
            main_page_url=str(row["main_page_url"]),
            booking_url=str(row["booking_url"]),
            cookie_file=str(row["cookie_file"]),
            anymessage_activation_id=str(row["anymessage_activation_id"]),
            status=str(row["status"]),
            created_at=str(row["created_at"]),
        )

    def delete_profile(self, local_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM profiles WHERE id = ?", (local_id,))
            conn.commit()
        return cursor.rowcount > 0
