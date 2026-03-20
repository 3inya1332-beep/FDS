from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from config import SENT_EMAILS_DB, SENT_EMAILS_DIR, SENT_EMAILS_TXT


class SentEmailStore:
    def __init__(self, base_dir: Path = SENT_EMAILS_DIR) -> None:
        self.base_dir = base_dir
        self.db_path = self.base_dir / SENT_EMAILS_DB
        self.txt_path = self.base_dir / SENT_EMAILS_TXT
        self.ensure_ready()

    def ensure_ready(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        if not self.txt_path.exists():
            self.txt_path.write_text("", encoding="utf-8")
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sent_emails (
                    email TEXT PRIMARY KEY,
                    sent_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _normalize(email: str) -> str:
        return email.strip().lower()

    def get_all(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT email FROM sent_emails").fetchall()
        return {str(row["email"]) for row in rows}

    def mark_many(self, emails: list[str]) -> int:
        normalized = []
        seen: set[str] = set()
        for email in emails:
            item = self._normalize(email)
            if not item or item in seen:
                continue
            seen.add(item)
            normalized.append(item)

        if not normalized:
            return 0

        inserted: list[str] = []
        now = datetime.utcnow().isoformat(timespec="seconds")
        with self._connect() as conn:
            for email in normalized:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO sent_emails(email, sent_at) VALUES (?, ?)",
                    (email, now),
                )
                if cursor.rowcount > 0:
                    inserted.append(email)
            conn.commit()

        if inserted:
            with self.txt_path.open("a", encoding="utf-8") as file:
                for email in inserted:
                    file.write(f"{email}\n")

        return len(inserted)

