"""Data models for NVR Viewer."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class DetectionEvent:
    """A single detection event."""
    detection_type: str  # 'motion', 'face', 'person', 'animal', 'vehicle', 'object'
    confidence: float = 0.0
    label: str = ""
    bbox: tuple = (0, 0, 0, 0)  # x, y, w, h
    timestamp: datetime = field(default_factory=datetime.now)
    camera_name: str = ""
    snapshot_path: str = ""


@dataclass 
class RecordingInfo:
    """Recording metadata."""
    camera_name: str
    file_path: str
    start_time: datetime = field(default_factory=datetime.now)
    end_time: Optional[datetime] = None
    trigger: str = "manual"
    file_size: int = 0
