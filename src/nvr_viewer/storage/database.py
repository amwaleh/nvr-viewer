"""SQLite database for detection events and analysis."""
import sqlite3
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)

DB_PATH = Path.home() / ".nvr-viewer" / "nvr_viewer.db"


class Database:
    """SQLite database manager."""
    
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()
    
    def _init_db(self):
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()
    
    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS cameras (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                host TEXT NOT NULL,
                port INTEGER DEFAULT 554,
                path TEXT DEFAULT '/onvif1',
                added_at TEXT DEFAULT (datetime('now')),
                last_seen TEXT,
                UNIQUE(host, port)
            );
            
            CREATE TABLE IF NOT EXISTS detection_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_id INTEGER,
                timestamp TEXT NOT NULL,
                detection_type TEXT NOT NULL,  -- 'motion', 'face', 'person', 'animal', 'vehicle', 'object'
                confidence REAL,
                label TEXT,  -- e.g., 'cat', 'dog', 'John Doe'
                bbox_x INTEGER,
                bbox_y INTEGER,
                bbox_w INTEGER,
                bbox_h INTEGER,
                snapshot_path TEXT,
                metadata TEXT,  -- JSON blob for extra data
                FOREIGN KEY (camera_id) REFERENCES cameras(id)
            );
            
            CREATE TABLE IF NOT EXISTS recordings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_id INTEGER,
                start_time TEXT NOT NULL,
                end_time TEXT,
                file_path TEXT NOT NULL,
                file_size INTEGER,
                trigger TEXT DEFAULT 'manual',  -- 'manual', 'motion', 'schedule'
                FOREIGN KEY (camera_id) REFERENCES cameras(id)
            );
            
            CREATE INDEX IF NOT EXISTS idx_events_camera ON detection_events(camera_id);
            CREATE INDEX IF NOT EXISTS idx_events_type ON detection_events(detection_type);
            CREATE INDEX IF NOT EXISTS idx_events_time ON detection_events(timestamp);
        """)
        self._conn.commit()
    
    # Camera CRUD
    def add_camera(self, name: str, host: str, port: int = 554, path: str = "/onvif1") -> int:
        cur = self._conn.execute(
            "INSERT OR REPLACE INTO cameras (name, host, port, path, last_seen) VALUES (?, ?, ?, ?, datetime('now'))",
            (name, host, port, path))
        self._conn.commit()
        return cur.lastrowid
    
    def get_cameras(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM cameras ORDER BY name").fetchall()
        return [dict(r) for r in rows]
    
    def get_camera_by_host(self, host: str) -> Optional[dict]:
        row = self._conn.execute("SELECT * FROM cameras WHERE host = ?", (host,)).fetchone()
        return dict(row) if row else None
    
    # Detection events
    def log_detection(self, camera_id: int, detection_type: str, confidence: float = 0.0,
                      label: str = "", bbox: tuple = None, snapshot_path: str = "",
                      metadata: dict = None) -> int:
        bbox_x, bbox_y, bbox_w, bbox_h = bbox if bbox else (0, 0, 0, 0)
        cur = self._conn.execute(
            """INSERT INTO detection_events 
               (camera_id, timestamp, detection_type, confidence, label,
                bbox_x, bbox_y, bbox_w, bbox_h, snapshot_path, metadata)
               VALUES (?, datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (camera_id, detection_type, confidence, label,
             bbox_x, bbox_y, bbox_w, bbox_h, snapshot_path,
             json.dumps(metadata) if metadata else None))
        self._conn.commit()
        return cur.lastrowid
    
    def get_events(self, camera_id: int = None, detection_type: str = None,
                   since: str = None, limit: int = 100) -> list[dict]:
        query = "SELECT * FROM detection_events WHERE 1=1"
        params = []
        if camera_id:
            query += " AND camera_id = ?"
            params.append(camera_id)
        if detection_type:
            query += " AND detection_type = ?"
            params.append(detection_type)
        if since:
            query += " AND timestamp >= ?"
            params.append(since)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    
    # Recordings
    def log_recording(self, camera_id: int, file_path: str, trigger: str = "manual") -> int:
        cur = self._conn.execute(
            "INSERT INTO recordings (camera_id, start_time, file_path, trigger) VALUES (?, datetime('now'), ?, ?)",
            (camera_id, file_path, trigger))
        self._conn.commit()
        return cur.lastrowid
    
    def end_recording(self, recording_id: int, file_size: int = 0):
        self._conn.execute(
            "UPDATE recordings SET end_time = datetime('now'), file_size = ? WHERE id = ?",
            (file_size, recording_id))
        self._conn.commit()
    
    def close(self):
        if self._conn:
            self._conn.close()
