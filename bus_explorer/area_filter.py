"""Size and public-accessibility filter for 'park' candidates (OSM
leisure=park/garden/nature_reserve) - see docs/what-can-i-do-spec.md
section 10, and the discussion that replaced it: rather than a dedicated
Parks search with its own access-tag filtering and an 'out geom'
candidate query baked into the main pipeline, every named park/garden/
nature_reserve the normal explore / what-can-i-do searches already find
is filtered down to only those that are (a) over MIN_AREA_M2 and (b)
plausibly open to the public - a single extra, targeted Overpass
geometry query for just the surviving candidates, run after the normal
candidate search rather than replacing it. This is what keeps the parks
category from being a huge list of every named pocket garden (or private
grounds that happen to be tagged leisure=garden).
"""

from . import area, overpass_client

MIN_AREA_M2 = 100_000  # 10 hectares

_PARK_LEISURE_VALUES = {"park", "garden", "nature_reserve"}
_DEFAULT_PUBLIC_LEISURE_VALUES = {"park", "garden"}

# access values that explicitly settle the question either way.
_PRIVATE_ACCESS_VALUES = {"private", "no", "permit"}
_PUBLIC_ACCESS_VALUES = {"yes", "public"}


def is_park_candidate(tags):
    return tags.get("leisure") in _PARK_LEISURE_VALUES


def _is_publicly_accessible(tags):
    """access=private/no/permit is always excluded, access=yes/public is
    always included. Untagged defaults to public for parks/gardens (the
    standard OSM convention), but not for nature reserves, which more
    often restrict access without bothering to tag it.
    """
    access = tags.get("access")
    if access in _PRIVATE_ACCESS_VALUES:
        return False
    if access in _PUBLIC_ACCESS_VALUES:
        return True
    return tags.get("leisure") in _DEFAULT_PUBLIC_LEISURE_VALUES


def _is_sports_or_school_land(tags):
    """Golf courses and school playing fields are large green space but
    not public open space worth a walk, and need explicit exclusion
    regardless of access tagging, since they're rarely tagged correctly
    (see module docstring and the What Can I Do spec, section 10).
    """
    if tags.get("leisure") == "golf_course" or tags.get("sport") == "golf":
        return True
    if tags.get("amenity") == "school" or tags.get("landuse") == "school":
        return True
    return False


def is_walkable_park(tags):
    """Combines is_park_candidate with the access/sports-or-school-land
    checks below - the single predicate park_walks.py needs to decide
    whether a park/garden/nature_reserve is worth routing a walk through,
    without reaching into this module's other, still-private helpers.
    """
    return (
        is_park_candidate(tags)
        and _is_publicly_accessible(tags)
        and not _is_sports_or_school_land(tags)
    )


def filter_by_area(candidates, min_area_m2=MIN_AREA_M2):
    """candidates: list of dicts with at least 'osm_type', 'osm_id', 'tags'.

    Returns a new list with park-tagged candidates removed if they're not
    plausibly public, or below min_area_m2 - or if their area can't be
    measured at all, e.g. a park tagged on a single node with no
    boundary. Non-park candidates pass through unchanged.
    """
    # Access/use filtering first (cheap, no extra query) - keeps the
    # geometry fetch below scoped to only candidates that could still
    # qualify once their size is known.
    candidates = [
        c
        for c in candidates
        if not is_park_candidate(c["tags"])
        or (_is_publicly_accessible(c["tags"]) and not _is_sports_or_school_land(c["tags"]))
    ]

    park_refs = [
        (c["osm_type"], c["osm_id"])
        for c in candidates
        if is_park_candidate(c["tags"]) and c["osm_type"] in ("way", "relation")
    ]
    areas = overpass_client.fetch_areas(park_refs) if park_refs else {}

    kept = []
    for c in candidates:
        if not is_park_candidate(c["tags"]):
            kept.append(c)
            continue
        candidate_area = areas.get((c["osm_type"], c["osm_id"]))
        if candidate_area is not None and candidate_area >= min_area_m2:
            kept.append(c)
    return kept
