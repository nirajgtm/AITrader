"""Base class for API provider clients.

Provides:
  - HTTP GET with retry + 429 backoff
  - Sliding-window rate limiter (per-minute + per-day)
  - Cache integration (transparent get-or-fetch)
  - Uniform error handling (returns None on hard failure rather than raising,
    so calling scripts can gracefully surface "data gap")

Subclasses set:
    name: provider name (used as cache namespace)
    base_url: API root
    rate_limit_per_minute: int | None
    rate_limit_per_day: int | None
    api_key_env: name of the env var holding the key
    auth_method: "query" | "bearer" | "header" | None
    auth_param: query/header name to set the key under (e.g. "apikey", "X-Finnhub-Token")
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

# scripts/ is on sys.path when invoked normally; this works for package use too.
_PARENT = Path(__file__).resolve().parent.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from _apikeys import get_key  # noqa: E402
from _cache import cache_get, cache_put  # noqa: E402

_contact = get_key("SEC_CONTACT_EMAIL") or "trader@example.com"
DEFAULT_UA = f"trader-skill/1.2 ({_contact})"  # set SEC_CONTACT_EMAIL in .env


class RateLimitExceeded(Exception):
    pass


class BaseProvider:
    name: str = "base"
    base_url: str = ""
    rate_limit_per_minute: Optional[int] = None
    rate_limit_per_day: Optional[int] = None
    api_key_env: Optional[str] = None
    auth_method: Optional[str] = None  # "query" | "bearer" | "header"
    auth_param: Optional[str] = None
    timeout: int = 20

    # Class-level call-timestamp deque (per process). Provider state is
    # ephemeral per CLI invocation — within a runbook walk we mostly hit cache.
    _call_log: dict[str, list[float]] = {}  # name -> list[ts]

    def __init__(self) -> None:
        self.api_key = get_key(self.api_key_env) if self.api_key_env else None
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": DEFAULT_UA,
            "Accept": "application/json",
        })
        BaseProvider._call_log.setdefault(self.name, [])

    # ---------- rate limiter ----------

    def _check_and_record_call(self) -> None:
        log = BaseProvider._call_log[self.name]
        now = time.time()
        # prune old
        log[:] = [t for t in log if now - t < 86400]

        if self.rate_limit_per_minute is not None:
            count_1m = sum(1 for t in log if now - t < 60)
            if count_1m >= self.rate_limit_per_minute:
                # Wait until the oldest call within window expires + small buffer
                oldest_in_window = min(t for t in log if now - t < 60)
                wait = 60 - (now - oldest_in_window) + 0.5
                if wait > 0:
                    # Cap wait at 65s — should be more than enough for 1-minute
                    # window. If somehow not, raise.
                    time.sleep(min(wait, 65))
                # Re-prune and re-check
                now = time.time()
                log[:] = [t for t in log if now - t < 86400]
                count_1m = sum(1 for t in log if now - t < 60)
                if count_1m >= self.rate_limit_per_minute:
                    raise RateLimitExceeded(
                        f"{self.name}: per-minute limit ({self.rate_limit_per_minute}) hit; "
                        f"backoff insufficient. Try again in 60s.")

        if self.rate_limit_per_day is not None:
            count_1d = sum(1 for t in log if now - t < 86400)
            if count_1d >= self.rate_limit_per_day:
                raise RateLimitExceeded(
                    f"{self.name}: per-day limit ({self.rate_limit_per_day}) reached. "
                    f"Reset at next UTC day.")

        log.append(now)

    # ---------- HTTP ----------

    def _auth(self, params: dict, headers: dict) -> tuple[dict, dict]:
        if not self.api_key or not self.auth_method:
            return params, headers
        if self.auth_method == "query":
            params = {**params, self.auth_param: self.api_key}
        elif self.auth_method == "bearer":
            headers = {**headers, "Authorization": f"Bearer {self.api_key}"}
        elif self.auth_method == "header":
            headers = {**headers, self.auth_param: self.api_key}
        return params, headers

    def get(self, path: str, params: Optional[dict] = None,
            cache_key: Optional[str] = None, cache_ttl: Optional[int] = None,
            extra_headers: Optional[dict] = None) -> Optional[Any]:
        """GET with cache-first + rate-limit + retry. Returns parsed JSON or None."""
        if cache_key:
            ck = f"{self.name}::{cache_key}"
            cached = cache_get(ck, ttl_seconds=cache_ttl or 600)
            if cached is not None:
                return cached

        try:
            self._check_and_record_call()
        except RateLimitExceeded as e:
            print(f"[{self.name}] {e}", file=sys.stderr)
            return None

        params = dict(params or {})
        headers = dict(extra_headers or {})
        params, headers = self._auth(params, headers)

        url = self.base_url.rstrip("/") + "/" + path.lstrip("/")

        # Retry: 429/5xx with backoff
        for attempt in range(3):
            try:
                r = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
            except Exception as e:
                if attempt == 2:
                    print(f"[{self.name}] http error: {e}", file=sys.stderr)
                    return None
                time.sleep(0.5 * (attempt + 1))
                continue

            if r.status_code == 200:
                try:
                    data = r.json()
                except Exception:
                    print(f"[{self.name}] non-JSON response from {url}", file=sys.stderr)
                    return None
                if cache_key:
                    cache_put(f"{self.name}::{cache_key}", data)
                return data

            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 2 + attempt))
                print(f"[{self.name}] 429 at {url}; sleeping {wait}s", file=sys.stderr)
                time.sleep(min(wait, 5))
                continue

            if r.status_code == 401:
                print(f"[{self.name}] 401 at {url} — check API key.", file=sys.stderr)
                return None

            if r.status_code == 403:
                print(f"[{self.name}] 403 at {url} — endpoint may require paid tier.",
                      file=sys.stderr)
                return None

            if r.status_code >= 500:
                time.sleep(0.5 * (attempt + 1))
                continue

            # 4xx other than above
            print(f"[{self.name}] {r.status_code} at {url}: {r.text[:200]}", file=sys.stderr)
            return None

        return None

    # ---------- diagnostics ----------

    def info(self) -> dict:
        return {
            "provider": self.name,
            "base_url": self.base_url,
            "key_present": bool(self.api_key),
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "rate_limit_per_day": self.rate_limit_per_day,
            "calls_made_this_process": len(BaseProvider._call_log.get(self.name, [])),
        }
