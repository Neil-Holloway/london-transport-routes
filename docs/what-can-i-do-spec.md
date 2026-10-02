# What Can I Do — Initial Product Specification

## 1. Purpose

"What Can I Do" answers a different question from Bus Explorer's "What to discover."

The central question is:

> "I've got a free bus pass and a day to fill — what's worth doing, within a single bus journey of here?"

Bus Explorer plans an interesting walking itinerary across several points of interest along a known route. This tool is simpler and more immediate: a single destination, for a single activity, on a single day — not a tour to plan, just something worth going out for.

Motivation: a Freedom Pass gives free travel anywhere in London, but is only useful to someone who already knows somewhere to go and something to do there. This tool exists to close that gap for people with time, curiosity and a bus pass, but no particular plan.

## 2. How this differs from Bus Explorer

| | Bus Explorer ("What to discover") | What Can I Do |
|---|---|---|
| Starting point | A bus route number | A place name (e.g. "Beckenham") |
| Journey shape | Walk the length of one known route | Any single bus journey reachable from stops near the starting point |
| Output | An ordered itinerary of several points of interest to visit in sequence | A browsable list of individual activities — pick one |
| Content | Heritage, history, culture — historic buildings, parks, museums, memorials | Activities — cinemas, sports centres, libraries, swimming pools, bowls greens, theatres, allotments |
| Typical day | A heritage walk combining several nearby discoveries | One destination, one activity |

They are two separate front doors into the same app, sharing the same underlying engine (place-finding, scoring, categorisation, per-visitor visited/favourite tracking) but answering genuinely different questions. Shops, cafes and restaurants are deliberately excluded from both — this is about places to go and things to do, not things to buy or eat.

**A family of four searches.** What Can I Do is the second of what's turning into four related but distinct front doors into the app, all sharing the same engine:

1. **What to discover** (existing Bus Explorer) — route-first, a walking itinerary of heritage points of interest along one known route.
2. **What can I do** (this document) — place-first, single bus journey, a pick-one list of activities.
3. **Parks** — place-first, single bus journey, large open spaces (over 10 hectares) worth a visit in their own right. See §10.
4. **Parkrun** — place-first, single bus journey, simply confirming a parkrun exists and how to reach it. See §11.

Searches 2, 3 and 4 share the same departure-point/single-bus-journey mechanism (§4) — only the candidate-finding query and category list differ between them.

## 3. Main screen

```
What can I do today?

Starting from: [Beckenham]

Maximum walk to the bus stop: 250 m | 500 m | 1 km
Maximum walk from the destination stop: 250 m | 500 m | 1 km | 2 km

Types of activity to include:
☑ Cinemas  ☑ Sports and leisure centres  ☑ Swimming pools
☑ Libraries  ☑ Theatres and community halls  ☑ Bowls greens
☑ Allotments and community gardens  ☑ Markets

[Find something to do]
```

Unlike Bus Explorer, there's no route number to enter — the starting point is a place, and every bus route reachable from it is considered automatically.

## 4. Finding candidate journeys

1. Geocode the starting point to coordinates (e.g. via Nominatim, as already used for debugging).
2. Find stops within walking distance of that point (TfL `StopPoint` search by lat/lon/radius).
3. For each nearby stop, list the bus routes serving it (TfL already returns this per stop).
4. For each route, take the stop sequence **in both directions from the boarding stop** — a single bus journey has a direction, and both are valid "one bus journey" options.
5. Within walking distance of each stop along that outward journey, search for candidate activity places (reusing the existing Overpass-based candidate search, with a different category list — see §5).

A single starting point will usually surface routes in several directions from several nearby stops. Results should make clear which bus (and which direction) reaches each activity, the same way Bus Explorer shows "Bus: 54, Get off: X."

**Performance note:** a busy area can have many routes serving nearby stops. Rather than running a separate Overpass query per route (slow, and the Overpass mirrors are already prone to timeouts under normal load), candidate places should be fetched once for the combined bounding box covering every reachable stop, then attributed back to whichever route(s)/direction(s) can actually reach them.

## 5. What constitutes an activity

Initial categories (deliberately separate from Bus Explorer's heritage categories, and deliberately excluding shops/cafes/restaurants):

- Cinemas
- Sports and leisure centres
- Swimming pools
- Libraries
- Theatres and community halls
- Bowls greens
- Allotments and community gardens
- Markets (shared with Bus Explorer's category — a market is as much an activity as a discovery)
- Recreation grounds (`landuse=recreation_ground` — not currently queried by the existing Overpass clauses used elsewhere in the app, so this is a new candidate tag needed specifically for this search. Distinct from the Parks search in §10, which deliberately excludes recreation grounds — here, a recreation ground is being surfaced as a place to do a sporting activity, not as open space to wander)

As with Bus Explorer's "Unusual/Other," this list should grow from real candidates found during use, not be treated as fixed in advance.

## 6. Results

Results are a simple list, not a sequence — there's no journey order to preserve, since each result is an independent single-destination option rather than a stop along one walk.

```
Beckenham Leisure Centre
Bus 162, towards Eltham — get off at Beckenham Road
450 m / approximately 6 minutes from the stop

Swimming pool, gym and sports hall.

○ Not visited
[More information]
```

This tool's job stops at identification. Opening times, membership terms and prices change often and belong to the venue, not to this app — the result links out to the venue's own website for that (see §7, Sources). This mirrors §8's existing "no automated web-search-derived descriptions" boundary: the tool says *what a place is* and *how to get there*, never *what it currently costs or when it's open*.

Sort order: nearest/soonest-reachable first, or grouped by activity type — to be decided once real results are seen (same experimental approach as Bus Explorer's Interest Score, per spec section 10).

## 7. Activity page

Lighter than Bus Explorer's attraction page — there's no "why it's interesting" cultural framing to construct, since the categories are self-explanatory (a swimming pool is a swimming pool). It should show:

```
Beckenham Leisure Centre

What it is
Swimming pool, gym and sports hall.

Getting there
Bus: 162 towards Eltham
Get off: Beckenham Road
Walk: approximately 450 m / 6 minutes

Sources
Link to OpenStreetMap, and to the venue's own website if OSM has one tagged — that's where to check opening times, membership and prices.
```

## 8. Explicitly out of scope

This is the most important boundary for this tool, learned the hard way from Bus Explorer's web-search fallback experience:

- **No opening hours, membership details, prices, events, or "what's on," live or otherwise.** This isn't just a staleness concern — it's a deliberate design boundary. This tool's job is to tell you a bowling green exists and how to catch a bus there; finding out whether it's open Tuesdays or what membership costs is what the bowling green's own website is for, and the activity page links to it rather than attempting to reproduce it. OpenStreetMap can confirm a library or theatre exists; it cannot say what's happening there today. This tool only ever answers "does something exist here, and how do I get there," never "what's currently happening there" or "what will it cost."
- **No restaurants, cafes or shops.** Deliberately excluded — this is for things to do, not things to buy or eat. There are already many apps for that.
- **No automated web-search-derived descriptions.** Given the false-positive problems encountered enriching Bus Explorer's heritage descriptions (wrong-location matches, irrelevant commercial sites), activity descriptions should stick to OpenStreetMap's own tags (name, opening_hours tag if present, website tag if present) rather than attempting AI/search-derived "why it's interesting" text. These places don't need persuading-you text; they need accurate identification.

## 9. Personal visit record

Reuses Bus Explorer's existing per-attraction, per-visitor visited/note/favourite model exactly as built — visited status belongs to the place, not to which starting point or journey found it, consistent with Bus Explorer's "crucial rule" (spec section 6).

## 10. Parks (third search)

Same departure-point/single-bus-journey mechanism as §4, but a different question: not "what activity can I do" but "what's a large, worthwhile open space I could spend an afternoon in."

**Candidate finding.** OSM tags only — `leisure=park`, `leisure=garden`, `leisure=nature_reserve`. Recreation grounds (`landuse=recreation_ground`) are deliberately excluded from this search even though they're open space, since the point here is parkland to wander, not a sports pitch (a recreation ground can still appear under §5's activity list instead).

**Size filter.** Over 10 hectares. This can't be done from the candidate search used elsewhere in the app, which only fetches a single center point per place (`out center tags`) — there's no geometry to measure. Parks need the full boundary (`out geom`, and relation members for multipolygon parks, which large parks often are) and a computed area (a spherical-polygon area calculation, not simple degree² maths). This is self-contained — same data source, just a heavier query and a filter step — and was chosen over GiGL's "Spaces to Visit" dataset (which had size as a ready-made field) because that dataset was withdrawn from open access in October 2025 with no replacement yet published and no archived copy found.

**Public accessibility.** Tag type alone (`leisure=park` etc.) means "open space," not "open to the public." A second filter on the OSM `access` tag is needed: `access=private`/`no`/`permit` excluded, `access=yes`/`public` included, and untagged treated as public by default for park/garden (standard convention) but not for nature reserves, which more often restrict access without tagging it. Golf courses and school playing fields are large green space but not public open space, and need explicit exclusion regardless of access tagging, since they're rarely tagged correctly. This filtering is inherently cruder than GiGL's dataset, whose selection criterion was already "public accessibility and likelihood people would visit" — this search is rebuilding a rougher version of that judgement from raw tags.

**Naming.** As with every other place in this app, the name shown is OSM's own `name` tag, never invented or inferred.

## 11. Parkrun (fourth search)

Same departure-point/single-bus-journey mechanism as §4, but the simplest possible content: does a parkrun exist here, and how do I reach it.

**Scope — identification only.** This shows that a parkrun exists at a location and a link to its official page. It does not show times, results, or any schedule detail beyond the fixed, well-known convention that parkrun happens Saturdays at 9am — even that may be omitted rather than stated, since the point is existence, not event detail. This is consistent with §8's "no live events" boundary: a parkrun's existence at a place is a stable, slow-changing fact (like "this library exists"), not a live/transient one, provided only the fixed fact is shown.

**Candidate finding.** Not OSM — there's no reliable, standard OSM tag for parkrun locations. The source is parkrun's own public events feed, `https://images.parkrun.com/events.json`, which lists every event with its name and coordinates in one request — no per-event page scraping needed. Candidates are found the same way as any other search in this family: fetch the feed once, then filter to events within walking distance of stops along each reachable single-bus journey, reusing the same distance-filtering approach already used elsewhere (haversine on lat/lon). This is the same "single authoritative source" principle already used for Wikidata descriptions, rather than a web search — and it's a proven approach, already implemented for a different, unrelated project (a personal fitness-tracking tool) that uses this exact feed to find parkrun events near London.

## 12. First development milestone

**What Can I Do 0.1**

It must:

1. Accept a place name as a starting point.
2. Find bus stops within walking distance of it.
3. Find the routes serving those stops, in both directions.
4. Find candidate activity places within walking distance of stops along those journeys.
5. Display them as a simple list with "which bus, which direction, which stop, how far to walk."
6. Allow an activity to be opened for more information.
7. Allow Visited / Not Visited, note and Favourite to be recorded, reusing the existing per-visitor system.

No live opening hours or events, no AI-drafted descriptions, no restaurants/shops, no itinerary-building across multiple activities.

**Success criterion**

Enter "Beckenham," and get a genuinely useful shortlist of single-bus-reachable activities — a leisure centre, a library, a theatre — each clearly labelled with which bus to catch and which direction. That's enough to establish whether this is a worthwhile second way into the app before adding anything further.
