import hashlib
import math
import sqlite3
import time
from pathlib import Path

from .config import Settings
from .errors import NmsError


class RateLimiter:
    """Atomic reservations persist across restarts and workers sharing the same file."""

    def __init__(self, settings: Settings):
        self.path: Path = settings.service.state_file
        self.path.parent.mkdir(parents=True, exist_ok=True)
        identity = settings.faa.environment_url + "\0" + settings.faa.key.get_secret_value()
        self.namespace = hashlib.sha256(identity.encode()).hexdigest()
        self.intervals = {
            "data": settings.data_interval,
            "content": settings.limits.content_interval_seconds,
            "delta": settings.limits.delta_interval_seconds,
            "bulk": settings.limits.bulk_interval_seconds,
        }
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS limits (key TEXT PRIMARY KEY, until REAL)")

    def reserve(self, *buckets: str) -> None:
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            wait = 0.0
            for bucket in ("upstream", *buckets):
                row = db.execute(
                    "SELECT until FROM limits WHERE key = ?", (self.namespace + bucket,)
                ).fetchone()
                if row:
                    wait = max(wait, row[0] - now)
            if wait > 0:
                raise NmsError(
                    "FAA request interval has not elapsed. Retry after the indicated delay.",
                    status_code=429,
                    retry_after=math.ceil(wait),
                )
            for bucket in buckets:
                db.execute(
                    "INSERT OR REPLACE INTO limits VALUES (?, ?)",
                    (self.namespace + bucket, now + self.intervals[bucket]),
                )

    def defer(self, seconds: int) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO limits VALUES (?, ?) ON CONFLICT(key) DO UPDATE "
                "SET until = MAX(until, excluded.until)",
                (self.namespace + "upstream", time.time() + seconds),
            )
