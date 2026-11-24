"""Bounded in-memory ledger of submitted Rodin jobs for session observability."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import threading
from typing import Any, Callable


@dataclass(frozen=True)
class JobRecord:
    task_uuid: str
    subscription_key: str | None
    endpoint: str
    submitted_at: str
    estimated_credits: float
    actual_credits: float | None = None
    producer: str | None = None


class JobStore:
    """Thread-safe bounded ring buffer of jobs submitted during this server session."""

    def __init__(
        self,
        max_entries: int = 100,
        now_utc: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._records: deque[JobRecord] = deque(maxlen=max_entries)
        self._lock = threading.Lock()
        self._now_utc = now_utc

    def record(
        self,
        *,
        endpoint: str,
        payload: dict[str, Any],
        estimated_credits: float,
    ) -> JobRecord:
        task_uuid = str(payload.get("uuid", "")).strip()
        jobs_obj = payload.get("jobs") or payload.get("job") or {}
        sub_key = jobs_obj.get("subscription_key") if isinstance(jobs_obj, dict) else None
        actual = payload.get("consumed")
        producer = payload.get("producer")
        entry = JobRecord(
            task_uuid=task_uuid,
            subscription_key=str(sub_key) if sub_key else None,
            endpoint=endpoint,
            submitted_at=self._now_utc().isoformat().replace("+00:00", "Z"),
            estimated_credits=estimated_credits,
            actual_credits=float(actual) if isinstance(actual, (int, float)) else None,
            producer=str(producer) if producer else None,
        )
        with self._lock:
            self._records.appendleft(entry)
        return entry

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        if type(limit) is not int or not (1 <= limit <= 100):
            raise ValueError("limit must be an integer between 1 and 100")
        with self._lock:
            items = list(self._records)[:limit]
        return [asdict(item) for item in items]
