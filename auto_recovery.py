"""
Auto Recovery - Attempts to restart unhealthy services via configured strategies.
Supports: HTTP callback, shell command, Docker restart, systemd restart.
"""

import asyncio
import logging
import subprocess
import time
from dataclasses import dataclass, field
from typing import Dict

import aiohttp

from monitors.health_checker import HealthResult

logger = logging.getLogger(__name__)

# Cooldown between recovery attempts per target (seconds)
DEFAULT_COOLDOWN = 120


@dataclass
class RecoveryAttempt:
    target_name: str
    strategy: str
    attempted_at: float
    success: bool
    output: str = ""


class AutoRecovery:
    def __init__(self, settings):
        self.settings = settings
        self.recovery_cfg: dict = settings.get("recovery", {})
        self._last_attempt: Dict[str, float] = {}
        self._attempt_counts: Dict[str, int] = {}
        self._history: list[RecoveryAttempt] = []

    async def attempt_recovery(self, result: HealthResult):
        """Attempt recovery for an unhealthy target if cooldown has elapsed."""
        name = result.target_name
        target_cfg = self._get_target_cfg(name)
        if not target_cfg:
            return

        max_attempts = target_cfg.get("max_attempts", 3)
        cooldown = target_cfg.get("cooldown_seconds", DEFAULT_COOLDOWN)

        if self._attempt_counts.get(name, 0) >= max_attempts:
            logger.warning(f"[RECOVERY] Max attempts reached for {name}, giving up.")
            return

        now = time.time()
        if now - self._last_attempt.get(name, 0) < cooldown:
            return  # Still in cooldown

        strategy = target_cfg.get("strategy", "none")
        logger.info(f"[RECOVERY] Attempting '{strategy}' recovery for {name}")
        self._last_attempt[name] = now
        self._attempt_counts[name] = self._attempt_counts.get(name, 0) + 1

        attempt = await self._execute_strategy(name, strategy, target_cfg)
        self._history.append(attempt)

        if attempt.success:
            logger.info(f"[RECOVERY] ✅ {name} recovery succeeded.")
            self._attempt_counts[name] = 0  # Reset on success
        else:
            logger.error(f"[RECOVERY] ❌ {name} recovery failed: {attempt.output}")

    async def _execute_strategy(self, name: str, strategy: str, cfg: dict) -> RecoveryAttempt:
        success, output = False, ""
        try:
            if strategy == "http_callback":
                success, output = await self._http_callback(cfg)
            elif strategy == "shell_command":
                success, output = self._shell_command(cfg)
            elif strategy == "docker_restart":
                success, output = self._docker_restart(cfg)
            elif strategy == "systemd_restart":
                success, output = self._systemd_restart(cfg)
            else:
                output = f"Unknown strategy: {strategy}"
        except Exception as e:
            output = str(e)

        return RecoveryAttempt(
            target_name=name,
            strategy=strategy,
            attempted_at=time.time(),
            success=success,
            output=output,
        )

    async def _http_callback(self, cfg: dict):
        url = cfg["callback_url"]
        method = cfg.get("method", "POST").upper()
        async with aiohttp.ClientSession() as session:
            fn = getattr(session, method.lower())
            async with fn(url, json=cfg.get("payload", {})) as resp:
                text = await resp.text()
                return resp.status < 400, f"HTTP {resp.status}: {text[:200]}"

    def _shell_command(self, cfg: dict):
        cmd = cfg["command"]
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        output = result.stdout + result.stderr
        return result.returncode == 0, output[:500]

    def _docker_restart(self, cfg: dict):
        container = cfg["container_name"]
        return self._shell_command({"command": f"docker restart {container}"})

    def _systemd_restart(self, cfg: dict):
        service = cfg["service_name"]
        return self._shell_command({"command": f"systemctl restart {service}"})

    def _get_target_cfg(self, name: str) -> dict | None:
        targets = self.recovery_cfg.get("targets", [])
        for t in targets:
            if t["name"] == name:
                return t
        return None

    def history(self) -> list:
        return [
            {
                "target": a.target_name,
                "strategy": a.strategy,
                "success": a.success,
                "output": a.output,
                "ts": a.attempted_at,
            }
            for a in self._history[-50:]
        ]
