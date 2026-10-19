#!/usr/bin/env python3
"""
Builds the Open Spaces report: every public open space (park/garden/nature
reserve/wood) over 10 hectares in Greater London, with a borough assignment,
whether it's a parkrun venue Neil has personally run (parkrun_matched),
whether the London Loop / Capital Ring / Thames Path passes through or
near it (trail_matched), and a real TfL Journey Planner route + duration
from Beckenham Junction departing 8am on the soonest Saturday
(journey_minutes/journey_summary - see match_journeys). Writes the result
to the open_spaces table (see bus_explorer/db.py), replacing its previous
contents entirely.

This is a standalone, manually-run script (like fetch_routes.py and
backfill_websearch.py) - the live app's /open-spaces page just reads
whatever this last wrote.

Usage:
    source .venv/bin/activate
    export DATABASE_URL='...'
    python3 build_open_spaces.py [--dry-run] [--parkrun-id 215124]

--dry-run runs the whole pipeline and prints summary counts without
writing anything to the database - use this first to sanity-check numbers
before touching production data. It also skips the Beckenham journey
lookups (one TfL API call per open space - by far the slowest step),
since they're not needed to sanity-check candidate/filter counts.
"""

import argparse
import html
import json
import re
import time
import urllib.request
from datetime import date, timedelta

from bus_explorer import area, borough, db, overpass_client, tfl_client
from bus_explorer.area_filter import MIN_AREA_M2, filter_by_area

# Covers Greater London with some margin - wide enough that no part of the
# city is clipped, narrow enough that the Overpass query doesn't balloon
# into querying most of South East England.
GREATER_LONDON_BBOX = (51.28, -0.52, 51.70, 0.33)

TRAIL_NAMES = ["London Loop", "Capital Ring", "Thames Path"]
TRAIL_MATCH_THRESHOLD_M = 75

PARKRUN_EVENTS_URL = "https://images.parkrun.com/events.json"
PARKRUN_UK_COUNTRY_CODE = 97
DEFAULT_PARKRUN_ID = "215124"

USER_AGENT = "Mozilla/5.0 (compatible; BusExplorer open-spaces builder)"

# Beckenham Junction station (rail/tram/bus interchange) - the fixed
# starting point for every open space's journey time/route. Passed to the
# Journey Planner as raw coordinates (see tfl_client.plan_journey) rather
# than a stop ID or the free-text name "Beckenham", which resolves fine
# but isn't guaranteed to pick this exact station as the start point.
BECKENHAM_ORIGIN = (51.411175, -0.026016)
JOURNEY_TIME = "0800"
JOURNEY_REQUEST_PAUSE_S = 0.3  # be polite to TfL's anonymous rate limit


def _next_saturday():
    """The soonest Saturday from today (today itself, if it already is
    one) - the Journey Planner needs a real calendar date to apply the
    right (Saturday) timetable, and re-running the script later should
    always ask about the *next* Saturday rather than a date that's now in
    the past.
    """
    today = date.today()
    return today + timedelta(days=(5 - today.weekday()) % 7)


def fetch_url(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


# --- Step 1-3: candidate parks, filter, geometry ---------------------------


def fetch_candidate_parks():
    min_lat, min_lon, max_lat, max_lon = GREATER_LONDON_BBOX
    # natural=wood: a lot of named London woods (e.g. Hoblingwell Wood,
    # Elmstead Wood) are never tagged leisure=park/garden/nature_reserve at
    # all, so they'd be invisible without this second clause - see
    # area_filter.is_park_candidate, which recognises natural=wood too.
    clauses = [
        '["leisure"~"^(park|garden|nature_reserve)$"]',
        '["natural"="wood"]',
    ]
    # include_relations: some open spaces (e.g. Mitcham Common - several
    # disjoint pieces of common land under one name) are mapped as a
    # multipolygon relation rather than a single way, which the default
    # node/way-only query misses entirely - see overpass_client._build_query.
    return overpass_client.find_candidates(
        min_lat, min_lon, max_lat, max_lon, clauses=clauses, timeout_s=180, include_relations=True
    )


def build_park_records(candidates):
    """Filters candidates down to qualifying open spaces (area_filter.py)
    and attaches each survivor's boundary ring (area.element_ring) for the
    parkrun/trail matching steps.
    """
    survivors = filter_by_area(candidates, min_area_m2=MIN_AREA_M2)

    refs = [(c["osm_type"], c["osm_id"]) for c in survivors if c["osm_type"] in ("way", "relation")]
    geometries = overpass_client.fetch_geometries(refs)
    areas = overpass_client.fetch_areas(refs)

    records = []
    for c in survivors:
        key = (c["osm_type"], c["osm_id"])
        element = geometries.get(key)
        ring = area.element_ring(element) if element else []
        records.append(
            {
                "id": f"osm:{c['osm_type']}:{c['osm_id']}",
                "name": c["tags"]["name"],
                "osm_type": c["osm_type"],
                "osm_id": c["osm_id"],
                "lat": c["lat"],
                "lon": c["lon"],
                "area_m2": areas.get(key, 0.0),
                "ring": ring,
                "borough": None,
                "parkrun_matched": False,
                "parkrun_name": None,
                "trail_matched": False,
                "trails_matched": set(),
                "journey_minutes": None,
                "journey_summary": None,
            }
        )
    return records


# --- Step 4: borough assignment ---------------------------------------------


def assign_boroughs(records):
    for r in records:
        r["borough"] = borough.find_borough(r["lat"], r["lon"])


# --- Step 5: parkrun matching -----------------------------------------------


def fetch_my_parkrun_venues(parkrun_id):
    """Scrapes the public parkrun results-history page for this athlete ID
    and returns the distinct venue slugs they've run (see module docstring
    for why this needs to be a personal history, not just the generic
    venue list). parkrun_id is the numeric athlete ID without the "A"
    prefix shown in the parkrun app/site (e.g. "215124" for "A215124").
    """
    page_url = f"https://www.parkrun.org.uk/parkrunner/{parkrun_id}/all/"
    page_html = fetch_url(page_url).decode("utf-8", errors="replace")
    slugs = set(re.findall(r'href="https://www\.parkrun\.org\.uk/([a-z0-9-]+)/results/', page_html))
    return slugs


def fetch_parkrun_venue_locations():
    """Returns {slug: (name, lat, lon)} for every UK parkrun venue."""
    data = json.loads(fetch_url(PARKRUN_EVENTS_URL))
    venues = {}
    for feature in data["events"]["features"]:
        props = feature["properties"]
        if props.get("countrycode") != PARKRUN_UK_COUNTRY_CODE:
            continue
        lon, lat = feature["geometry"]["coordinates"]
        venues[props["eventname"]] = (html.unescape(props["EventShortName"]), lat, lon)
    return venues


def match_parkruns(records, my_slugs, venue_locations):
    for slug in my_slugs:
        venue = venue_locations.get(slug)
        if not venue:
            continue
        name, lat, lon = venue
        point = (lat, lon)

        matched = None
        for r in records:
            if r["ring"] and area.point_in_ring(point, r["ring"]):
                matched = r
                break

        if matched is None:
            # Fall back to nearest park centre within a small tolerance -
            # covers a parkrun start point mapped just outside the park's
            # own boundary polygon.
            best, best_dist = None, float("inf")
            for r in records:
                dist = area.point_distance_m(point, (r["lat"], r["lon"]))
                if dist < best_dist:
                    best, best_dist = r, dist
            if best is not None and best_dist <= 200:
                matched = best

        if matched is not None:
            matched["parkrun_matched"] = True
            matched["parkrun_name"] = name


# --- Step 6: trail matching -------------------------------------------------


def fetch_trail_points_by_name():
    min_lat, min_lon, max_lat, max_lon = GREATER_LONDON_BBOX
    relations = overpass_client.fetch_trail_relations(TRAIL_NAMES, min_lat, min_lon, max_lat, max_lon)
    points_by_name = {name: [] for name in TRAIL_NAMES}
    for rel in relations:
        rel_name = (rel.get("tags") or {}).get("name", "")
        # Case-insensitive: the London LOOP is tagged in OSM with "LOOP" in
        # capitals (it's an acronym - London Outer Orbital Path), not the
        # title-case "Loop" used for display in this app.
        matched_name = next((n for n in TRAIL_NAMES if n.lower() in rel_name.lower()), None)
        if not matched_name:
            continue
        # A trail relation isn't a single closed ring like a park boundary -
        # it's a chain of way members end-to-end - so just concatenate
        # every member's own geometry into one flat list of (lat, lon)
        # points, rather than using element_ring (which picks a single
        # "largest" member, built for multipolygon parks, not routes).
        for member in rel.get("members", []):
            geometry = member.get("geometry") or []
            points_by_name[matched_name].extend((pt["lat"], pt["lon"]) for pt in geometry if pt)
    return points_by_name


def _ring_bbox(ring, buffer_deg=0.001):
    lats = [p[0] for p in ring]
    lons = [p[1] for p in ring]
    return (min(lats) - buffer_deg, max(lats) + buffer_deg, min(lons) - buffer_deg, max(lons) + buffer_deg)


def match_trails(records, trail_points_by_name):
    bboxes = {id(r): _ring_bbox(r["ring"]) for r in records if r["ring"]}
    for trail_name, points in trail_points_by_name.items():
        for r in records:
            if not r["ring"]:
                continue
            min_lat, max_lat, min_lon, max_lon = bboxes[id(r)]
            nearby = [p for p in points if min_lat <= p[0] <= max_lat and min_lon <= p[1] <= max_lon]
            if not nearby:
                continue
            for p in nearby:
                if area.point_in_ring(p, r["ring"]) or area.min_distance_point_to_ring_m(p, r["ring"]) <= TRAIL_MATCH_THRESHOLD_M:
                    r["trail_matched"] = True
                    r["trails_matched"].add(trail_name)
                    break


# --- Step 7: journey from Beckenham ----------------------------------------


def match_journeys(records, journey_date):
    """Looks up a real Journey Planner route + duration from
    BECKENHAM_ORIGIN to each record's centre point, departing at
    JOURNEY_TIME on journey_date (a Saturday - see _next_saturday). One
    TfL API call per record, so this is by far the slowest step (400+
    records); progress is printed periodically and a record that fails
    just gets a null journey rather than aborting the whole run (e.g.
    genuinely no public transport link at 8am, or a transient TfL API
    hiccup).
    """
    date_str = journey_date.strftime("%Y%m%d")
    failures = 0
    for i, r in enumerate(records, 1):
        result = tfl_client.plan_journey(
            BECKENHAM_ORIGIN[0], BECKENHAM_ORIGIN[1], r["lat"], r["lon"],
            date=date_str, time=JOURNEY_TIME,
        )
        if result:
            r["journey_minutes"] = result["duration_minutes"]
            r["journey_summary"] = " \u2192 ".join(
                leg["instruction"] or leg["mode"].replace("-", " ").title() for leg in result["legs"]
            )
        else:
            r["journey_minutes"] = None
            r["journey_summary"] = None
            failures += 1
        if i % 50 == 0 or i == len(records):
            print(f"  ...{i}/{len(records)} journeys looked up")
        time.sleep(JOURNEY_REQUEST_PAUSE_S)
    if failures:
        print(f"  {failures} open spaces had no journey found (left blank)")


# --- main --------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print summary counts without writing to the DB")
    parser.add_argument("--parkrun-id", default=DEFAULT_PARKRUN_ID, help="Numeric parkrun athlete ID (no 'A' prefix)")
    args = parser.parse_args()

    if not args.dry_run:
        db.init_db()  # ensures the open_spaces table exists - a no-op if
        # the live app has already created it via its own startup.

    print("Fetching candidate parks/gardens/nature reserves across Greater London...")
    candidates = fetch_candidate_parks()
    print(f"  {len(candidates)} raw candidates from Overpass")

    print("Filtering by size (>10ha) and public access...")
    records = build_park_records(candidates)
    print(f"  {len(records)} qualifying open spaces")

    print("Assigning boroughs...")
    assign_boroughs(records)
    before = len(records)
    # A candidate with no containing borough is outside Greater London
    # entirely (the search bbox has a margin beyond the GLA boundary so
    # nothing right at the edge gets clipped) - drop it, since this report
    # is specifically about London, not the Home Counties.
    records = [r for r in records if r["borough"]]
    dropped = before - len(records)
    if dropped:
        print(f"  dropped {dropped} open spaces outside Greater London")

    print(f"Fetching your parkrun history (athlete ID {args.parkrun_id})...")
    my_slugs = fetch_my_parkrun_venues(args.parkrun_id)
    print(f"  {len(my_slugs)} distinct parkrun venues in your history")
    venue_locations = fetch_parkrun_venue_locations()
    match_parkruns(records, my_slugs, venue_locations)
    parkrun_matches = sum(1 for r in records if r["parkrun_matched"])
    print(f"  matched {parkrun_matches} open spaces to a parkrun you've run")

    print("Fetching trail routes (London Loop, Capital Ring, Thames Path)...")
    trail_points = fetch_trail_points_by_name()
    for name, points in trail_points.items():
        print(f"  {name}: {len(points)} geometry points")
    match_trails(records, trail_points)
    trail_matches = sum(1 for r in records if r["trail_matched"])
    print(f"  matched {trail_matches} open spaces to a trail")

    print("\nBy borough:")
    boroughs = sorted({r["borough"] or "(unassigned)" for r in records})
    for b in boroughs:
        count = sum(1 for r in records if (r["borough"] or "(unassigned)") == b)
        print(f"  {b}: {count}")

    if args.dry_run:
        print("\n--dry-run: not writing to the database, skipping journey lookups (slowest step).")
        return

    journey_date = _next_saturday()
    print(f"\nLooking up Saturday 8am journeys from Beckenham ({journey_date.isoformat()})...")
    match_journeys(records, journey_date)

    rows = [
        {
            "id": r["id"],
            "name": r["name"],
            "osm_type": r["osm_type"],
            "osm_id": r["osm_id"],
            "lat": r["lat"],
            "lon": r["lon"],
            "area_m2": r["area_m2"],
            "borough": r["borough"],
            "parkrun_matched": r["parkrun_matched"],
            "parkrun_name": r["parkrun_name"],
            "trail_matched": r["trail_matched"],
            "trails_matched": ",".join(sorted(r["trails_matched"])) or None,
            "journey_minutes": r["journey_minutes"],
            "journey_summary": r["journey_summary"],
        }
        for r in records
    ]
    print(f"\nWriting {len(rows)} open spaces to the database...")
    db.bulk_replace_open_spaces(rows)
    print("Done.")


if __name__ == "__main__":
    main()
