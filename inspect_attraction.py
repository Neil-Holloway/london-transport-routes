"""One-off: print full details for attractions matching a name, to sanity-
check a web-search-fallback match without needing direct DB access.

Usage:
    python3 inspect_attraction.py "Dante" "Crystal Palace Aquarium"
"""
import sys

from bus_explorer import db


def main():
    names = sys.argv[1:]
    if not names:
        print("Usage: python3 inspect_attraction.py \"Name One\" \"Name Two\" ...")
        return

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, category, lat, lon, address, why, source_url FROM attractions WHERE name = ANY(%s)",
                (names,),
            )
            rows = cur.fetchall()

    for r in rows:
        print("-" * 60)
        print(f"name:       {r['name']}")
        print(f"category:   {r['category']}")
        print(f"location:   {r['lat']}, {r['lon']}")
        print(f"address:    {r['address']}")
        print(f"why:        {r['why']}")
        print(f"source_url: {r['source_url']}")


if __name__ == "__main__":
    main()
