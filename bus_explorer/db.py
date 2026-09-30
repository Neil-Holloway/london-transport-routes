"""SQLite persistence for Bus Explorer.

Design note (spec section 6, "crucial rule"): visited status, notes and
favourites are stored keyed by attraction_id alone, in a table separate from
route_attractions. The same attraction discovered from two different bus
routes shares one row here, so marking it visited on one route makes it show
as visited on the other automatically.
"""

import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "bus_explorer.db")


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS attractions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                why TEXT,
                history TEXT,
                source_url TEXT,
                osm_type TEXT,
                osm_id INTEGER,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS route_attractions (
                line_id TEXT NOT NULL,
                attraction_id TEXT NOT NULL REFERENCES attractions(id),
                nearest_stop_id TEXT,
                nearest_stop_name TEXT,
                distance_m REAL,
                walk_minutes REAL,
                sequence_index INTEGER,
                direction TEXT,
                max_walk_m INTEGER,
                PRIMARY KEY (line_id, attraction_id)
            );

            CREATE TABLE IF NOT EXISTS visits (
                attraction_id TEXT PRIMARY KEY REFERENCES attractions(id),
                visited INTEGER NOT NULL DEFAULT 0,
                date_visited TEXT,
                note TEXT,
                favourite INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT DEFAULT (datetime('now'))
            );
            """
        )


def upsert_attraction(attraction):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO attractions (id, name, category, lat, lon, why, history, source_url, osm_type, osm_id)
            VALUES (:id, :name, :category, :lat, :lon, :why, :history, :source_url, :osm_type, :osm_id)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, category=excluded.category, lat=excluded.lat, lon=excluded.lon,
                why=excluded.why, history=excluded.history, source_url=excluded.source_url,
                osm_type=excluded.osm_type, osm_id=excluded.osm_id
            """,
            attraction,
        )


def upsert_route_attraction(row):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO route_attractions
                (line_id, attraction_id, nearest_stop_id, nearest_stop_name, distance_m, walk_minutes, sequence_index, direction, max_walk_m)
            VALUES
                (:line_id, :attraction_id, :nearest_stop_id, :nearest_stop_name, :distance_m, :walk_minutes, :sequence_index, :direction, :max_walk_m)
            ON CONFLICT(line_id, attraction_id) DO UPDATE SET
                nearest_stop_id=excluded.nearest_stop_id, nearest_stop_name=excluded.nearest_stop_name,
                distance_m=excluded.distance_m, walk_minutes=excluded.walk_minutes,
                sequence_index=excluded.sequence_index, direction=excluded.direction,
                max_walk_m=excluded.max_walk_m
            """,
            row,
        )


def get_route_results(line_id):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT a.*, ra.nearest_stop_name, ra.distance_m, ra.walk_minutes,
                   ra.sequence_index, ra.direction,
                   COALESCE(v.visited, 0) AS visited, v.date_visited, v.note,
                   COALESCE(v.favourite, 0) AS favourite
            FROM route_attractions ra
            JOIN attractions a ON a.id = ra.attraction_id
            LEFT JOIN visits v ON v.attraction_id = a.id
            WHERE ra.line_id = ?
            ORDER BY ra.sequence_index ASC
            """,
            (line_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_attraction(attraction_id):
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT a.*, COALESCE(v.visited, 0) AS visited, v.date_visited, v.note,
                   COALESCE(v.favourite, 0) AS favourite
            FROM attractions a
            LEFT JOIN visits v ON v.attraction_id = a.id
            WHERE a.id = ?
            """,
            (attraction_id,),
        ).fetchone()
        return dict(row) if row else None


def get_nearby(attraction_id, radius_m=1000, limit=10):
    attraction = get_attraction(attraction_id)
    if not attraction:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, category, lat, lon FROM attractions WHERE id != ?",
            (attraction_id,),
        ).fetchall()
    from .scoring import haversine_m

    nearby = []
    for r in rows:
        d = haversine_m(attraction["lat"], attraction["lon"], r["lat"], r["lon"])
        if d <= radius_m:
            item = dict(r)
            item["distance_m"] = d
            nearby.append(item)
    nearby.sort(key=lambda x: x["distance_m"])
    return nearby[:limit]


def set_visit(attraction_id, visited, date_visited=None, note=None, favourite=False):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO visits (attraction_id, visited, date_visited, note, favourite, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(attraction_id) DO UPDATE SET
                visited=excluded.visited, date_visited=excluded.date_visited,
                note=excluded.note, favourite=excluded.favourite, updated_at=excluded.updated_at
            """,
            (attraction_id, int(visited), date_visited, note, int(favourite)),
        )


def get_all_visits(category=None, favourites_only=False, line_id=None):
    query = """
        SELECT DISTINCT a.*, v.visited, v.date_visited, v.note, v.favourite
        FROM attractions a
        JOIN visits v ON v.attraction_id = a.id
    """
    conditions = ["v.visited = 1"]
    params = []
    if line_id:
        query += " JOIN route_attractions ra ON ra.attraction_id = a.id"
        conditions.append("ra.line_id = ?")
        params.append(line_id)
    if category:
        conditions.append("a.category = ?")
        params.append(category)
    if favourites_only:
        conditions.append("v.favourite = 1")
    query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY v.date_visited DESC"

    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_routes_explored():
    with get_conn() as conn:
        rows = conn.execute("SELECT DISTINCT line_id FROM route_attractions").fetchall()
        return [r["line_id"] for r in rows]
