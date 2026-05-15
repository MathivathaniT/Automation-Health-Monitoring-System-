"""
Health Checker - Performs HTTP/TCP health checks on configured targets.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class HealthResult:
    target_name: str
    url: str
    status: str          # "healthy" | "degraded" | "unhealthy"
    status_code: Optional[int]
    response_time_ms: float
    checked_at: datetime
    error: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    @property
    def is_healthy(self) -> bool:
        return self.status == "healthy"


class HealthChecker:
    def __init__(self, settings):
        self.settings = settings
        self.targets = settings.get("targets", [])
        self._state: dict[str, HealthResult] = {}   # last result per target
        self._consecutive_failures: dict[str, int] = {}

    async def check_all(self) -> List[HealthResult]:
        """Run all health checks concurrently."""
        timeout = self.settings.get("monitoring.request_timeout_seconds", 10)
        connector = aiohttp.TCPConnector(limit=50)
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as session:
            tasks = [self._check_target(session, t) for t in self.targets]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        valid = []
        for r in results:
            if isinstance(r, HealthResult):
                self._update_state(r)
                valid.append(r)
            else:
                logger.error(f"Unexpected error during health check: {r}")
        return valid

    async def _check_target(self, session: aiohttp.ClientSession, target: dict) -> HealthResult:
        name = target["name"]
        url = target["url"]
        expected_codes = target.get("expected_status_codes", [200])
        degraded_threshold_ms = target.get("degraded_threshold_ms", 2000)
        headers = target.get("headers", {})

        start = time.monotonic()
        try:
            async with session.get(url, headers=headers, allow_redirects=True) as resp:
                elapsed_ms = (time.monotonic() - start) * 1000
                status_code = resp.status

                if status_code not in expected_codes:
                    status = "unhealthy"
                elif elapsed_ms > degraded_threshold_ms:
                    status = "degraded"
                else:
                    status = "healthy"

                result = HealthResult(
                    target_name=name,
                    url=url,
                    status=status,
                    status_code=status_code,
                    response_time_ms=round(elapsed_ms, 2),
                    checked_at=datetime.utcnow(),
                    metadata={"degraded_threshold_ms": degraded_threshold_ms},
                )
        except asyncio.TimeoutError:
            elapsed_ms = (time.monotonic() - start) * 1000
            result = HealthResult(
                target_name=name, url=url, status="unhealthy",
                status_code=None, response_time_ms=round(elapsed_ms, 2),
                checked_at=datetime.utcnow(), error="Request timed out",
            )
        except Exception as e:
            elapsed_ms = (time.monotonic() - start) * 1000
            result = HealthResult(
                target_name=name, url=url, status="unhealthy",
                status_code=None, response_time_ms=round(elapsed_ms, 2),
                checked_at=datetime.utcnow(), error=str(e),
            )

        logger.info(
            f"[{result.status.upper():9s}] {name} — {result.response_time_ms:.0f}ms "
            + (f"HTTP {result.status_code}" if result.status_code else f"ERR: {result.error}")
        )
        return result

    def _update_state(self, result: HealthResult):
        name = result.target_name
        if result.status != "healthy":
            self._consecutive_failures[name] = self._consecutive_failures.get(name, 0) + 1
        else:
            self._consecutive_failures[name] = 0
        self._state[name] = result

    def get_state(self) -> dict[str, HealthResult]:
        return dict(self._state)

    def consecutive_failures(self, target_name: str) -> int:
        return self._consecutive_failures.get(target_name, 0)
