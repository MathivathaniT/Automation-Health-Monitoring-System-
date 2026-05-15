"""
Health Monitor - Main Orchestrator
Coordinates health checks, alerting, metrics, and auto-recovery.
"""

import asyncio
import logging
import signal
import sys
from datetime import datetime
from pathlib import Path

from monitors.health_checker import HealthChecker
from monitors.metrics_collector import MetricsCollector
from alerts.alert_manager import AlertManager
from recovery.auto_recovery import AutoRecovery
from dashboard.server import DashboardServer
from config.settings import Settings

logger = logging.getLogger(__name__)


class HealthMonitor:
    def __init__(self, config_path: str = "config/config.yaml"):
        self.settings = Settings(config_path)
        self.health_checker = HealthChecker(self.settings)
        self.metrics_collector = MetricsCollector(self.settings)
        self.alert_manager = AlertManager(self.settings)
        self.auto_recovery = AutoRecovery(self.settings)
        self.dashboard = DashboardServer(self.settings)
        self._running = False

    async def start(self):
        """Start all monitoring components."""
        self._running = True
        logger.info("🚀 Starting Health Monitor...")

        tasks = [
            asyncio.create_task(self._run_health_checks()),
            asyncio.create_task(self._run_metrics_collection()),
            asyncio.create_task(self.dashboard.start()),
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Monitor shutting down...")
        finally:
            await self.cleanup()

    async def _run_health_checks(self):
        """Periodically run health checks on all configured targets."""
        interval = self.settings.get("monitoring.check_interval_seconds", 30)
        while self._running:
            try:
                results = await self.health_checker.check_all()
                for result in results:
                    # Store in metrics
                    await self.metrics_collector.record_health_result(result)
                    # Trigger alerts if needed
                    await self.alert_manager.process_result(result)
                    # Attempt recovery if needed
                    if result.status == "unhealthy":
                        await self.auto_recovery.attempt_recovery(result)
            except Exception as e:
                logger.error(f"Error during health check cycle: {e}")
            await asyncio.sleep(interval)

    async def _run_metrics_collection(self):
        """Periodically collect and flush metrics."""
        interval = self.settings.get("monitoring.metrics_interval_seconds", 60)
        while self._running:
            try:
                await self.metrics_collector.collect_system_metrics()
                await self.metrics_collector.flush()
            except Exception as e:
                logger.error(f"Error collecting metrics: {e}")
            await asyncio.sleep(interval)

    async def cleanup(self):
        """Gracefully shut down all components."""
        logger.info("Cleaning up resources...")
        await self.metrics_collector.flush()
        await self.dashboard.stop()

    def stop(self):
        self._running = False


def setup_logging(log_level: str = "INFO", log_dir: str = "logs"):
    Path(log_dir).mkdir(exist_ok=True)
    log_format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"{log_dir}/monitor.log"),
    ]
    logging.basicConfig(level=getattr(logging, log_level.upper()), format=log_format, handlers=handlers)


async def main():
    setup_logging()
    monitor = HealthMonitor()

    # Handle graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, monitor.stop)

    await monitor.start()


if __name__ == "__main__":
    asyncio.run(main())
