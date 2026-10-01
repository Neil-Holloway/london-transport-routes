"""Postgres (Supabase) persistence for Bus Explorer.

Design note (spec section 6, "crucial rule"): visited status, notes and
favourites are stored keyed by attraction_id alone, in a table separate from
route_attractions. The same attraction discovered from two different bus
routes shares one row here, so marking it visited on one route makes it show
as visited on the other automatically.
"""

import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL")


def _connect():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
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
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS attractions (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    lat DOUBLE PRECISION NOT NULL,
                    lon DOUBLE PRECISION NOT NULL,
                    why TEXT,
                    history TEXT,
                    source_url TEXT,
                    address TEXT,
                    osm_type TEXT,
                    osm_id BIGINT,
                    created_at TIMESTAMPTZ DEFAULT now()
                );

                CREATE TABLE IF NOT EXISTS route_attractions (
                    line_id TEXT NOT NULL,
                    attraction_id TEXT NOT NULL REFERENCES attractions(id),
                    nearest_stop_id TEXT,
                    nearest_stop_name TEXT,
                    distance_m DOUBLE PRECISION,
                    walk_minutes DOUBLE PRECISION,
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
                    updated_at TIMESTAMPTZ DEFAULT now()
                );
                """
            )
            # Migration for databases created before the 'address' column existed.
            cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'attractions'
                """
            )
            existing_cols = {row["column_name"] for row in cur.fetchall()}
            if "address" not in existing_cols:
                cur.execute("ALTER TABLE attractions ADD COLUMN address TEXT")


def upsert_attraction(attraction):
    attraction = {"address": None, **attraction}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO attractions (id, name, category, lat, lon, why, history, source_url, address, osm_type, osm_id)
                VALUES (%(id)s, %(name)s, %(category)s, %(lat)s, %(lon)s, %(why)s, %(history)s, %(source_url)s, %(address)s, %(osm_type)s, %(osm_id)s)
                ON CONFLICT (id) DO UPDATE SET
                    name=excluded.name, category=excluded.category, lat=excluded.lat, lon=excluded.lon,
                    why=excluded.why, history=excluded.history, source_url=excluded.source_url,
                    address=COALESCE(excluded.address, attractions.address),
                    osm_type=excluded.osm_type, osm_id=excluded.osm_id
                """,
                attraction,
            )


def set_address(attraction_id, address):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE attractions SET address = %s WHERE id = %s", (address, attraction_id))


def clear_route_attractions(line_id):
    """Remove all cached route_attractions rows for a line before
    re-exploring it, so stale entries (e.g. from an old dedup pass) don't
    linger alongside freshly computed ones. The underlying attractions rows
    and any visit data are untouched, since attractions can be shared across
    routes.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM route_attractions WHERE line_id = %s", (line_id,))


def upsert_route_attraction(row):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO route_attractions
                    (line_id, attraction_id, nearest_stop_id, nearest_stop_name, distance_m, walk_minutes, sequence_index, direction, max_walk_m)
                VALUES
                    (%(line_id)s, %(attraction_id)s, %(nearest_stop_id)s, %(nearest_stop_name)s, %(distance_m)s, %(walk_minutes)s, %(sequence_index)s, %(direction)s, %(max_walk_m)s)
                ON CONFLICT (line_id, attraction_id) DO UPDATE SET
                    nearest_stop_id=excluded.nearest_stop_id, nearest_stop_name=excluded.nearest_stop_name,
                    distance_m=excluded.distance_m, walk_minutes=excluded.walk_minutes,
                    sequence_index=excluded.sequence_index, direction=excluded.direction,
                    max_walk_m=excluded.max_walk_m
                """,
                row,
            )


def get_route_results(line_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.*, ra.nearest_stop_name, ra.distance_m, ra.walk_minutes,
                       ra.sequence_index, ra.direction,
                       COALESCE(v.visited, 0) AS visited, v.date_visited, v.note,
                       COALESCE(v.favourite, 0) AS favourite
                FROM route_attractions ra
                JOIN attractions a ON a.id = ra.attraction_id
                LEFT JOIN visits v ON v.attraction_id = a.id
                WHERE ra.line_id = %s
                ORDER BY ra.sequence_index ASC
                """,
                (line_id,),
            )
            return [dict(r) for r in cur.fetchall()]


def get_attraction(attraction_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.*, COALESCE(v.visited, 0) AS visited, v.date_visited, v.note,
                       COALESCE(v.favourite, 0) AS favourite
                FROM attractions a
                LEFT JOIN visits v ON v.attraction_id = a.id
                WHERE a.id = %s
                """,
                (attraction_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def get_nearby(attraction_id, radius_m=1000, limit=10):
    attraction = get_attraction(attraction_id)
    if not attraction:
        return []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, category, lat, lon FROM attractions WHERE id != %s",
                (attraction_id,),
            )
            rows = cur.fetchall()
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
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO visits (attraction_id, visited, date_visited, note, favourite, updated_at)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (attraction_id) DO UPDATE SET
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
        conditions.append("ra.line_id = %s")
        params.append(line_id)
    if category:
        conditions.append("a.category = %s")
        params.append(category)
    if favourites_only:
        conditions.append("v.favourite = 1")
    query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY v.date_visited DESC"

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return [dict(r) for r in cur.fetchall()]


def get_routes_explored():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT line_id FROM route_attractions")
            return [r["line_id"] for r in cur.fetchall()]


def max_explored_walk_m(line_id):
    """The largest max_walk_m the pipeline has already been run at for this
    route, or None if it has never been explored. Used to skip re-running
    the (slow) TfL + OpenStreetMap pipeline when the cached results already
    cover the requested walk distance.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(max_walk_m) AS m FROM route_attractions WHERE line_id = %s",
                (line_id,),
            )
            row = cur.fetchone()
            return row["m"] if row and row["m"] is not None else None
