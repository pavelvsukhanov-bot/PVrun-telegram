import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "garmin.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                activity_id          TEXT PRIMARY KEY,
                name                 TEXT,
                start_time           TEXT,
                distance_m           REAL,
                duration_s           REAL,
                moving_time_s        REAL,
                avg_hr               INTEGER,
                max_hr               INTEGER,
                avg_pace_s_km        REAL,
                calories             INTEGER,
                elevation_gain       REAL,
                avg_cadence          REAL,
                avg_gct_ms           REAL,
                avg_stride_length_cm REAL,
                avg_vert_osc_cm      REAL,
                avg_vert_ratio_pct   REAL,
                training_effect      REAL,
                tg_sent              INTEGER DEFAULT 0,
                raw_json             TEXT
            )
        """)
        # migrate existing DB: add new columns if absent
        existing = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        new_cols = {
            "avg_gct_ms":           "REAL",
            "avg_stride_length_cm": "REAL",
            "avg_vert_osc_cm":      "REAL",
            "avg_vert_ratio_pct":   "REAL",
            "tg_sent":              "INTEGER DEFAULT 0",
        }
        for col, col_type in new_cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {col_type}")
