"""Object, face, and animal detection using YOLOv8."""
import cv2
import numpy as np
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# COCO classes grouped by category
PERSON_CLASSES = {"person"}
ANIMAL_CLASSES = {"bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"}
VEHICLE_CLASSES = {"bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat"}

MODELS_DIR = Path.home() / ".nvr-viewer" / "models"


class ObjectDetector:
    """YOLO-based object detection with category filtering."""
    
    def __init__(self, model_name: str = "yolov8n.pt", confidence: float = 0.4, device: str = "cpu"):
        self.confidence = confidence
        self.device = device
        self._model = None
        self._model_name = model_name
        self._model_path = MODELS_DIR / model_name
    
    def _ensure_model(self):
        """Lazy-load YOLO model (downloads on first use)."""
        if self._model is not None:
            return
        
        try:
            from ultralytics import YOLO
            MODELS_DIR.mkdir(parents=True, exist_ok=True)
            
            if self._model_path.exists():
                self._model = YOLO(str(self._model_path))
            else:
                logger.info(f"Downloading {self._model_name}...")
                self._model = YOLO(self._model_name)
                # Save to our models dir for future use
                import shutil
                default_path = Path(self._model_name)
                if default_path.exists():
                    shutil.move(str(default_path), str(self._model_path))
            
            self._model.to(self.device)
            logger.info(f"Loaded YOLO model: {self._model_name} on {self.device}")
        except ImportError:
            logger.error("ultralytics not installed. Run: pip install ultralytics")
            raise
    
    def detect(self, frame: np.ndarray, categories: set = None) -> list[dict]:
        """Run detection on a frame.
        
        Args:
            frame: BGR image (numpy array)
            categories: Set of categories to detect. None = all.
                        Options: 'person', 'animal', 'vehicle', 'object'
        
        Returns:
            List of detection dicts with: type, label, confidence, bbox
        """
        self._ensure_model()
        
        results = self._model(frame, conf=self.confidence, verbose=False)
        
        detections = []
        for result in results:
            for box in result.boxes:
                label = result.names[int(box.cls)]
                conf = float(box.conf)
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                
                # Categorize
                det_type = self._categorize(label)
                
                # Filter by category if specified
                if categories and det_type not in categories:
                    continue
                
                detections.append({
                    "type": det_type,
                    "label": label,
                    "confidence": round(conf, 3),
                    "bbox": (x1, y1, x2 - x1, y2 - y1),
                })
        
        return detections
    
    @staticmethod
    def _categorize(label: str) -> str:
        """Map COCO class name to detection category."""
        if label in PERSON_CLASSES:
            return "person"
        elif label in ANIMAL_CLASSES:
            return "animal"
        elif label in VEHICLE_CLASSES:
            return "vehicle"
        return "object"


class FaceDetector:
    """Face detection using OpenCV's DNN face detector (built-in, no extra models)."""
    
    def __init__(self, confidence: float = 0.5):
        self.confidence = confidence
        self._detector = None
    
    def _ensure_detector(self):
        if self._detector is not None:
            return
        # Use OpenCV's built-in Yunet face detector
        try:
            self._detector = cv2.FaceDetectorYN.create(
                "",  # Will use built-in model
                "",
                (320, 320),
                self.confidence
            )
        except Exception:
            # Fallback to Haar cascade
            cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            self._detector = cv2.CascadeClassifier(cascade_path)
            self._use_cascade = True
            logger.info("Using Haar cascade face detector (fallback)")
            return
        self._use_cascade = False
    
    def detect(self, frame: np.ndarray) -> list[dict]:
        """Detect faces in frame.
        
        Returns list of dicts: [{type: 'face', bbox: (x,y,w,h), confidence: float}]
        """
        self._ensure_detector()
        
        if hasattr(self, '_use_cascade') and self._use_cascade:
            return self._detect_cascade(frame)
        
        return self._detect_dnn(frame)
    
    def _detect_cascade(self, frame: np.ndarray) -> list[dict]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = self._detector.detectMultiScale(gray, 1.1, 5, minSize=(30, 30))
        
        return [{
            "type": "face",
            "label": "face",
            "confidence": 0.8,  # Cascades don't provide confidence
            "bbox": (int(x), int(y), int(w), int(h)),
        } for (x, y, w, h) in faces]
    
    def _detect_dnn(self, frame: np.ndarray) -> list[dict]:
        h, w = frame.shape[:2]
        self._detector.setInputSize((w, h))
        
        _, faces = self._detector.detect(frame)
        if faces is None:
            return []
        
        return [{
            "type": "face",
            "label": "face",
            "confidence": round(float(face[14]), 3) if len(face) > 14 else 0.8,
            "bbox": (int(face[0]), int(face[1]), int(face[2]), int(face[3])),
        } for face in faces]
