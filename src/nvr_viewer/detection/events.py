"""Detection event processing and notification."""
import cv2
import numpy as np
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SNAPSHOT_DIR = Path.home() / ".nvr-viewer" / "snapshots"


class EventProcessor:
    """Processes detection events — saves snapshots, logs to DB, deduplicates."""
    
    def __init__(self, db=None, snapshot_dir: Path = SNAPSHOT_DIR, cooldown_seconds: float = 5.0):
        self.db = db
        self.snapshot_dir = snapshot_dir
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.cooldown = cooldown_seconds
        self._last_events: dict[str, datetime] = {}  # key -> last trigger time
    
    def process(self, camera_id: int, camera_name: str, frame: np.ndarray,
                detections: list[dict]) -> list[dict]:
        """Process detections: deduplicate, save snapshots, log to DB.
        
        Returns list of new (non-duplicate) events that were logged.
        """
        new_events = []
        
        for det in detections:
            key = f"{camera_name}:{det['type']}:{det.get('label', '')}"
            now = datetime.now()
            
            # Cooldown deduplication
            if key in self._last_events:
                elapsed = (now - self._last_events[key]).total_seconds()
                if elapsed < self.cooldown:
                    continue
            
            self._last_events[key] = now
            
            # Save snapshot
            snapshot_path = ""
            try:
                ts = now.strftime("%Y%m%d_%H%M%S")
                fname = f"{camera_name}_{det['type']}_{ts}.jpg"
                snapshot_path = str(self.snapshot_dir / fname)
                
                # Draw bbox on snapshot
                snap = frame.copy()
                x, y, w, h = det["bbox"]
                cv2.rectangle(snap, (x, y), (x + w, y + h), (0, 255, 0), 2)
                label_text = f"{det.get('label', det['type'])} {det.get('confidence', 0):.0%}"
                cv2.putText(snap, label_text, (x, y - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                cv2.imwrite(snapshot_path, snap)
            except Exception as e:
                logger.warning(f"Failed to save snapshot: {e}")
                snapshot_path = ""
            
            # Log to database
            if self.db:
                try:
                    self.db.log_detection(
                        camera_id=camera_id,
                        detection_type=det["type"],
                        confidence=det.get("confidence", 0),
                        label=det.get("label", ""),
                        bbox=det["bbox"],
                        snapshot_path=snapshot_path,
                    )
                except Exception as e:
                    logger.warning(f"Failed to log detection: {e}")
            
            det["snapshot_path"] = snapshot_path
            new_events.append(det)
            logger.info(f"[{camera_name}] {det['type']}: {det.get('label', '')} "
                        f"({det.get('confidence', 0):.0%})")
        
        return new_events
