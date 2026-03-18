from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import config


@dataclass(slots=True)
class Profile:
    id: int
    title: str
    ads_profile_id: str
    start_url: str
    parent_folder_id: int
    permission_level: int


class ProfileStorage:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path or config.PROFILE_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL UNIQUE,
                    ads_profile_id TEXT NOT NULL UNIQUE,
                    start_url TEXT NOT NULL,
                    parent_folder_id INTEGER NOT NULL DEFAULT 0,
                    permission_level INTEGER NOT NULL DEFAULT 3,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def add_profile(
        self,
        title: str,
        ads_profile_id: str,
        start_url: str,
        parent_folder_id: int,
        permission_level: int,
    ) -> Profile:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO profiles (
                    title,
                    ads_profile_id,
                    start_url,
                    parent_folder_id,
                    permission_level
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    title.strip(),
                    ads_profile_id.strip(),
                    start_url.strip(),
                    int(parent_folder_id),
                    int(permission_level),
                ),
            )
            profile_id = int(cursor.lastrowid)

        return self.get_profile(profile_id)

    def list_profiles(self) -> list[Profile]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    id,
                    title,
                    ads_profile_id,
                    start_url,
                    parent_folder_id,
                    permission_level
                FROM profiles
                ORDER BY id ASC
                """
            ).fetchall()
        return [self._row_to_profile(row) for row in rows]

    def get_profile(self, profile_id: int) -> Profile:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    title,
                    ads_profile_id,
                    start_url,
                    parent_folder_id,
                    permission_level
                FROM profiles
                WHERE id = ?
                """,
                (int(profile_id),),
            ).fetchone()
        if row is None:
            raise ValueError(f"Profile with id={profile_id} not found.")
        return self._row_to_profile(row)

    def delete_profile(self, profile_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM profiles WHERE id = ?",
                (int(profile_id),),
            )
        return cursor.rowcount > 0

    @staticmethod
    def _row_to_profile(row: sqlite3.Row) -> Profile:
        return Profile(
            id=int(row["id"]),
            title=str(row["title"]),
            ads_profile_id=str(row["ads_profile_id"]),
            start_url=str(row["start_url"]),
            parent_folder_id=int(row["parent_folder_id"]),
            permission_level=int(row["permission_level"]),
        )


def format_profiles_table(profiles: Iterable[Profile]) -> str:
    rows = list(profiles)
    if not rows:
        return "No profiles added yet."

    header = (
        f"{'ID':<4} {'Title':<20} {'ADS profile id':<18} "
        f"{'Folder ID':<10} {'Perm':<6} Start URL"
    )
    separator = "-" * len(header)
    body = [
        (
            f"{profile.id:<4} {profile.title[:20]:<20} "
            f"{profile.ads_profile_id[:18]:<18} {profile.parent_folder_id:<10} "
            f"{profile.permission_level:<6} {profile.start_url}"
        )
        for profile in rows
    ]
    return "\n".join([header, separator, *body])
