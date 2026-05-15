"""
Metrics Collector - Records health check results and system metrics.
Writes to a local JSON store; swap out the writer for Prometheus/InfluxDB.
"""

import asyncio
import json
import logging
import os
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Deque, Dict, List

import psutil

from monitors.health_checker import HealthResult

logger = logging.getLogger(__name__)

# Max data points kept in memory per target
RING_BUFFER_SIZE = 1000


class MetricsCollector:
    def __init__(self, settings):
        self.settings = settings
        self.metrics_dir = Path(settings.get("metrics.output_dir", "logs/metrics"))
        self.metrics_dir.mkdir(parents=True, exist_ok=True)

        # Ring buffer per target: deque of dicts
        self._health_history: Dict[str, Deque[dict]] = {}
        self._system_history: Deque[dict] = deque(maxlen=RING_BUFFER_SIZE)
        self._pending_flush: List[dict] = []

    # ------------------------------------------------------------------ #
    # Health results                                                       #
    # ------------------------------------------------------------------ #

    async def record_health_result(self, result: HealthResult):
        """Store a health-check result in the ring buffer."""
        entry = {
            "target": result.target_name,
            "url": result.url,
            "status": result.status,
            "status_code": result.status_code,
            "response_time_ms": result.response_time_ms,
            "error": result.error,
            "ts": result.checked_at.isoformat(),
        }
        buf = self._health_history.setdefault(result.target_name, deque(maxlen=RING_BUFFER_SIZE))
        buf.append(entry)
        self._pending_flush.append(entry)

    def get_history(self, target_name: str, last_n: int = 100) -> List[dict]:
        buf = self._health_history.get(target_name, deque())
        return list(buf)[-last_n:]

    def availability(self, target_name: str, last_n: int = 100) -> float:
        """Return uptime percentage over the last N checks."""
        history = self.get_history(target_name, last_n)
        if not history:
            return 100.0
        healthy = sum(1 for h in history if h["status"] == "healthy")
        return round(healthy / len(history) * 100, 2)

    def avg_response_time(self, target_name: str, last_n: int = 100) -> float:
        history = self.get_history(target_name, last_n)
        times = [h["response_time_ms"] for h in history if h["response_time_ms"] is not None]
        return round(sum(times) / len(times), 2) if times else 0.0

    # ------------------------------------------------------------------ #
    # System metrics                                                       #
    # ------------------------------------------------------------------ #

    async def collect_system_metrics(self):
        """Gather host-level CPU / memory / disk metrics."""
        entry = {
            "ts": datetime.utcnow().isoformat(),
            "cpu_percent": psutil.cpu_percent(interval=1),
            "memory": {
                "total_mb": round(psutil.virtual_memory().total / 1024**2),
                "used_mb": round(psutil.virtual_memory().used / 1024**2),
                "percent": psutil.virtual_memory().percent,
            },
            "disk": {
                "total_gb": round(psutil.disk_usage("/").total / 1024**3, 1),
                "used_gb": round(psutil.disk_usage("/").used / 1024**3, 1),
                "percent": psutil.disk_usage("/").percent,
            },
        }
        self._system_history.append(entry)
        logger.debug(f"System metrics: CPU {entry['cpu_percent']}% | MEM {entry['memory']['percent']}%")

    def latest_system_metrics(self) -> dict:
        return self._system_history[-1] if self._system_history else {}

    # ------------------------------------------------------------------ #
    # Persistence                                                          #
    # ------------------------------------------------------------------ #

    async def flush(self):
        """Write pending entries to rotating daily JSONL files."""
        if not self._pending_flush:
            return
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
        path = self.metrics_dir / f"health_{date_str}.jsonl"
        with open(path, "a") as f:
            for entry in self._pending_flush:
                f.write(json.dumps(entry) + "\n")
        self._pending_flush.clear()

        # Also flush system metrics snapshot
        if self._system_history:
            sys_path = self.metrics_dir / f"system_{date_str}.jsonl"
            with open(sys_path, "a") as f:
                f.write(json.dumps(self._system_history[-1]) + "\n")

    def summary(self) -> dict:
        """Return a JSON-serialisable summary for the dashboard."""
        targets = list(self._health_history.keys())
        return {
            "targets": [
                {
                    "name": t,
                    "availability_pct": self.availability(t),
                    "avg_response_time_ms": self.avg_response_time(t),
                    "last_100": self.get_history(t, 100),
                }
                for t in targets
            ],
            "system": self.latest_system_metrics(),
        }
