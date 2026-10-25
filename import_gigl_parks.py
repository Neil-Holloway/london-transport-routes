#!/usr/bin/env python3
"""
Loads the GiGL >10ha open-space checklist into the gigl_parks table (see
bus_explorer/db.py). The data itself - data/gigl_parks.json - was extracted
once from two static docx exports Neil had outside this repo:

  - "london-green-spaces-all-visits-ticked.docx": the full 359-site GiGL/GLA
    "Spaces to Visit 2025" checklist, each site's tick colour(s) recorded
    (parkrun / trail / other visit / none - a site can carry more than one).
  - "sorted remaining-parks-saturday-transport-sorted-by-time.docx": for the
    208 sites that were unticked in that checklist, a pre-planned Saturday
    8am public transport journey from Beckenham (BR3 4UE) to each one.

Those two exports matched up perfectly 1:1 by name for the unticked sites,
so they were merged into data/gigl_parks.json (one row per site) by a
one-off script rather than re-parsing the docx files on every run - this
importer just loads that merged JSON into the database.

This is a standalone, manually-run script (like build_open_spaces.py) -
the live app's /parks-visited page just reads whatever this last wrote.

Usage:
    source .venv/bin/activate
    export DATABASE_URL='...'
    python3 import_gigl_parks.py [--dry-run]
"""

import argparse
import json
from pathlib import Path

from bus_explorer import db

DATA_PATH = Path(__file__).parent / "data" / "gigl_parks.json"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rows = json.loads(DATA_PATH.read_text())

    print(f"Loaded {len(rows)} parks from {DATA_PATH}")
    print(f"  parkrun: {sum(1 for r in rows if 'parkrun' in (r['source_ticks'] or ''))}")
    print(f"  trail:   {sum(1 for r in rows if 'trail' in (r['source_ticks'] or ''))}")
    print(f"  other:   {sum(1 for r in rows if 'other' in (r['source_ticks'] or ''))}")
    print(f"  unticked (with journey plan): {sum(1 for r in rows if not r['source_ticks'])}")

    if args.dry_run:
        print("--dry-run: not writing to the database")
        return

    db.bulk_replace_gigl_parks(rows)
    print("Written to gigl_parks table.")


if __name__ == "__main__":
    main()
