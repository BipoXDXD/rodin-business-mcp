"""Fixed-endpoint HTTP client for Hyper3D Rodin Business REST API v2."""

from __future__ import annotations

from contextlib import ExitStack
from email.utils import parsedate_to_datetime
import json
import mimetypes
from pathlib import Path
import time
from typing import Any, Callable

import requests

from .config import ALLOWED_REST_ENDPOINTS, ApiKeyProvider, ServerConfig
from .job_store import JobStore
from .pricing import verify_spend_budget

RequestFn = Callable[..., requests.Response]
SleepFn = Callable[[float], None]
MonotonicFn = Callable[[], float]

MAX_READ_RETRIES = 3
MAX_RETRY_WAIT_SECONDS = 30.0


def encode_form_field(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def _parse_retry_after(response: requests.Response, default_delay: float) -> float:
    raw = response.headers.get("Retry-After") if hasattr(response, "headers") else None
    if not raw:
        return default_delay
    try:
        return max(1.0, min(MAX_RETRY_WAIT_SECONDS, float(int(raw.strip()))))
    except ValueError:
        pass
    try:
        retry_dt = parsedate_to_datetime(raw)
        date_hdr = response.headers.get("Date")
        if date_hdr:
            base_dt = parsedate_to_datetime(date_hdr)
            delta = (retry_dt - base_dt).total_seconds()
            return max(1.0, min(MAX_RETRY_WAIT_SECONDS, delta))
    except (TypeError, ValueError, KeyError):
        pass
    return default_delay


class RodinRestClient:
    """Thin, hardened HTTP adapter for documented REST v2 endpoints.

    - Only fixed allowlisted endpoints are permitted.
    - Billable submissions are NEVER retried automatically to prevent double charges.
    - Idempotent read endpoints (status, download, check_balance, rigging_animations)
      retry HTTP 429 (honoring Retry-After) and transient 5xx responses with backoff.
    """

    def __init__(
        self,
        config: ServerConfig,
        key_provider: ApiKeyProvider,
        job_store: JobStore,
        *,
        request_fn: RequestFn | None = None,
        sleep_fn: SleepFn = time.sleep,
        monotonic_fn: MonotonicFn = time.monotonic,
    ) -> None:
        self.config = config
        self.key_provider = key_provider
        self.job_store = job_store
        self._request_fn = request_fn or requests.request
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn

    def _safe_http_error(self, response: requests.Response, endpoint: str) -> RuntimeError:
        status = getattr(response, "status_code", "unknown")
        detail = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                code = body.get("error") or ""
                msg = body.get("message") or ""
                detail = f"{code}: {msg}".strip(": ")
        except Exception:  # noqa: BLE001
            detail = ""
        if detail:
            return RuntimeError(f"Rodin API HTTP {status} on '{endpoint}': {detail}")
        return RuntimeError(f"Rodin API HTTP {status} on '{endpoint}'")

    def request_raw(
        self,
        method: str,
        endpoint: str,
        *,
        retryable: bool = False,
        **kwargs: Any,
    ) -> Any:
        if endpoint not in ALLOWED_REST_ENDPOINTS:
            raise ValueError(f"Disallowed REST endpoint: {endpoint}")

        url = f"{self.config.api_base_url.rstrip('/')}/{endpoint}"
        headers = {"Authorization": f"Bearer {self.key_provider.get()}"}
        if "headers" in kwargs and kwargs["headers"]:
            headers.update(kwargs.pop("headers"))

        timeout = kwargs.pop("timeout", (self.config.connect_timeout, self.config.read_timeout))
        max_attempts = (MAX_READ_RETRIES + 1) if retryable else 1

        for attempt in range(max_attempts):
            response = self._request_fn(
                method,
                url,
                headers=headers,
                timeout=timeout,
                **kwargs,
            )
            status = getattr(response, "status_code", 200)
            if retryable and isinstance(status, int) and (status == 429 or 500 <= status < 600):
                if attempt + 1 < max_attempts:
                    delay = _parse_retry_after(response, min(MAX_RETRY_WAIT_SECONDS, float(2 ** (attempt + 1))))
                    self._sleep(delay)
                    continue
                raise self._safe_http_error(response, endpoint)

            if isinstance(status, int) and status >= 400:
                raise self._safe_http_error(response, endpoint)

            if hasattr(response, "raise_for_status"):
                response.raise_for_status()
            return response.json()

        raise RuntimeError(f"Exhausted retries for '{endpoint}'")

    def request_dict(
        self,
        method: str,
        endpoint: str,
        *,
        retryable: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload = self.request_raw(method, endpoint, retryable=retryable, **kwargs)
        if not isinstance(payload, dict):
            raise RuntimeError("Unexpected non-object response from Rodin API")
        if payload.get("error"):
            raise RuntimeError(f"Rodin error {payload['error']}: {payload.get('message', '')}".strip())
        return payload

    def submit_paid(
        self,
        endpoint: str,
        fields: dict[str, Any],
        paths: list[tuple[str, Path]],
        estimated_credits: float,
        *,
        approved_spend: bool,
        max_credits: float,
        repeated: list[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Guard spending, build multipart payload, submit once (no retry), and record job."""
        preview = {
            "endpoint": endpoint,
            "estimated_credits": estimated_credits,
            "parameters": {k: v for k, v in fields.items() if v is not None},
            "files": [{"field": field, "name": path.name} for field, path in paths],
        }
        if not approved_spend:
            return {
                "submitted": False,
                "reason": "Explicit approval required (approved_spend=True); no credits were spent.",
                **preview,
            }

        verify_spend_budget(estimated_credits, max_credits)

        parts: list[tuple[str, tuple[Any, ...]]] = [
            (key, (None, encode_form_field(value)))
            for key, value in fields.items()
            if value is not None
        ]
        parts.extend((key, (None, value)) for key, value in (repeated or []))

        with ExitStack() as stack:
            for field, path in paths:
                mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                parts.append((field, (path.name, stack.enter_context(path.open("rb")), mime)))
            payload = self.request_dict("POST", endpoint, retryable=False, files=parts)

        if not payload.get("uuid"):
            raise RuntimeError(
                f"Rodin API did not return a task UUID: {payload.get('message', 'submission unconfirmed')}"
            )

        self.job_store.record(
            endpoint=endpoint,
            payload=payload,
            estimated_credits=estimated_credits,
        )
        return {
            "submitted": True,
            **preview,
            "actual_credits": payload.get("consumed"),
            "result": payload,
        }

    def wait_for_task(
        self,
        subscription_key: str,
        *,
        timeout_seconds: int = 45,
        poll_interval_seconds: int = 5,
        max_interval_seconds: int = 30,
    ) -> dict[str, Any]:
        """Poll /status with linear backoff until all jobs are Done, any job Fails, or deadline hits."""
        if not isinstance(subscription_key, str) or not subscription_key.strip():
            raise ValueError("subscription_key is required")
        if type(timeout_seconds) is not int or not (1 <= timeout_seconds <= 120):
            raise ValueError("timeout_seconds must be an integer in 1..120")
        if type(poll_interval_seconds) is not int or not (1 <= poll_interval_seconds <= 60):
            raise ValueError("poll_interval_seconds must be an integer in 1..60")
        if type(max_interval_seconds) is not int or not (poll_interval_seconds <= max_interval_seconds <= 60):
            raise ValueError("max_interval_seconds must be >= poll_interval_seconds and <= 60")

        started = self._monotonic()
        deadline = started + timeout_seconds
        delay = float(poll_interval_seconds)
        polls = 0
        latest_jobs: list[dict[str, Any]] = []

        while True:
            polls += 1
            data = self.request_dict(
                "POST",
                "status",
                retryable=True,
                json={"subscription_key": subscription_key.strip()},
            )
            latest_jobs = list(data.get("jobs") or [])
            states = [str(job.get("status", "")) for job in latest_jobs if isinstance(job, dict)]

            if "Failed" in states:
                return {
                    "state": "Failed",
                    "done": False,
                    "failed": True,
                    "timed_out": False,
                    "polls": polls,
                    "elapsed_seconds": round(self._monotonic() - started, 2),
                    "jobs": latest_jobs,
                }
            if states and all(state == "Done" for state in states):
                return {
                    "state": "Done",
                    "done": True,
                    "failed": False,
                    "timed_out": False,
                    "polls": polls,
                    "elapsed_seconds": round(self._monotonic() - started, 2),
                    "jobs": latest_jobs,
                }

            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return {
                    "state": "Pending",
                    "done": False,
                    "failed": False,
                    "timed_out": True,
                    "polls": polls,
                    "elapsed_seconds": round(self._monotonic() - started, 2),
                    "jobs": latest_jobs,
                    "note": "Polling timed out; the task is still running. Do not resubmit—poll again later.",
                }

            self._sleep(min(delay, remaining))
            delay = min(delay + 5.0, float(max_interval_seconds))
