"""Motion detection using background subtraction and frame differencing."""
import cv2
import numpy as np
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class MotionDetector:
    """Detects motion by comparing consecutive frames.
    
    Uses background subtraction (MOG2) for robust motion detection.
    """
    
    def __init__(self, min_area: int = 500, threshold: float = 25.0, history: int = 500):
        self.min_area = min_area
        self.threshold = threshold
        self._bg_sub = cv2.createBackgroundSubtractorMOG2(
            history=history, varThreshold=threshold, detectShadows=True)
        self._frame_count = 0
        self._warmup = 30  # frames before detection starts
    
    def detect(self, frame: np.ndarray) -> list[dict]:
        """Detect motion regions in frame.
        
        Returns list of dicts: [{type: 'motion', bbox: (x,y,w,h), area: int, confidence: float}]
        """
        self._frame_count += 1
        
        # Apply background subtraction
        mask = self._bg_sub.apply(frame)
        
        # Remove shadows (value 127 in MOG2)
        _, mask = cv2.threshold(mask, 200, 255, cv2.THRESH_BINARY)
        
        # Morphological operations to clean noise
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        
        # Skip warmup period
        if self._frame_count < self._warmup:
            return []
        
        # Find contours
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        detections = []
        frame_area = frame.shape[0] * frame.shape[1]
        
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < self.min_area:
                continue
            
            x, y, w, h = cv2.boundingRect(contour)
            confidence = min(area / frame_area * 10, 1.0)  # Normalize
            
            detections.append({
                "type": "motion",
                "bbox": (x, y, w, h),
                "area": int(area),
                "confidence": round(confidence, 3),
                "label": "motion"
            })
        
        return detections
    
    def reset(self):
        """Reset the background model."""
        self._bg_sub = cv2.createBackgroundSubtractorMOG2(
            history=500, varThreshold=self.threshold, detectShadows=True)
        self._frame_count = 0
