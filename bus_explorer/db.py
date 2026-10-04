"""Postgres (Supabase) persistence for Bus Explorer.

Design note (spec section 6, "crucial rule"): visited status, notes and
favourites are stored keyed by (attraction_id, visitor_name), in a table
separate from route_attractions. The same attraction discovered from two
different bus routes shares one row here per visitor, so marking it visited
on one route makes it show as visited on the other automatically - but only
for that visitor. There is no login/password - visitor_name is just a
nickname a person picks once (see app.py's cookie-based identification), so
this is trust-based separation for friends sharing one deployment, not real
per-user security.
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

                CREATE TABLE IF NOT EXISTS journey_attractions (
                    search_key TEXT NOT NULL,
                    attraction_id TEXT NOT NULL REFERENCES attractions(id),
                    line_id TEXT,
                    direction TEXT,
                    destination_stop_name TEXT,
                    distance_m DOUBLE PRECISION,
                    walk_minutes DOUBLE PRECISION,
                    max_walk_to_stop_m INTEGER,
                    max_walk_from_stop_m INTEGER,
                    PRIMARY KEY (search_key, attraction_id)
                );

                CREATE TABLE IF NOT EXISTS car_attractions (
                    search_key TEXT NOT NULL,
                    attraction_id TEXT NOT NULL REFERENCES attractions(id),
                    distance_m DOUBLE PRECISION,
                    max_radius_m INTEGER,
                    PRIMARY KEY (search_key, attraction_id)
                );

                CREATE TABLE IF NOT EXISTS train_attractions (
                    search_key TEXT NOT NULL,
                    attraction_id TEXT NOT NULL REFERENCES attractions(id),
                    line_id TEXT,
                    direction TEXT,
                    destination_stop_name TEXT,
                    distance_m DOUBLE PRECISION,
                    walk_minutes DOUBLE PRECISION,
                    max_walk_to_station_m INTEGER,
                    max_stops INTEGER,
                    PRIMARY KEY (search_key, attraction_id)
                );

                CREATE TABLE IF NOT EXISTS park_walks (
                    search_key TEXT NOT NULL,
                    park_name TEXT NOT NULL,
                    distance_m DOUBLE PRECISION,
                    duration_s DOUBLE PRECISION,
                    start_lat DOUBLE PRECISION,
                    start_lon DOUBLE PRECISION,
                    route_geometry JSONB,
                    instructions JSONB,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    PRIMARY KEY (search_key)
                );

                CREATE TABLE IF NOT EXISTS visits (
                    attraction_id TEXT NOT NULL REFERENCES attractions(id),
                    visitor_name TEXT NOT NULL,
                    visited INTEGER NOT NULL DEFAULT 0,
                    date_visited TEXT,
                    note TEXT,
                    favourite INTEGER NOT NULL DEFAULT 0,
                    ignored INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMPTZ DEFAULT now(),
                    PRIMARY KEY (attraction_id, visitor_name)
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

            # Migration for databases created before per-visitor tracking:
            # visits used to be keyed by attraction_id alone (one shared
            # visited/favourite per place for everyone). Add visitor_name,
            # attribute all pre-existing rows to "Neil" (this app's first
            # user, before nicknames existed), and widen the primary key.
            cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'visits'
                """
            )
            visit_cols = {row["column_name"] for row in cur.fetchall()}
            if "visitor_name" not in visit_cols:
                cur.execute("ALTER TABLE visits ADD COLUMN visitor_name TEXT")
                cur.execute("UPDATE visits SET visitor_name = 'Neil' WHERE visitor_name IS NULL")
                cur.execute("ALTER TABLE visits ALTER COLUMN visitor_name SET NOT NULL")
                cur.execute("ALTER TABLE visits DROP CONSTRAINT visits_pkey")
                cur.execute("ALTER TABLE visits ADD PRIMARY KEY (attraction_id, visitor_name)")

            # Migration for databases created before "ignore this item"
            # existed - a results-page action to hide a place from listings
            # without affecting its visited/favourite/note state.
            if "ignored" not in visit_cols:
                cur.execute("ALTER TABLE visits ADD COLUMN ignored INTEGER NOT NULL DEFAULT 0")

            # Migration for park_walks rows created before step-by-step
            # walking instructions were captured alongside the route geometry.
            cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'park_walks'
                """
            )
            park_walk_cols = {row["column_name"] for row in cur.fetchall()}
            if "instructions" not in park_walk_cols:
                cur.execute("ALTER TABLE park_walks ADD COLUMN instructions JSONB")

            # Migration for park_walks rows created before Park Walks was
            # simplified from "search near a starting place, joining a
            # second park if needed" down to "enter a park's own name" -
            # place_name (the old starting-place search term) becomes
            # park_name (the park itself); mode/park_names were only
            # meaningful for the old joined-route case and no longer apply.
            if "park_name" not in park_walk_cols and "place_name" in park_walk_cols:
                cur.execute("ALTER TABLE park_walks RENAME COLUMN place_name TO park_name")
            if "mode" in park_walk_cols:
                cur.execute("ALTER TABLE park_walks DROP COLUMN mode")
            if "park_names" in park_walk_cols:
                cur.execute("ALTER TABLE park_walks DROP COLUMN park_names")

            # Migration for park_walks rows created before the 5/10/15km
            # target-distance choice was dropped in favour of always
            # generating one route sized to the specific park's own paths -
            # target_distance_m no longer means anything, and dropping it
            # changes the primary key from (search_key, target_distance_m)
            # to search_key alone, which could otherwise collide across the
            # old per-distance rows. park_walks is just a cache of
            # generated routes (regenerated on next search), so clearing it
            # outright is simpler and safer than picking one surviving row
            # per search_key to keep.
            if "target_distance_m" in park_walk_cols:
                cur.execute("DELETE FROM park_walks")
                cur.execute("ALTER TABLE park_walks DROP CONSTRAINT park_walks_pkey")
                cur.execute("ALTER TABLE park_walks DROP COLUMN target_distance_m")
                cur.execute("ALTER TABLE park_walks ADD PRIMARY KEY (search_key)")


def _run(conn, sql, params):
    """Run one statement, either on a caller-supplied connection (left open
    and uncommitted - the caller owns its lifecycle, e.g. one connection
    shared across a whole batch of upserts) or, if none is given, on a
    fresh connection opened and committed just for this one statement.
    """
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        return
    with get_conn() as owned:
        with owned.cursor() as cur:
            cur.execute(sql, params)


def upsert_attraction(attraction, conn=None):
    attraction = {"address": None, **attraction}
    _run(
        conn,
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


def bulk_upsert_attractions(attractions, conn):
    """Same upsert as upsert_attraction, but for a whole batch in one round
    trip instead of one per row. A well-connected interchange (e.g.
    Lewisham: ~25 distinct bus lines) can have hundreds of surviving
    candidates - doing one DB round trip per row made the write phase alone
    take long enough (on top of the Overpass fetch that comes before it) to
    blow gunicorn's worker timeout, even after that fetch had already
    succeeded. Requires an explicit conn (unlike the single-row functions)
    since this is only ever meant to be called as part of a larger batch.
    """
    if not attractions:
        return
    rows = [
        (
            a["id"], a["name"], a["category"], a["lat"], a["lon"],
            a.get("why"), a.get("history"), a.get("source_url"),
            a.get("address"), a.get("osm_type"), a.get("osm_id"),
        )
        for a in attractions
    ]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO attractions (id, name, category, lat, lon, why, history, source_url, address, osm_type, osm_id)
            VALUES %s
            ON CONFLICT (id) DO UPDATE SET
                name=excluded.name, category=excluded.category, lat=excluded.lat, lon=excluded.lon,
                why=excluded.why, history=excluded.history, source_url=excluded.source_url,
                address=COALESCE(excluded.address, attractions.address),
                osm_type=excluded.osm_type, osm_id=excluded.osm_id
            """,
            rows,
        )


def set_address(attraction_id, address):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE attractions SET address = %s WHERE id = %s", (address, attraction_id))


def clear_route_attractions(line_id, conn=None):
    """Remove all cached route_attractions rows for a line before
    re-exploring it, so stale entries (e.g. from an old dedup pass) don't
    linger alongside freshly computed ones. The underlying attractions rows
    and any visit data are untouched, since attractions can be shared across
    routes.
    """
    _run(conn, "DELETE FROM route_attractions WHERE line_id = %s", (line_id,))


def upsert_route_attraction(row, conn=None):
    _run(
        conn,
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


def bulk_upsert_route_attractions(rows, conn):
    """Batched equivalent of upsert_route_attraction - see
    bulk_upsert_attractions for why this exists.
    """
    if not rows:
        return
    values = [
        (
            r["line_id"], r["attraction_id"], r["nearest_stop_id"], r["nearest_stop_name"],
            r["distance_m"], r["walk_minutes"], r["sequence_index"], r["direction"], r["max_walk_m"],
        )
        for r in rows
    ]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO route_attractions
                (line_id, attraction_id, nearest_stop_id, nearest_stop_name, distance_m, walk_minutes, sequence_index, direction, max_walk_m)
            VALUES %s
            ON CONFLICT (line_id, attraction_id) DO UPDATE SET
                nearest_stop_id=excluded.nearest_stop_id, nearest_stop_name=excluded.nearest_stop_name,
                distance_m=excluded.distance_m, walk_minutes=excluded.walk_minutes,
                sequence_index=excluded.sequence_index, direction=excluded.direction,
                max_walk_m=excluded.max_walk_m
            """,
            values,
        )


def clear_journey_attractions(search_key, conn=None):
    """Same purpose as clear_route_attractions, for What Can I Do searches:
    remove stale placement rows for this starting place before re-running
    the pipeline, without touching the shared attractions/visits data.
    """
    _run(conn, "DELETE FROM journey_attractions WHERE search_key = %s", (search_key,))


def upsert_journey_attraction(row, conn=None):
    _run(
        conn,
        """
        INSERT INTO journey_attractions
            (search_key, attraction_id, line_id, direction, destination_stop_name,
             distance_m, walk_minutes, max_walk_to_stop_m, max_walk_from_stop_m)
        VALUES
            (%(search_key)s, %(attraction_id)s, %(line_id)s, %(direction)s, %(destination_stop_name)s,
             %(distance_m)s, %(walk_minutes)s, %(max_walk_to_stop_m)s, %(max_walk_from_stop_m)s)
        ON CONFLICT (search_key, attraction_id) DO UPDATE SET
            line_id=excluded.line_id, direction=excluded.direction,
            destination_stop_name=excluded.destination_stop_name,
            distance_m=excluded.distance_m, walk_minutes=excluded.walk_minutes,
            max_walk_to_stop_m=excluded.max_walk_to_stop_m,
            max_walk_from_stop_m=excluded.max_walk_from_stop_m
        """,
        row,
    )


def bulk_upsert_journey_attractions(rows, conn):
    """Batched equivalent of upsert_journey_attraction - see
    bulk_upsert_attractions for why this exists. This is the table What Can
    I Do writes to, so it's the one that actually mattered for the Lewisham
    timeout.
    """
    if not rows:
        return
    values = [
        (
            r["search_key"], r["attraction_id"], r["line_id"], r["direction"],
            r["destination_stop_name"], r["distance_m"], r["walk_minutes"],
            r["max_walk_to_stop_m"], r["max_walk_from_stop_m"],
        )
        for r in rows
    ]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO journey_attractions
                (search_key, attraction_id, line_id, direction, destination_stop_name,
                 distance_m, walk_minutes, max_walk_to_stop_m, max_walk_from_stop_m)
            VALUES %s
            ON CONFLICT (search_key, attraction_id) DO UPDATE SET
                line_id=excluded.line_id, direction=excluded.direction,
                destination_stop_name=excluded.destination_stop_name,
                distance_m=excluded.distance_m, walk_minutes=excluded.walk_minutes,
                max_walk_to_stop_m=excluded.max_walk_to_stop_m,
                max_walk_from_stop_m=excluded.max_walk_from_stop_m
            """,
            values,
        )


def get_journey_results(search_key, visitor_name, include_ignored=False):
    query = """
        SELECT a.*, ja.line_id, ja.direction, ja.destination_stop_name,
               ja.distance_m, ja.walk_minutes,
               COALESCE(v.visited, 0) AS visited, v.date_visited, v.note,
               COALESCE(v.favourite, 0) AS favourite, COALESCE(v.ignored, 0) AS ignored
        FROM journey_attractions ja
        JOIN attractions a ON a.id = ja.attraction_id
        LEFT JOIN visits v ON v.attraction_id = a.id AND v.visitor_name = %s
        WHERE ja.search_key = %s
    """
    if not include_ignored:
        query += " AND COALESCE(v.ignored, 0) = 0"
    query += " ORDER BY ja.distance_m ASC"

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (visitor_name, search_key))
            return [dict(r) for r in cur.fetchall()]


def max_explored_journey_walk_m(search_key):
    """Largest (max_walk_to_stop_m, max_walk_from_stop_m) this search has
    already been run at, or None if never explored - same caching purpose
    as max_explored_walk_m, for What Can I Do searches.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT MAX(max_walk_to_stop_m) AS to_m, MAX(max_walk_from_stop_m) AS from_m
                FROM journey_attractions WHERE search_key = %s
                """,
                (search_key,),
            )
            row = cur.fetchone()
            if not row or row["to_m"] is None:
                return None
            return row["to_m"], row["from_m"]


def clear_train_attractions(search_key, conn=None):
    """Same purpose as clear_journey_attractions/clear_car_attractions, for
    Explore by train searches.
    """
    _run(conn, "DELETE FROM train_attractions WHERE search_key = %s", (search_key,))


def bulk_upsert_train_attractions(rows, conn):
    """Batched equivalent of a single-row train_attractions upsert - see
    bulk_upsert_attractions for why this exists.
    """
    if not rows:
        return
    values = [
        (
            r["search_key"], r["attraction_id"], r["line_id"], r["direction"],
            r["destination_stop_name"], r["distance_m"], r["walk_minutes"],
            r["max_walk_to_station_m"], r["max_stops"],
        )
        for r in rows
    ]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO train_attractions
                (search_key, attraction_id, line_id, direction, destination_stop_name,
                 distance_m, walk_minutes, max_walk_to_station_m, max_stops)
            VALUES %s
            ON CONFLICT (search_key, attraction_id) DO UPDATE SET
                line_id=excluded.line_id, direction=excluded.direction,
                destination_stop_name=excluded.destination_stop_name,
                distance_m=excluded.distance_m, walk_minutes=excluded.walk_minutes,
                max_walk_to_station_m=excluded.max_walk_to_station_m,
                max_stops=excluded.max_stops
            """,
            values,
        )


def get_train_results(search_key, visitor_name, include_ignored=False):
    query = """
        SELECT a.*, ta.line_id, ta.direction, ta.destination_stop_name,
               ta.distance_m, ta.walk_minutes,
               COALESCE(v.visited, 0) AS visited, v.date_visited, v.note,
               COALESCE(v.favourite, 0) AS favourite, COALESCE(v.ignored, 0) AS ignored
        FROM train_attractions ta
        JOIN attractions a ON a.id = ta.attraction_id
        LEFT JOIN visits v ON v.attraction_id = a.id AND v.visitor_name = %s
        WHERE ta.search_key = %s
    """
    if not include_ignored:
        query += " AND COALESCE(v.ignored, 0) = 0"
    query += " ORDER BY ta.distance_m ASC"

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (visitor_name, search_key))
            return [dict(r) for r in cur.fetchall()]


def max_explored_train_stops(search_key):
    """Largest (max_walk_to_station_m, max_stops) this search has already
    been run at, or None if never explored - same caching purpose as
    max_explored_car_radius_m.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT MAX(max_walk_to_station_m) AS walk_m, MAX(max_stops) AS stops
                FROM train_attractions WHERE search_key = %s
                """,
                (search_key,),
            )
            row = cur.fetchone()
            if not row or row["walk_m"] is None:
                return None
            return row["walk_m"], row["stops"]


def upsert_park_walk(row, conn=None):
    """Caches one generated Park Walks route, keyed by search_key -
    re-searching the same park reuses the cached route rather than calling
    the routing engine again (see routing_client.py's ORS rate limits).
    """
    _run(
        conn,
        """
        INSERT INTO park_walks
            (search_key, park_name,
             distance_m, duration_s, start_lat, start_lon, route_geometry, instructions)
        VALUES
            (%(search_key)s, %(park_name)s,
             %(distance_m)s, %(duration_s)s, %(start_lat)s, %(start_lon)s, %(route_geometry)s, %(instructions)s)
        ON CONFLICT (search_key) DO UPDATE SET
            park_name=excluded.park_name,
            distance_m=excluded.distance_m, duration_s=excluded.duration_s,
            start_lat=excluded.start_lat, start_lon=excluded.start_lon,
            route_geometry=excluded.route_geometry, instructions=excluded.instructions
        """,
        {
            **row,
            "route_geometry": psycopg2.extras.Json(row["route_geometry"]),
            "instructions": psycopg2.extras.Json(row.get("instructions") or []),
        },
    )


def get_park_walk(search_key):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM park_walks WHERE search_key = %s",
                (search_key,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def clear_car_attractions(search_key, conn=None):
    """Same purpose as clear_route_attractions/clear_journey_attractions,
    for Explore by car searches.
    """
    _run(conn, "DELETE FROM car_attractions WHERE search_key = %s", (search_key,))


def bulk_upsert_car_attractions(rows, conn):
    """Batched equivalent of a single-row car_attractions upsert - see
    bulk_upsert_attractions for why this exists.
    """
    if not rows:
        return
    values = [
        (r["search_key"], r["attraction_id"], r["distance_m"], r["max_radius_m"])
        for r in rows
    ]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO car_attractions (search_key, attraction_id, distance_m, max_radius_m)
            VALUES %s
            ON CONFLICT (search_key, attraction_id) DO UPDATE SET
                distance_m=excluded.distance_m, max_radius_m=excluded.max_radius_m
            """,
            values,
        )


def get_car_results(search_key, visitor_name, include_ignored=False):
    query = """
        SELECT a.*, ca.distance_m,
               COALESCE(v.visited, 0) AS visited, v.date_visited, v.note,
               COALESCE(v.favourite, 0) AS favourite, COALESCE(v.ignored, 0) AS ignored
        FROM car_attractions ca
        JOIN attractions a ON a.id = ca.attraction_id
        LEFT JOIN visits v ON v.attraction_id = a.id AND v.visitor_name = %s
        WHERE ca.search_key = %s
    """
    if not include_ignored:
        query += " AND COALESCE(v.ignored, 0) = 0"
    query += " ORDER BY ca.distance_m ASC"

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (visitor_name, search_key))
            return [dict(r) for r in cur.fetchall()]


def max_explored_car_radius_m(search_key):
    """Largest radius_m this place has already been searched at, or None if
    never searched - same caching purpose as max_explored_walk_m.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(max_radius_m) AS m FROM car_attractions WHERE search_key = %s",
                (search_key,),
            )
            row = cur.fetchone()
            return row["m"] if row and row["m"] is not None else None


def get_route_results(line_id, visitor_name, include_ignored=False):
    query = """
        SELECT a.*, ra.nearest_stop_name, ra.distance_m, ra.walk_minutes,
               ra.sequence_index, ra.direction,
               COALESCE(v.visited, 0) AS visited, v.date_visited, v.note,
               COALESCE(v.favourite, 0) AS favourite, COALESCE(v.ignored, 0) AS ignored
        FROM route_attractions ra
        JOIN attractions a ON a.id = ra.attraction_id
        LEFT JOIN visits v ON v.attraction_id = a.id AND v.visitor_name = %s
        WHERE ra.line_id = %s
    """
    if not include_ignored:
        query += " AND COALESCE(v.ignored, 0) = 0"
    query += " ORDER BY ra.sequence_index ASC"

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (visitor_name, line_id))
            return [dict(r) for r in cur.fetchall()]


def get_attraction(attraction_id, visitor_name):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.*, COALESCE(v.visited, 0) AS visited, v.date_visited, v.note,
                       COALESCE(v.favourite, 0) AS favourite
                FROM attractions a
                LEFT JOIN visits v ON v.attraction_id = a.id AND v.visitor_name = %s
                WHERE a.id = %s
                """,
                (visitor_name, attraction_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def get_nearby(attraction_id, radius_m=1000, limit=10):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT lat, lon FROM attractions WHERE id = %s", (attraction_id,))
            attraction = cur.fetchone()
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


def set_visited(attraction_id, visitor_name, visited):
    """Quick toggle for the results-page 'mark visited' button - a partial
    update that only touches the visited flag, leaving any existing note,
    favourite or ignored status on the row alone (unlike a full upsert of
    every visit field at once).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO visits (attraction_id, visitor_name, visited, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (attraction_id, visitor_name) DO UPDATE SET
                    visited=excluded.visited, updated_at=excluded.updated_at
                """,
                (attraction_id, visitor_name, int(visited)),
            )


def set_ignored(attraction_id, visitor_name, ignored):
    """Hides (or restores) an attraction from this visitor's route/activity
    results listings, independently of visited/favourite/note.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO visits (attraction_id, visitor_name, ignored, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (attraction_id, visitor_name) DO UPDATE SET
                    ignored=excluded.ignored, updated_at=excluded.updated_at
                """,
                (attraction_id, visitor_name, int(ignored)),
            )


def set_note_favourite(attraction_id, visitor_name, note, favourite):
    """Partial update for the attraction detail page's note/favourite form -
    leaves visited/ignored alone (those are now set from the results-page
    quick actions, see set_visited/set_ignored).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO visits (attraction_id, visitor_name, note, favourite, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (attraction_id, visitor_name) DO UPDATE SET
                    note=excluded.note, favourite=excluded.favourite, updated_at=excluded.updated_at
                """,
                (attraction_id, visitor_name, note, int(favourite)),
            )


def get_all_visits(visitor_name, category=None, favourites_only=False, line_id=None):
    query = """
        SELECT DISTINCT a.*, v.visited, v.date_visited, v.note, v.favourite
        FROM attractions a
        JOIN visits v ON v.attraction_id = a.id
    """
    # "Ignored" is a hide-everywhere action, so it's excluded here too, not
    # just from the route/activity results listings it was added for.
    conditions = ["v.visited = 1", "v.visitor_name = %s", "COALESCE(v.ignored, 0) = 0"]
    params = [visitor_name]
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
    # Mark-visited no longer records a date (see set_visited), so
    # date_visited is now always empty for newly visited places - order by
    # when the visit row last changed instead, which stays meaningful.
    query += " ORDER BY v.updated_at DESC"

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
