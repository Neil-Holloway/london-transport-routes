"""One-off: undo the first backfill_websearch.py run, which used a flawed
matching check and wrote several wrong-location/irrelevant-source rows (e.g.
"Wellington Park" -> a Somerset council page, "Engine Block" -> a car parts
shop, "Royal Arsenal Thames Path Garden" -> an Agoda hotel listing).

Resets every row that run touched back to the generic "no further details"
placeholder. Identifies them by history IS NULL (only the wikidata-
description and web-search paths set history to NULL) with a source_url
that isn't wikidata.org (the wikidata-description path always points there,
so this leaves genuine wikidata-sourced rows untouched).

Run once, then re-run the fixed backfill_websearch.py:
    source .venv/bin/activate
    export DATABASE_URL='...'
    python3 revert_websearch_backfill.py
"""
from bus_explorer import db
from bus_explorer.enrich import NO_FURTHER_DETAILS


def main():
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, category, why, source_url FROM attractions WHERE history IS NULL"
            )
            rows = cur.fetchall()

    to_revert = [
        r for r in rows
        if r["why"] and " — " in r["why"]
        and (not r["source_url"] or "wikidata.org" not in r["source_url"])
    ]

    print(f"Found {len(to_revert)} rows to revert (out of {len(rows)} with no history).")
    for r in to_revert:
        why = f"{r['name']} is tagged in OpenStreetMap as {r['category'].lower()}."
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE attractions SET why = %s, history = %s, source_url = NULL WHERE id = %s",
                    (why, NO_FURTHER_DETAILS, r["id"]),
                )
        print(f"Reverted: {r['name']}")

    print(f"Reverted {len(to_revert)} rows.")


if __name__ == "__main__":
    main()
