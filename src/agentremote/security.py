from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque

from .common import AgentRemoteError


@dataclass
class SecurityConfig:
    max_concurrent_requests: int = 32
    unauthenticated_per_minute: int = 60
    authenticated_per_minute: int = 600
    authenticated_transfer_per_minute: int = 120000
    login_failures_per_minute: int = 8
    login_block_seconds: int = 120
    overload_events_per_minute: int = 120
    panic_on_flood: bool = False


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window_seconds = window_seconds
        self.events: dict[str, Deque[float]] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        cutoff = now - self.window_seconds
        with self.lock:
            bucket = self.events[key]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(now)
            return True

    def count(self, key: str, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        cutoff = now - self.window_seconds
        with self.lock:
            bucket = self.events[key]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            return len(bucket)


class SecurityState:
    def __init__(self, config: SecurityConfig | None = None):
        self.config = config or SecurityConfig()
        self.concurrent = threading.BoundedSemaphore(self.config.max_concurrent_requests)
        self.unauthenticated = SlidingWindowLimiter(
            self.config.unauthenticated_per_minute, 60
        )
        self.authenticated = SlidingWindowLimiter(self.config.authenticated_per_minute, 60)
        self.authenticated_transfer = SlidingWindowLimiter(
            self.config.authenticated_transfer_per_minute, 60
        )
        self.login_failures = SlidingWindowLimiter(self.config.login_failures_per_minute, 60)
        self.overload = SlidingWindowLimiter(self.config.overload_events_per_minute, 60)
        self.blocked_until: dict[str, float] = {}
        self.lock = threading.Lock()
        self.flood_shutdown_requested = False

    def acquire_request(self) -> bool:
        return self.concurrent.acquire(blocking=False)

    def release_request(self) -> None:
        try:
            self.concurrent.release()
        except ValueError:
            pass

    def check_rate(self, ip: str, *, authenticated: bool, transfer: bool = False) -> None:
        now = time.time()
        with self.lock:
            until = self.blocked_until.get(ip, 0)
            if until > now:
                raise AgentRemoteError(429, "temporarily_blocked", "Client is temporarily blocked")
            if until:
                self.blocked_until.pop(ip, None)
        if authenticated and transfer:
            limiter = self.authenticated_transfer
        else:
            limiter = self.authenticated if authenticated else self.unauthenticated
        if not limiter.allow(ip, now):
            self.note_overload(ip)
            raise AgentRemoteError(429, "rate_limited", "Too many requests")

    def note_login_failure(self, ip: str) -> None:
        now = time.time()
        self.login_failures.allow(ip, now)
        if self.login_failures.count(ip, now) >= self.config.login_failures_per_minute:
            with self.lock:
                self.blocked_until[ip] = now + self.config.login_block_seconds
            self.note_overload(ip)

    def note_overload(self, ip: str) -> None:
        if not self.overload.allow(ip):
            if self.config.panic_on_flood:
                self.flood_shutdown_requested = True
