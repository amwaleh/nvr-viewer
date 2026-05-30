"""Notification system for NVR Viewer.

Sends alerts via email (SMTP), webhook (generic HTTP POST), and/or
desktop notification when important events occur:
  - Detection events (person, face, vehicle)
  - Camera disconnected / reconnected
  - Disk space warnings
  - Recording paused / resumed
"""
import json
import logging
import smtplib
import threading
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".nvr-viewer"
NOTIFY_CONFIG_FILE = CONFIG_DIR / "notifications.json"


@dataclass
class NotifyConfig:
    """Notification configuration."""
    enabled: bool = False

    # Email (SMTP)
    email_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    email_to: list[str] = field(default_factory=list)

    # Webhook (generic HTTP POST — works with Slack, Discord, Teams, IFTTT, etc.)
    webhook_enabled: bool = False
    webhook_url: str = ""

    # What triggers notifications
    notify_on_detection: bool = True
    notify_detection_types: list[str] = field(
        default_factory=lambda: ["person", "face", "vehicle"]
    )
    notify_on_camera_disconnect: bool = True
    notify_on_disk_warning: bool = True
    notify_on_recording_paused: bool = True

    # Throttle: minimum seconds between notifications of same type
    cooldown_seconds: int = 60

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "email_enabled": self.email_enabled,
            "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port,
            "smtp_user": self.smtp_user,
            "smtp_password": self.smtp_password,
            "smtp_use_tls": self.smtp_use_tls,
            "email_to": self.email_to,
            "webhook_enabled": self.webhook_enabled,
            "webhook_url": self.webhook_url,
            "notify_on_detection": self.notify_on_detection,
            "notify_detection_types": self.notify_detection_types,
            "notify_on_camera_disconnect": self.notify_on_camera_disconnect,
            "notify_on_disk_warning": self.notify_on_disk_warning,
            "notify_on_recording_paused": self.notify_on_recording_paused,
            "cooldown_seconds": self.cooldown_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "NotifyConfig":
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)


class NotificationManager:
    """Manages sending notifications with throttling."""

    def __init__(self, config: Optional[NotifyConfig] = None):
        self.config = config or self._load_config()
        self._cooldowns: dict[str, float] = {}  # event_key -> last_sent_timestamp
        self._lock = threading.Lock()

    # --- Config persistence ---

    @staticmethod
    def _load_config() -> NotifyConfig:
        if NOTIFY_CONFIG_FILE.exists():
            try:
                with open(NOTIFY_CONFIG_FILE, "r") as f:
                    return NotifyConfig.from_dict(json.load(f))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load notification config: %s", e)
        return NotifyConfig()

    def save_config(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(NOTIFY_CONFIG_FILE, "w") as f:
                json.dump(self.config.to_dict(), f, indent=2)
        except OSError as e:
            logger.warning("Failed to save notification config: %s", e)

    def update_config(self, data: dict):
        """Update config from a partial dict and save."""
        current = self.config.to_dict()
        current.update(data)
        self.config = NotifyConfig.from_dict(current)
        self.save_config()

    # --- Notification triggers ---

    def notify_detection(self, detection_type: str, camera_name: str,
                         confidence: float = 0.0, label: str = ""):
        """Send notification for a detection event."""
        if not self.config.enabled or not self.config.notify_on_detection:
            return
        if detection_type not in self.config.notify_detection_types:
            return

        key = f"detection:{camera_name}:{detection_type}"
        if self._is_throttled(key):
            return

        subject = f"NVR Alert: {detection_type} detected on {camera_name}"
        body = (
            f"Detection: {detection_type}\n"
            f"Camera: {camera_name}\n"
            f"Label: {label or detection_type}\n"
            f"Confidence: {confidence:.0%}\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self._send(subject, body, key)

    def notify_camera_disconnect(self, camera_name: str, host: str):
        """Send notification when a camera disconnects."""
        if not self.config.enabled or not self.config.notify_on_camera_disconnect:
            return

        key = f"disconnect:{host}"
        if self._is_throttled(key):
            return

        subject = f"NVR Alert: Camera '{camera_name}' disconnected"
        body = (
            f"Camera '{camera_name}' ({host}) has disconnected.\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"The system will attempt to reconnect automatically."
        )
        self._send(subject, body, key)

    def notify_camera_reconnect(self, camera_name: str, host: str):
        """Send notification when a camera reconnects."""
        if not self.config.enabled or not self.config.notify_on_camera_disconnect:
            return

        key = f"reconnect:{host}"
        if self._is_throttled(key):
            return

        subject = f"NVR Info: Camera '{camera_name}' reconnected"
        body = (
            f"Camera '{camera_name}' ({host}) is back online.\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self._send(subject, body, key)

    def notify_disk_warning(self, free_percent: float, free_gb: float):
        """Send notification for low disk space."""
        if not self.config.enabled or not self.config.notify_on_disk_warning:
            return

        key = "disk:warning"
        if self._is_throttled(key):
            return

        subject = f"NVR Warning: Disk space low ({free_percent:.0f}% free)"
        body = (
            f"Disk space is running low.\n"
            f"Free: {free_gb:.1f} GB ({free_percent:.1f}%)\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Old recordings will be cleaned up automatically."
        )
        self._send(subject, body, key)

    def notify_recording_paused(self, reason: str = "disk space critically low"):
        """Send notification when recording is paused."""
        if not self.config.enabled or not self.config.notify_on_recording_paused:
            return

        key = "recording:paused"
        if self._is_throttled(key):
            return

        subject = "NVR CRITICAL: Recording paused"
        body = (
            f"Recording has been paused: {reason}\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Recording will resume automatically once space is freed."
        )
        self._send(subject, body, key)

    def notify_recording_resumed(self):
        """Send notification when recording resumes."""
        if not self.config.enabled or not self.config.notify_on_recording_paused:
            return

        key = "recording:resumed"
        if self._is_throttled(key):
            return

        subject = "NVR Info: Recording resumed"
        body = (
            f"Recording has resumed.\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self._send(subject, body, key)

    # --- Internal ---

    def _is_throttled(self, key: str) -> bool:
        """Check if this event type is within cooldown period."""
        with self._lock:
            now = datetime.now().timestamp()
            last = self._cooldowns.get(key, 0)
            if now - last < self.config.cooldown_seconds:
                return True
            self._cooldowns[key] = now
            return False

    def _send(self, subject: str, body: str, key: str):
        """Send notification via all enabled channels (non-blocking)."""
        threading.Thread(
            target=self._send_all, args=(subject, body, key),
            daemon=True, name=f"notify-{key}",
        ).start()

    def _send_all(self, subject: str, body: str, key: str):
        if self.config.email_enabled:
            self._send_email(subject, body)
        if self.config.webhook_enabled:
            self._send_webhook(subject, body)

    def _send_email(self, subject: str, body: str):
        """Send email via SMTP."""
        if not self.config.smtp_host or not self.config.email_to:
            return
        try:
            msg = MIMEText(body)
            msg["Subject"] = subject
            msg["From"] = self.config.smtp_user or "nvr-viewer@localhost"
            msg["To"] = ", ".join(self.config.email_to)

            if self.config.smtp_use_tls:
                server = smtplib.SMTP(self.config.smtp_host, self.config.smtp_port)
                server.starttls()
            else:
                server = smtplib.SMTP(self.config.smtp_host, self.config.smtp_port)

            if self.config.smtp_user and self.config.smtp_password:
                server.login(self.config.smtp_user, self.config.smtp_password)

            server.sendmail(msg["From"], self.config.email_to, msg.as_string())
            server.quit()
            logger.info("Email notification sent: %s", subject)
        except Exception:
            logger.exception("Failed to send email notification")

    def _send_webhook(self, subject: str, body: str):
        """Send webhook POST (JSON payload compatible with Slack/Discord/Teams)."""
        if not self.config.webhook_url:
            return
        try:
            payload = json.dumps({
                "text": f"**{subject}**\n\n{body}",
                # Slack format
                "blocks": [
                    {"type": "header", "text": {"type": "plain_text", "text": subject}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": body}},
                ],
            }).encode()

            req = urllib.request.Request(
                self.config.webhook_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                logger.info("Webhook notification sent (%d): %s", resp.status, subject)
        except Exception:
            logger.exception("Failed to send webhook notification")
