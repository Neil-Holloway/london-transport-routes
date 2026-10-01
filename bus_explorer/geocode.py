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
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
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
