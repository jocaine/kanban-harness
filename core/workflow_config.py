"""Hot-reloadable workflow configuration from config/workflow.yaml."""

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger("kh.core.workflow_config")

_CONFIG_PATH = Path(os.getenv(
    "KH_WORKFLOW_CONFIG",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "workflow.yaml"),
))

_DEFAULTS = {
    "polling": {
        "interval_seconds": 30,
        "stuck_cooldown_seconds": 30,
    },
    "agent": {
        "max_concurrent_sessions": 5,
        "default_timeout_seconds": 600,
        "max_research_rounds": 10,
        "stall_timeout_seconds": 120,
    },
}


@dataclass
class WorkflowConfig:
    poll_interval: int = 30
    stuck_cooldown: int = 30
    max_concurrent_sessions: int = 5
    default_timeout: int = 600
    max_research_rounds: int = 10
    stall_timeout: int = 120
    escalation_threshold: int = 600

    _last_mtime: float = field(default=0.0, repr=False)
    _last_check: float = field(default=0.0, repr=False)

    def reload_if_changed(self) -> bool:
        """Check file mtime and reload if changed. Returns True if reloaded."""
        now = time.monotonic()
        if now - self._last_check < 1.0:
            return False
        self._last_check = now

        try:
            mtime = _CONFIG_PATH.stat().st_mtime
        except OSError:
            return False

        if mtime == self._last_mtime:
            return False

        return self._load()

    def _load(self) -> bool:
        """Load config from yaml. Returns True on success, keeps old values on failure."""
        try:
            with open(_CONFIG_PATH, "r") as f:
                raw = yaml.safe_load(f) or {}

            polling = raw.get("polling", {})
            agent = raw.get("agent", {})

            self.poll_interval = int(polling.get("interval_seconds", _DEFAULTS["polling"]["interval_seconds"]))
            self.stuck_cooldown = int(polling.get("stuck_cooldown_seconds", _DEFAULTS["polling"]["stuck_cooldown_seconds"]))
            self.max_concurrent_sessions = int(agent.get("max_concurrent_sessions", _DEFAULTS["agent"]["max_concurrent_sessions"]))
            self.default_timeout = int(agent.get("default_timeout_seconds", _DEFAULTS["agent"]["default_timeout_seconds"]))
            self.max_research_rounds = int(agent.get("max_research_rounds", _DEFAULTS["agent"]["max_research_rounds"]))
            self.stall_timeout = int(agent.get("stall_timeout_seconds", _DEFAULTS["agent"]["stall_timeout_seconds"]))
            self.escalation_threshold = int(agent.get("escalation_threshold_seconds", _DEFAULTS["agent"].get("escalation_threshold_seconds", 600)))

            self._last_mtime = _CONFIG_PATH.stat().st_mtime
            logger.info(
                "Workflow config reloaded: poll=%ds, stuck_cooldown=%ds, max_concurrent=%d, timeout=%ds",
                self.poll_interval, self.stuck_cooldown, self.max_concurrent_sessions, self.default_timeout,
            )
            return True

        except Exception as e:
            logger.warning("[FAULT:CONFIG] failed to reload workflow config, keeping previous values: %s", e)
            return False


# Singleton instance
workflow_config = WorkflowConfig()
workflow_config._load()
