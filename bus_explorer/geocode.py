"""Address resolution for attractions.

Two strategies, cheapest first:
1. Build an address directly from OSM addr:* tags, when the element itself
   carries them (fast, no network call).
2. Reverse-geocode the coordinates via OpenStreetMap's Nominatim service as
   a fallback for elements with no addr:* tags of their own (e.g. a bench or
   memorial inside a park). Nominatim's usage policy caps anonymous use at
   1 request/second, so this is only ever called lazily (once, on first
   view of an attraction) rather than for every candidate during the bulk
   route-exploring pipeline.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "BusExplorer/0.1 (https://github.com/Georege-Holloway/london-transport-routes)"

# Greater London, used as a soft ranking bias for geocode_place (see there).
_LONDON_VIEWBOX = "-0.52,51.70,0.30,51.28"  # left,top,right,bottom


def geocode_place(place_name):
    """Forward-geocode a place name to (lat, lon) via Nominatim's search
    endpoint. Returns None if nothing matches. Biased towards London (and
    restricted to GB) since every search in this app assumes a London bus
    journey, but not hard-restricted to a bounding box (viewbox+bounded=0 is
    a soft preference, not a filter), so a specific place name still
    resolves to its real location rather than being silently dropped if
    Nominatim's idea of "London" doesn't cover it.

    The bias matters: without it, a same-named place elsewhere in GB can
    outrank the London one on Nominatim's own "importance" score - e.g.
    "West Wickham" (the London Borough of Bromley suburb this app means)
    previously lost to "West Wickham, Cambridgeshire", a small village that
    Nominatim ranks as very slightly more important.
    """
    url = (
        "https://nominatim.openstreetmap.org/search?format=jsonv2"
        f"&q={urllib.parse.quote(place_name)}&countrycodes=gb&limit=1"
        f"&viewbox={_LONDON_VIEWBOX}&bounded=0"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except (OSError, ValueError):
        return None

    if not data:
        return None
    return float(data[0]["lat"]), float(data[0]["lon"])


def address_from_tags(tags):
    """Returns a formatted address string from addr:* tags, or None."""
    house = tags.get("addr:housenumber")
    street = tags.get("addr:street")
    city = tags.get("addr:city")
    postcode = tags.get("addr:postcode")

    if not (street or city or postcode):
        return None

    line1 = " ".join(part for part in [house, street] if part)
    parts = [p for p in [line1 or None, city, postcode] if p]
    return ", ".join(parts) if parts else None


def reverse_geocode(lat, lon):
    """Best-effort reverse geocode via Nominatim. Returns a display address
    string, or None if the lookup fails.
    """
    url = (
        "https://nominatim.openstreetmap.org/reverse?format=jsonv2"
        f"&lat={lat}&lon={lon}&zoom=18&addressdetails=1"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except (OSError, ValueError):
        return None

    addr = data.get("address", {})
    if not addr:
        return data.get("display_name")

    house = addr.get("house_number")
    street = addr.get("road")
    city = addr.get("suburb") or addr.get("town") or addr.get("city_district") or addr.get("city")
    postcode = addr.get("postcode")

    line1 = " ".join(part for part in [house, street] if part)
    parts = [p for p in [line1 or None, city, postcode] if p]
    return ", ".join(parts) if parts else data.get("display_name")
