from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.base import RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

import app
from app.logging import Ansi
from app.logging import log
from app.logging import magnitude_fmt_time


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        start_time = time.perf_counter_ns()
        response = await call_next(request)
        end_time = time.perf_counter_ns()

        time_elapsed = end_time - start_time

        col = Ansi.LGREEN if response.status_code < 400 else Ansi.LRED

        url = f"{request.headers['host']}{request['path']}"

        log(
            f"[{request.method}] {response.status_code} {url}{Ansi.RESET!r} | {Ansi.LBLUE!r}Request took: {magnitude_fmt_time(time_elapsed)}",
            col,
        )

        response.headers["process-time"] = str(round(time_elapsed) / 1e6)
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP token-bucket rate limiter.

    - api.* subdomain : up to API_MAX_PER_SECOND requests/s per IP
    - login/register  : up to AUTH_MAX_PER_MINUTE requests/min per IP
      (brute-force protection)
    """

    API_MAX_PER_SECOND: int = 30
    AUTH_MAX_PER_MINUTE: int = 10
    AUTH_PATHS: frozenset[str] = frozenset(
        {"/users/login", "/web/login.php", "/users/register"},
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        # {ip_str: [tokens, last_refill_time]}
        self._api_buckets: dict[str, list[float]] = {}
        # {ip_str: [attempt_count, window_start_time]}
        self._auth_windows: dict[str, list[float]] = {}

    def _api_allowed(self, ip: str) -> bool:
        now = time.time()
        if ip not in self._api_buckets:
            self._api_buckets[ip] = [float(self.API_MAX_PER_SECOND - 1), now]
            return True
        tokens, last = self._api_buckets[ip]
        tokens = min(
            float(self.API_MAX_PER_SECOND),
            tokens + (now - last) * self.API_MAX_PER_SECOND,
        )
        self._api_buckets[ip][1] = now
        if tokens < 1.0:
            return False
        self._api_buckets[ip][0] = tokens - 1.0
        return True

    def _auth_allowed(self, ip: str) -> bool:
        now = time.time()
        if ip not in self._auth_windows:
            self._auth_windows[ip] = [1.0, now]
            return True
        count, window_start = self._auth_windows[ip]
        if now - window_start >= 60.0:
            self._auth_windows[ip] = [1.0, now]
            return True
        count += 1.0
        if count > self.AUTH_MAX_PER_MINUTE:
            return False
        self._auth_windows[ip][0] = count
        return True

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        host = request.headers.get("host", "")
        ip = str(app.state.services.ip_resolver.get_ip(request.headers))

        # API subdomain – per-IP token bucket
        if host.startswith("api.") and not self._api_allowed(ip):
            log(f"Rate limit [API] IP={ip} path={request.url.path}", Ansi.LRED)
            return Response("Too Many Requests", status_code=429)

        # Login / register – per-IP sliding window (brute-force protection)
        if request.url.path in self.AUTH_PATHS and not self._auth_allowed(ip):
            log(f"Rate limit [AUTH] IP={ip} path={request.url.path}", Ansi.LRED)
            return Response("Too Many Requests", status_code=429)

        return await call_next(request)