"""One-off: for attractions whose history is still the generic "No further
details..." placeholder (Wikipedia and Wikidata had nothing), try the new
web-search fallback and update why/history/source_url if a real match is
found.

Run once after setting BRAVE_API_KEY (and DATABASE_URL), then delete:
    source .venv/bin/activate
    export DATABASE_URL='...'
    export BRAVE_API_KEY='...'
    python3 backfill_websearch.py
"""
from bus_explorer import db, websearch
from bus_explorer.enrich import NO_FURTHER_DETAILS


def main():
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name FROM attractions WHERE history = %s",
                (NO_FURTHER_DETAILS,),
            )
            rows = cur.fetchall()

    print(f"Found {len(rows)} attractions with no further details.")
    updated = 0
    for row in rows:
        result = websearch.search(row["name"])
        if not result:
            continue
        why = f"{row['name']} — {result['snippet']}"
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE attractions SET why = %s, history = NULL, source_url = %s WHERE id = %s",
                    (why, result["url"], row["id"]),
                )
        updated += 1
        print(f"Updated: {row['name']} -> {result['url']}")

    print(f"Updated {updated} of {len(rows)}, skipped {len(rows) - updated}.")


if __name__ == "__main__":
    main()
