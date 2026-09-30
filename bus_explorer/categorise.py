"""Maps raw OSM tags onto the Bus Explorer category list.

The 'Unusual/Other' fallback is deliberate: the spec requires that nothing
is rejected merely because it doesn't fit an anticipated category.
"""

CATEGORIES = [
    "Historic buildings",
    "Architecture",
    "Museums and galleries",
    "Parks, gardens and woodland",
    "Rivers, canals and waterways",
    "Industrial heritage",
    "Archaeology",
    "Music and cultural history",
    "Memorials and monuments",
    "Public art",
    "Markets",
    "Viewpoints",
    "Churches and religious architecture",
    "Historic cemeteries",
    "Famous people/events",
    "Engineering",
    "Local curiosities",
    "Unusual/Other",
]

_RELIGIOUS_BUILDINGS = {"church", "cathedral", "chapel", "mosque", "synagogue", "temple"}
_INDUSTRIAL_MAN_MADE = {
    "watermill",
    "windmill",
    "water_tower",
    "gasometer",
    "kiln",
    "lighthouse",
    "crane",
}
_HISTORIC_BUILDING_VALUES = {
    "castle",
    "ruins",
    "fort",
    "fortification",
    "manor",
    "tower_house",
    "city_gate",
    "citywalls",
    "house",
    "building",
    "yes",
    "heritage",
}


def categorise(tags):
    historic = tags.get("historic")
    tourism = tags.get("tourism")
    leisure = tags.get("leisure")
    amenity = tags.get("amenity")
    man_made = tags.get("man_made")
    natural = tags.get("natural")
    waterway = tags.get("waterway")
    landuse = tags.get("landuse")
    shop = tags.get("shop")
    building = tags.get("building")
    memorial = tags.get("memorial")

    if historic in ("memorial", "wayside_cross", "wayside_shrine", "milestone") or memorial:
        return "Memorials and monuments"
    if tourism == "artwork":
        return "Public art"
    if tourism in ("museum", "gallery"):
        return "Museums and galleries"
    if tourism == "viewpoint":
        return "Viewpoints"
    if historic == "archaeological_site":
        return "Archaeology"
    if historic in _HISTORIC_BUILDING_VALUES:
        return "Historic buildings"
    if landuse == "cemetery" or amenity == "grave_yard":
        return "Historic cemeteries"
    if amenity == "place_of_worship" or building in _RELIGIOUS_BUILDINGS:
        return "Churches and religious architecture"
    if leisure in ("park", "garden", "nature_reserve"):
        return "Parks, gardens and woodland"
    if waterway or natural == "water":
        return "Rivers, canals and waterways"
    if man_made in _INDUSTRIAL_MAN_MADE:
        return "Industrial heritage"
    if man_made in ("bridge", "tower") or tags.get("bridge"):
        return "Engineering"
    if amenity == "marketplace" or shop == "market":
        return "Markets"
    if historic:
        return "Historic buildings"
    return "Unusual/Other"
