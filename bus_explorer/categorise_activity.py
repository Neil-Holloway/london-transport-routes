"""Maps raw OSM tags onto the What Can I Do activity category list.

Deliberately separate from categorise.py's heritage categories (per the
What Can I Do spec, section 2) - this is a different question (what can I do)
answered from a different set of tags, even though both pull from the same
underlying OSM data.
"""

CATEGORIES = [
    "Cinemas",
    "Sports and leisure centres",
    "Swimming pools",
    "Libraries",
    "Theatres",
    "Bowls greens",
    "Allotments and community gardens",
    "Markets",
    "Recreation grounds",
]


# Tag-derived "what it is" sentence per category - never an invented or
# AI-drafted description, just the category translated into a short phrase
# (spec section 8: activity descriptions stick to OSM's own tags).
DESCRIPTIONS = {
    "Cinemas": "Cinema.",
    "Sports and leisure centres": "Sports or leisure centre.",
    "Swimming pools": "Swimming pool.",
    "Libraries": "Library.",
    "Theatres": "Theatre.",
    "Bowls greens": "Bowls green.",
    "Allotments and community gardens": "Allotments or community garden.",
    "Markets": "Market.",
    "Recreation grounds": "Recreation ground.",
}


def categorise_activity(tags):
    amenity = tags.get("amenity")
    leisure = tags.get("leisure")
    landuse = tags.get("landuse")
    shop = tags.get("shop")

    if amenity == "cinema":
        return "Cinemas"
    if amenity == "library":
        return "Libraries"
    if amenity == "theatre":
        return "Theatres"
    if amenity == "marketplace" or shop == "market":
        return "Markets"
    if leisure == "swimming_pool":
        return "Swimming pools"
    if leisure == "bowling_green":
        return "Bowls greens"
    if leisure in ("sports_centre", "fitness_centre"):
        return "Sports and leisure centres"
    if landuse == "allotments":
        return "Allotments and community gardens"
    if landuse == "recreation_ground":
        return "Recreation grounds"
    return None
