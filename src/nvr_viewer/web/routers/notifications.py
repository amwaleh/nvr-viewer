"""Notification settings router."""
import logging
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

from ..state import notifier

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["notifications"])


class NotificationSettings(BaseModel):
    enabled: Optional[bool] = None
    email_enabled: Optional[bool] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_use_tls: Optional[bool] = None
    email_to: Optional[list[str]] = None
    webhook_enabled: Optional[bool] = None
    webhook_url: Optional[str] = None
    notify_on_detection: Optional[bool] = None
    notify_detection_types: Optional[list[str]] = None
    notify_on_camera_disconnect: Optional[bool] = None
    notify_on_disk_warning: Optional[bool] = None
    notify_on_recording_paused: Optional[bool] = None
    cooldown_seconds: Optional[int] = None


class TestNotification(BaseModel):
    channel: str = "all"  # "email", "webhook", or "all"


@router.get("/notifications")
async def get_notification_settings():
    """Get current notification settings (password hidden)."""
    config = notifier.config.to_dict()
    if config.get("smtp_password"):
        config["smtp_password"] = "********"
    return config


@router.post("/notifications")
async def update_notification_settings(settings: NotificationSettings):
    """Update notification settings."""
    updates = {k: v for k, v in settings.model_dump().items() if v is not None}
    notifier.update_config(updates)
    return {"message": "Notification settings updated", "config": notifier.config.to_dict()}


@router.post("/notifications/test")
async def test_notification(req: TestNotification):
    """Send a test notification to verify configuration."""
    subject = "NVR Viewer Test Notification"
    body = "This is a test notification from NVR Viewer. If you received this, notifications are working correctly."

    if req.channel in ("email", "all") and notifier.config.email_enabled:
        notifier._send_email(subject, body)
    if req.channel in ("webhook", "all") and notifier.config.webhook_enabled:
        notifier._send_webhook(subject, body)

    return {"message": f"Test notification sent via {req.channel}"}
