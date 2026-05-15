"""
Dashboard Server - Serves the web UI and JSON API for monitoring data.
"""

import json
import logging
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class DashboardServer:
    def __init__(self, settings):
        self.settings = settings
        self.port = settings.get("dashboard.port", 8080)
        self.host = settings.get("dashboard.host", "0.0.0.0")
        self._app: web.Application = web.Application()
        self._runner: web.AppRunner | None = None

        # These are injected after construction by monitor.py
        self.health_checker = None
        self.metrics_collector = None
        self.auto_recovery = None

        self._setup_routes()

    def _setup_routes(self):
        self._app.router.add_get("/", self._serve_index)
        self._app.router.add_get("/api/status", self._api_status)
        self._app.router.add_get("/api/metrics", self._api_metrics)
        self._app.router.add_get("/api/recovery", self._api_recovery)
        self._app.router.add_get("/api/history/{target}", self._api_history)

        if STATIC_DIR.exists():
            self._app.router.add_static("/static", STATIC_DIR)

    # ------------------------------------------------------------------ #
    # Route handlers                                                       #
    # ------------------------------------------------------------------ #

    async def _serve_index(self, request):
        html_path = STATIC_DIR / "index.html"
        if html_path.exists():
            return web.FileResponse(html_path)
        return web.Response(text="Dashboard UI not found. Use /api/status.", content_type="text/plain")

    async def _api_status(self, request):
        if not self.health_checker:
            return web.json_response({"error": "not initialised"}, status=503)
        state = self.health_checker.get_state()
        data = {
            name: {
                "status": r.status,
                "status_code": r.status_code,
                "response_time_ms": r.response_time_ms,
                "checked_at": r.checked_at.isoformat(),
                "error": r.error,
                "consecutive_failures": self.health_checker.consecutive_failures(name),
            }
            for name, r in state.items()
        }
        return web.json_response({"targets": data})

    async def _api_metrics(self, request):
        if not self.metrics_collector:
            return web.json_response({"error": "not initialised"}, status=503)
        return web.json_response(self.metrics_collector.summary())

    async def _api_recovery(self, request):
        if not self.auto_recovery:
            return web.json_response({"error": "not initialised"}, status=503)
        return web.json_response({"history": self.auto_recovery.history()})

    async def _api_history(self, request):
        target = request.match_info["target"]
        last_n = int(request.query.get("n", 100))
        if not self.metrics_collector:
            return web.json_response({"error": "not initialised"}, status=503)
        history = self.metrics_collector.get_history(target, last_n)
        availability = self.metrics_collector.availability(target, last_n)
        avg_rt = self.metrics_collector.avg_response_time(target, last_n)
        return web.json_response({
            "target": target,
            "availability_pct": availability,
            "avg_response_time_ms": avg_rt,
            "history": history,
        })

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    async def start(self):
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info(f"📊 Dashboard running at http://{self.host}:{self.port}")
        # Keep alive (run forever until cancelled)
        import asyncio
        while True:
            await asyncio.sleep(3600)

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()
