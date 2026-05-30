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
    
    def __init__(self, min_area: int = 2000, threshold: float = 40.0, history: int = 500):
        self.min_area = min_area
        self.threshold = threshold
        self._bg_sub = cv2.createBackgroundSubtractorMOG2(
            history=history, varThreshold=threshold, detectShadows=True)
        self._bg_sub.setNMixtures(5)  # more Gaussian mixtures for outdoor scenes
        self._frame_count = 0
        self._warmup = 60  # longer warmup for stable background model
    
    def detect(self, frame: np.ndarray) -> list[dict]:
        """Detect motion regions in frame.
        
        Returns list of dicts: [{type: 'motion', bbox: (x,y,w,h), area: int, confidence: float}]
        """
        self._frame_count += 1

        # Downscale for faster processing
        h_orig, w_orig = frame.shape[:2]
        scale = 1.0
        if w_orig > 640:
            scale = 640 / w_orig
            small = cv2.resize(frame, (640, int(h_orig * scale)), interpolation=cv2.INTER_NEAREST)
        else:
            small = frame

        # Apply background subtraction
        blurred = cv2.GaussianBlur(small, (11, 11), 0)  # smooth out leaf/noise jitter
        mask = self._bg_sub.apply(blurred, learningRate=0.002)  # slower learning = more stable bg
        
        # Remove shadows (value 127 in MOG2)
        _, mask = cv2.threshold(mask, 200, 255, cv2.THRESH_BINARY)
        
        # Aggressive morphological ops to eliminate scattered small motion (leaves, rain)
        kernel_small = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        kernel_large = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel_small, iterations=2)  # remove noise
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel_large)  # merge nearby blobs
        mask = cv2.dilate(mask, kernel_small, iterations=1)
        
        # Skip warmup period
        if self._frame_count < self._warmup:
            return []
        
        # Find contours
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        detections = []
        small_area = small.shape[0] * small.shape[1]
        
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < self.min_area * (scale ** 2):
                continue
            
            x, y, w, h = cv2.boundingRect(contour)

            # Reject very wide/flat shapes (leaves, branches swaying)
            aspect = w / max(h, 1)
            if aspect > 5.0 or aspect < 0.15:
                continue

            # Solidity filter: real objects fill their bounding box more than scattered leaves
            hull = cv2.convexHull(contour)
            hull_area = cv2.contourArea(hull)
            solidity = area / max(hull_area, 1)
            if solidity < 0.25:
                continue
            # Scale bbox back to original resolution
            if scale != 1.0:
                x, y, w, h = int(x / scale), int(y / scale), int(w / scale), int(h / scale)
                area = area / (scale ** 2)
            
            confidence = min(area / (h_orig * w_orig) * 10, 1.0)
            
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
        self._bg_sub.setNMixtures(5)
        self._frame_count = 0
