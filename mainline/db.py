"""SQLite 数据层：建表、按日写入与读取。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    date TEXT PRIMARY KEY,
    generated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS themes_daily (
    date TEXT NOT NULL,
    theme_id TEXT NOT NULL,
    name TEXT NOT NULL,
    tier TEXT NOT NULL,
    carrier TEXT NOT NULL,
    carrier_type TEXT NOT NULL,
    rs REAL,
    rs_prev REAL,
    rs_rising INTEGER,
    trend_above_ma20 INTEGER,
    trend_ma5_over_ma10 INTEGER,
    trend_golden_cross INTEGER,
    volume_ratio REAL,
    breadth REAL,
    breadth_covered INTEGER,
    catalyst_count INTEGER,
    limit_up_count INTEGER,
    limit_up_trend TEXT,
    ret_5d REAL,
    status TEXT NOT NULL,
    note TEXT,
    extras_json TEXT,
    PRIMARY KEY (date, theme_id)
);

CREATE TABLE IF NOT EXISTS news_items (
    date TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    published_at TEXT,
    summary TEXT,
    matched_themes TEXT,
    sentiment TEXT,
    url TEXT,
    event_key TEXT,
    PRIMARY KEY (date, category, url)
);

CREATE TABLE IF NOT EXISTS market_quotes (
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    latest REAL,
    change_pct REAL,
    note TEXT,
    quote_time TEXT,
    PRIMARY KEY (date, symbol)
);

CREATE TABLE IF NOT EXISTS limit_up_clusters (
    date TEXT NOT NULL,
    sector TEXT NOT NULL,
    count INTEGER,
    lianban INTEGER,
    first_board INTEGER,
    series_json TEXT,
    trend TEXT,
    in_theme_pool INTEGER,
    theme_ids TEXT,
    note TEXT,
    PRIMARY KEY (date, sector)
);

CREATE TABLE IF NOT EXISTS holdings_note (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    theme_id TEXT,
    theme_name TEXT,
    theme_status TEXT,
    risk_note TEXT,
    mapping_status TEXT,
    PRIMARY KEY (date, code)
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def save_day(
    conn: sqlite3.Connection,
    date_str: str,
    generated_at: str,
    payload: dict,
    themes_rows: list[dict],
    news_rows: list[dict],
    quote_rows: list[dict],
    limit_rows: list[dict],
    holding_rows: list[dict],
) -> None:
    """同一日期重复运行视为覆盖，保证幂等。"""
    with conn:
        for table in ("snapshots", "themes_daily", "news_items", "market_quotes", "limit_up_clusters", "holdings_note"):
            conn.execute(f"DELETE FROM {table} WHERE date = ?", (date_str,))

        conn.execute(
            "INSERT INTO snapshots (date, generated_at, payload_json) VALUES (?, ?, ?)",
            (date_str, generated_at, _json(payload)),
        )

        conn.executemany(
            """
            INSERT INTO themes_daily (
                date, theme_id, name, tier, carrier, carrier_type, rs, rs_prev, rs_rising,
                trend_above_ma20, trend_ma5_over_ma10, trend_golden_cross, volume_ratio,
                breadth, breadth_covered, catalyst_count, limit_up_count, limit_up_trend,
                ret_5d, status, note, extras_json
            ) VALUES (
                :date, :theme_id, :name, :tier, :carrier, :carrier_type, :rs, :rs_prev, :rs_rising,
                :trend_above_ma20, :trend_ma5_over_ma10, :trend_golden_cross, :volume_ratio,
                :breadth, :breadth_covered, :catalyst_count, :limit_up_count, :limit_up_trend,
                :ret_5d, :status, :note, :extras_json
            )
            """,
            themes_rows,
        )

        conn.executemany(
            """
            INSERT INTO news_items (
                date, category, title, source, published_at, summary,
                matched_themes, sentiment, url, event_key
            ) VALUES (
                :date, :category, :title, :source, :published_at, :summary,
                :matched_themes, :sentiment, :url, :event_key
            )
            """,
            news_rows,
        )

        conn.executemany(
            """
            INSERT INTO market_quotes (
                date, symbol, name, category, latest, change_pct, note, quote_time
            ) VALUES (
                :date, :symbol, :name, :category, :latest, :change_pct, :note, :quote_time
            )
            """,
            quote_rows,
        )

        conn.executemany(
            """
            INSERT INTO limit_up_clusters (
                date, sector, count, lianban, first_board, series_json,
                trend, in_theme_pool, theme_ids, note
            ) VALUES (
                :date, :sector, :count, :lianban, :first_board, :series_json,
                :trend, :in_theme_pool, :theme_ids, :note
            )
            """,
            limit_rows,
        )

        conn.executemany(
            """
            INSERT INTO holdings_note (
                date, code, name, theme_id, theme_name, theme_status, risk_note, mapping_status
            ) VALUES (
                :date, :code, :name, :theme_id, :theme_name, :theme_status, :risk_note, :mapping_status
            )
            """,
            holding_rows,
        )


def list_dates(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT date FROM snapshots ORDER BY date DESC").fetchall()
    return [row["date"] for row in rows]


def load_day(conn: sqlite3.Connection, date_str: str) -> dict[str, Any]:
    snapshot = conn.execute("SELECT * FROM snapshots WHERE date = ?", (date_str,)).fetchone()
    if snapshot is None:
        return {}
    return {
        "date": snapshot["date"],
        "generated_at": snapshot["generated_at"],
        "payload": json.loads(snapshot["payload_json"]),
        "themes": [dict(r) for r in conn.execute(
            "SELECT * FROM themes_daily WHERE date = ? ORDER BY tier, theme_id", (date_str,)
        )],
        "news": [dict(r) for r in conn.execute(
            "SELECT * FROM news_items WHERE date = ? ORDER BY published_at DESC", (date_str,)
        )],
        "quotes": [dict(r) for r in conn.execute(
            "SELECT * FROM market_quotes WHERE date = ?", (date_str,)
        )],
        "limit_up": [dict(r) for r in conn.execute(
            "SELECT * FROM limit_up_clusters WHERE date = ? ORDER BY count DESC, sector", (date_str,)
        )],
        "holdings": [dict(r) for r in conn.execute(
            "SELECT * FROM holdings_note WHERE date = ?", (date_str,)
        )],
    }


def recent_themes(
    conn: sqlite3.Connection, date_str: str, limit: int = 1
) -> list[tuple[str, dict[str, dict]]]:
    """返回此前 limit 个快照的主线状态，按日期升序，最新一天在末尾。"""
    if limit <= 0:
        return []
    rows = conn.execute(
        "SELECT date FROM snapshots WHERE date < ? ORDER BY date DESC LIMIT ?",
        (date_str, int(limit)),
    ).fetchall()
    history: list[tuple[str, dict[str, dict]]] = []
    for day in sorted(row["date"] for row in rows):
        themes = conn.execute("SELECT * FROM themes_daily WHERE date = ?", (day,)).fetchall()
        history.append((day, {theme["theme_id"]: dict(theme) for theme in themes}))
    return history


def previous_date(conn: sqlite3.Connection, date_str: str) -> str | None:
    row = conn.execute(
        "SELECT date FROM snapshots WHERE date < ? ORDER BY date DESC LIMIT 1", (date_str,)
    ).fetchone()
    return row["date"] if row else None
