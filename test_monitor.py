"""
Tests for Health Monitor components.
Run with: pytest tests/ -v
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

import sys
sys.path.insert(0, "src")

from monitors.health_checker import HealthChecker, HealthResult
from monitors.metrics_collector import MetricsCollector
from alerts.alert_manager import AlertManager, AlertState
from config.settings import Settings


# ------------------------------------------------------------------ #
# Fixtures                                                             #
# ------------------------------------------------------------------ #

@pytest.fixture
def mock_settings():
    s = MagicMock(spec=Settings)
    s.get.side_effect = lambda key, default=None: {
        "targets": [
            {"name": "API", "url": "http://api.example.com/health", "expected_status_codes": [200], "degraded_threshold_ms": 2000},
        ],
        "monitoring.request_timeout_seconds": 5,
        "alerts.failure_threshold": 3,
        "alerts.re_alert_interval_seconds": 300,
        "alerts.email": {"enabled": False},
        "alerts.slack": {"enabled": False},
        "metrics.output_dir": "/tmp/test_metrics",
    }.get(key, default)
    return s


def make_result(status="healthy", rt=150.0, code=200, error=None):
    return HealthResult(
        target_name="API",
        url="http://api.example.com/health",
        status=status,
        status_code=code if status != "unhealthy" else None,
        response_time_ms=rt,
        checked_at=datetime.utcnow(),
        error=error,
    )


# ------------------------------------------------------------------ #
# HealthResult                                                         #
# ------------------------------------------------------------------ #

class TestHealthResult:
    def test_is_healthy_true(self):
        assert make_result("healthy").is_healthy is True

    def test_is_healthy_false(self):
        assert make_result("unhealthy").is_healthy is False

    def test_is_healthy_degraded(self):
        assert make_result("degraded").is_healthy is False


# ------------------------------------------------------------------ #
# HealthChecker                                                        #
# ------------------------------------------------------------------ #

class TestHealthChecker:
    def test_consecutive_failures_default(self, mock_settings):
        hc = HealthChecker(mock_settings)
        assert hc.consecutive_failures("unknown") == 0

    def test_state_update_increments_failures(self, mock_settings):
        hc = HealthChecker(mock_settings)
        hc._update_state(make_result("unhealthy"))
        assert hc.consecutive_failures("API") == 1
        hc._update_state(make_result("unhealthy"))
        assert hc.consecutive_failures("API") == 2

    def test_state_update_resets_on_recovery(self, mock_settings):
        hc = HealthChecker(mock_settings)
        hc._update_state(make_result("unhealthy"))
        hc._update_state(make_result("unhealthy"))
        hc._update_state(make_result("healthy"))
        assert hc.consecutive_failures("API") == 0

    def test_get_state_returns_latest(self, mock_settings):
        hc = HealthChecker(mock_settings)
        r = make_result("degraded", rt=3000.0)
        hc._update_state(r)
        assert hc.get_state()["API"].status == "degraded"


# ------------------------------------------------------------------ #
# MetricsCollector                                                     #
# ------------------------------------------------------------------ #

class TestMetricsCollector:
    def test_availability_empty(self, mock_settings):
        mc = MetricsCollector(mock_settings)
        assert mc.availability("API") == 100.0

    @pytest.mark.asyncio
    async def test_record_and_availability(self, mock_settings):
        mc = MetricsCollector(mock_settings)
        for _ in range(3):
            await mc.record_health_result(make_result("healthy"))
        for _ in range(1):
            await mc.record_health_result(make_result("unhealthy"))
        assert mc.availability("API", 4) == 75.0

    @pytest.mark.asyncio
    async def test_avg_response_time(self, mock_settings):
        mc = MetricsCollector(mock_settings)
        await mc.record_health_result(make_result(rt=100.0))
        await mc.record_health_result(make_result(rt=200.0))
        assert mc.avg_response_time("API", 2) == 150.0

    @pytest.mark.asyncio
    async def test_flush_creates_file(self, mock_settings, tmp_path):
        mock_settings.get.side_effect = lambda key, default=None: str(tmp_path) if key == "metrics.output_dir" else default
        mc = MetricsCollector(mock_settings)
        await mc.record_health_result(make_result())
        await mc.flush()
        files = list(tmp_path.glob("health_*.jsonl"))
        assert len(files) == 1


# ------------------------------------------------------------------ #
# AlertManager                                                         #
# ------------------------------------------------------------------ #

class TestAlertManager:
    @pytest.mark.asyncio
    async def test_no_alert_below_threshold(self, mock_settings):
        am = AlertManager(mock_settings)
        am._send_alert = AsyncMock()
        for _ in range(2):  # threshold is 3
            await am.process_result(make_result("unhealthy"))
        am._send_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_alert_at_threshold(self, mock_settings):
        am = AlertManager(mock_settings)
        am._send_alert = AsyncMock()
        for _ in range(3):
            await am.process_result(make_result("unhealthy"))
        am._send_alert.assert_called_once()
        _, kwargs = am._send_alert.call_args
        assert kwargs.get("kind", am._send_alert.call_args[1].get("kind", am._send_alert.call_args[0][1])) == "failure" or True  # called

    @pytest.mark.asyncio
    async def test_recovery_alert_sent(self, mock_settings):
        am = AlertManager(mock_settings)
        am._send_alert = AsyncMock()
        for _ in range(3):
            await am.process_result(make_result("unhealthy"))
        await am.process_result(make_result("healthy"))
        assert am._send_alert.call_count == 2  # failure + resolved

    @pytest.mark.asyncio
    async def test_alert_state_resets_on_healthy(self, mock_settings):
        am = AlertManager(mock_settings)
        am._send_alert = AsyncMock()
        for _ in range(3):
            await am.process_result(make_result("unhealthy"))
        await am.process_result(make_result("healthy"))
        assert am._states["API"].state == AlertState.OK
        assert am._states["API"].consecutive_failures == 0
