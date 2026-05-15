"""
Alert Manager - Sends email and Slack notifications on status changes.
Uses a state machine per target to avoid alert storms.
"""

import logging
import smtplib
import time
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import Enum
from typing import Dict, Optional

import aiohttp

from monitors.health_checker import HealthResult

logger = logging.getLogger(__name__)


class AlertState(Enum):
    OK = "ok"
    ALERTING = "alerting"
    RESOLVED = "resolved"


@dataclass
class TargetAlertState:
    state: AlertState = AlertState.OK
    last_alerted_at: float = 0.0
    consecutive_failures: int = 0
    last_status: str = "healthy"


class AlertManager:
    def __init__(self, settings):
        self.settings = settings
        self._states: Dict[str, TargetAlertState] = {}

        # Alert config
        self.failure_threshold = settings.get("alerts.failure_threshold", 3)
        self.re_alert_interval = settings.get("alerts.re_alert_interval_seconds", 300)

        # Channels
        self.email_cfg = settings.get("alerts.email", {})
        self.slack_cfg = settings.get("alerts.slack", {})

    async def process_result(self, result: HealthResult):
        """Update alert state and fire notifications when thresholds are crossed."""
        name = result.target_name
        state = self._states.setdefault(name, TargetAlertState())

        if result.status == "healthy":
            if state.state == AlertState.ALERTING:
                # Recovery
                state.state = AlertState.RESOLVED
                await self._send_alert(result, kind="resolved")
            state.state = AlertState.OK
            state.consecutive_failures = 0
        else:
            state.consecutive_failures += 1
            now = time.time()
            crossed_threshold = state.consecutive_failures >= self.failure_threshold
            re_alert_due = (now - state.last_alerted_at) >= self.re_alert_interval

            if crossed_threshold and (state.state != AlertState.ALERTING or re_alert_due):
                state.state = AlertState.ALERTING
                state.last_alerted_at = now
                await self._send_alert(result, kind="failure")

        state.last_status = result.status

    async def _send_alert(self, result: HealthResult, kind: str):
        subject, body = self._build_message(result, kind)
        logger.warning(f"[ALERT:{kind.upper()}] {result.target_name} — {body}")

        if self.email_cfg.get("enabled"):
            await self._send_email(subject, body)
        if self.slack_cfg.get("enabled"):
            await self._send_slack(result, kind)

    def _build_message(self, result: HealthResult, kind: str):
        emoji = "🔴" if kind == "failure" else "🟢"
        subject = f"{emoji} [{kind.upper()}] {result.target_name} is {result.status}"
        lines = [
            f"Target  : {result.target_name}",
            f"URL     : {result.url}",
            f"Status  : {result.status.upper()}",
            f"HTTP    : {result.status_code or 'N/A'}",
            f"Resp ms : {result.response_time_ms:.0f}",
            f"Time    : {result.checked_at.isoformat()}",
        ]
        if result.error:
            lines.append(f"Error   : {result.error}")
        return subject, "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Email                                                                #
    # ------------------------------------------------------------------ #

    async def _send_email(self, subject: str, body: str):
        cfg = self.email_cfg
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = cfg["from"]
            msg["To"] = ", ".join(cfg["to"])
            msg.attach(MIMEText(body, "plain"))
            msg.attach(MIMEText(self._html_body(subject, body), "html"))

            with smtplib.SMTP(cfg["smtp_host"], cfg.get("smtp_port", 587)) as server:
                server.starttls()
                server.login(cfg["username"], cfg["password"])
                server.sendmail(cfg["from"], cfg["to"], msg.as_string())
            logger.info(f"Email alert sent to {cfg['to']}")
        except Exception as e:
            logger.error(f"Failed to send email alert: {e}")

    def _html_body(self, subject: str, body: str) -> str:
        rows = ""
        for line in body.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                rows += f"<tr><td style='padding:6px 12px;font-weight:bold;color:#555'>{k.strip()}</td><td style='padding:6px 12px'>{v.strip()}</td></tr>"
        color = "#e53e3e" if "FAILURE" in subject else "#38a169"
        return f"""
        <html><body style='font-family:sans-serif;background:#f7f7f7;padding:24px'>
          <div style='max-width:520px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.1)'>
            <div style='background:{color};padding:16px 24px;color:#fff;font-size:18px;font-weight:bold'>{subject}</div>
            <table style='width:100%;border-collapse:collapse'>{rows}</table>
          </div>
        </body></html>"""

    # ------------------------------------------------------------------ #
    # Slack                                                                #
    # ------------------------------------------------------------------ #

    async def _send_slack(self, result: HealthResult, kind: str):
        cfg = self.slack_cfg
        color = "#e53e3e" if kind == "failure" else "#38a169"
        emoji = "🔴" if kind == "failure" else "🟢"
        payload = {
            "attachments": [{
                "color": color,
                "blocks": [
                    {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} {result.target_name} — {kind.upper()}"}},
                    {"type": "section", "fields": [
                        {"type": "mrkdwn", "text": f"*URL*\n{result.url}"},
                        {"type": "mrkdwn", "text": f"*Status*\n{result.status.upper()}"},
                        {"type": "mrkdwn", "text": f"*HTTP Code*\n{result.status_code or 'N/A'}"},
                        {"type": "mrkdwn", "text": f"*Response Time*\n{result.response_time_ms:.0f}ms"},
                    ]},
                ],
            }]
        }
        if result.error:
            payload["attachments"][0]["blocks"].append(
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*Error*: `{result.error}`"}}
            )
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(cfg["webhook_url"], json=payload) as resp:
                    if resp.status != 200:
                        logger.error(f"Slack returned {resp.status}: {await resp.text()}")
                    else:
                        logger.info("Slack alert sent")
        except Exception as e:
            logger.error(f"Failed to send Slack alert: {e}")
