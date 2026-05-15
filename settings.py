"""
Settings - Loads and provides typed access to configuration.
"""

import logging
import os
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class Settings:
    def __init__(self, config_path: str = "config/config.yaml"):
        self._data: dict = {}
        self._load(config_path)
        self._apply_env_overrides()

    def _load(self, path: str):
        p = Path(path)
        if not p.exists():
            logger.warning(f"Config file not found at {path}; using defaults.")
            return
        with open(p) as f:
            self._data = yaml.safe_load(f) or {}
        logger.info(f"Loaded config from {path}")

    def _apply_env_overrides(self):
        """Allow environment variables to override config values.

        Convention:  HM__SECTION__KEY=value  → settings["section"]["key"] = value
        Example:     HM__ALERTS__EMAIL__PASSWORD=secret
        """
        prefix = "HM__"
        for k, v in os.environ.items():
            if not k.startswith(prefix):
                continue
            parts = k[len(prefix):].lower().split("__")
            node = self._data
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = v

    def get(self, key: str, default: Any = None) -> Any:
        """Dot-notation key access, e.g. 'alerts.email.smtp_host'."""
        parts = key.split(".")
        node = self._data
        for part in parts:
            if not isinstance(node, dict):
                return default
            node = node.get(part)
            if node is None:
                return default
        return node

    def all(self) -> dict:
        return dict(self._data)
