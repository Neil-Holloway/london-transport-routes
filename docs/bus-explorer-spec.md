# Bus Explorer — Initial Product Specification

## 1. Purpose

Bus Explorer helps a user discover interesting places accessible from a UK bus route.

The central question is:

> "If I travel on this bus, what interesting things could I get off and explore?"

It should deliberately go beyond conventional tourist attractions to uncover local history, architecture, unusual objects, parks, industrial remains, monuments and other easily overlooked places.

## 2. Main screen

The opening screen should be deliberately simple:

```
Where shall I explore?

Bus route: [54]
Area: [London]

Maximum walk from route:
250 m | 500 m | 1 km | 2 km

[Explore this route]
```

Initially, route number and area are both required because route numbers are repeated throughout Britain.

## 3. Route results

The system identifies the route and displays its principal journey:

```
54 — Woolwich → Elmers End
```

Attractions appear in geographical/journey order, rather than simply ranked from best to worst.

Each result card contains:

```
Bowie Bandstand
Croydon Road Recreation Ground
🎵 Music · History · Architecture
200 m / approximately 3 minutes from the bus

David Bowie performed here at the Growth Summer Festival in 1969. The
unusual Edwardian bandstand is now Grade II listed.

○ Not visited
[More information]
```

The user can choose: Highlights | All discoveries | Unvisited only

## 4. What constitutes an interesting place

Initial categories:

- Historic buildings
- Architecture
- Museums and galleries
- Parks, gardens and woodland
- Rivers, canals and waterways
- Industrial heritage
- Archaeology
- Music and cultural history
- Memorials and monuments
- Public art
- Markets
- Viewpoints
- Churches and religious architecture
- Historic cemeteries
- Famous people/events
- Engineering
- Local curiosities
- Unusual/Other

The last category is important. The software shouldn't reject something merely because we hadn't anticipated its category.

## 5. Attraction page

Selecting an attraction opens its individual record.

```
Bowie Bandstand
Croydon Road Recreation Ground, Beckenham

Why it's interesting
A concise explanation of why someone might want to visit.

History
A fuller account, with length determined by how much worthwhile
information exists.

Things to notice
Specific details worth looking for while there.

Getting there
Bus: 54
Get off: Croydon Road / War Memorial
Walk: approximately 200 m / 3 minutes
```

Eventually this should include a small map and walking directions.

```
Practical information
Opening/access restrictions where relevant, admission charge where
applicable, accessibility information when available and approximate
visit duration.

Sources
Links to authoritative sources used to construct the description.

Nearby
Other Bus Explorer discoveries within perhaps 1 km.
```

## 6. Personal visit record

Every attraction has a persistent status:

```
○ Not visited
```

or

```
✓ Visited
```

Selecting Visited optionally allows:

```
Date visited: 30 September 2026
My note: ____________________
★ Favourite
```

The date should be optional because users may want to record somewhere they visited years ago without remembering exactly when.

The note is also optional.

**Crucial rule**

Visited status belongs to the attraction, not the bus route.

If the Bowie Bandstand is accessible from routes 54 and 194, marking it visited while exploring the 54 means it will also appear as visited when looking at the 194.

## 7. My Visits

A separate section provides a personal exploration record.

```
My discoveries
127 places visited
```

It can be filtered by: Category | Area | Bus route | Date | Favourites

Later we could display statistics such as number of different bus routes explored, towns visited and categories of attraction.

## 8. Finding interesting places

The system obtains the geographical path and stops of the selected bus route.

It then searches around the route for candidate locations using sources such as:

**Bus data**

- UK Bus Open Data Service
- TfL where appropriate

**Places**

- OpenStreetMap
- Wikidata/Wikipedia
- Historic England
- other appropriate public datasets

The application then researches promising candidates rather than relying upon a single source.

## 9. AI's job

AI should not invent the attractions.

Its job is to take real candidate locations and determine:

- Is this genuinely interesting?
- Why?
- Is there enough reliable information about it?
- Is it worth getting off the bus for?
- What category does it belong to?
- What should the visitor particularly notice?

It then writes the concise route description and the fuller attraction page.

## 10. Interest Score

Internally, each candidate gets an Interest Score, initially based on:

- **Intrinsic interest** — historical/cultural/natural significance.
- **Unusualness** — something unexpected gets additional weight.
- **Proximity** — an interesting object 100 m from the bus gets an advantage over something 1 km away.
- **Information quality** — there must be sufficient trustworthy information to describe it.
- **Duplication** — avoid several records representing essentially the same attraction.

The exact scoring formula should be developed experimentally rather than fixed now.

Importantly, the user doesn't need to see a numerical score. It's an internal ranking mechanism.

## 11. Personalisation

Once visit information exists, Bus Explorer can answer a much more interesting question:

> Which bus route should I explore next?

For example:

```
Route 75 — 14 unvisited discoveries within 1 km
```

That is a later feature, but the database should be designed from the beginning so that it becomes possible.

## 12. First development milestone

I would make Milestone 1 quite constrained.

**Bus Explorer 0.1**

It must:

1. Accept a London bus route number.
2. Identify the correct route and stops.
3. Obtain their coordinates.
4. Find candidate places within 1 km.
5. Produce a useful shortlist of interesting places.
6. Display them in route order.
7. Allow an attraction to be opened for considerably more information.
8. Allow Visited / Not Visited to be recorded permanently.
9. Allow an optional personal note and Favourite flag.
10. Remember that information when the application is reopened.

No accounts, live bus times, journey planning, nationwide coverage or sophisticated recommendations yet.

**Success criterion**

I would use the 54 as our test route.

The first milestone succeeds if we can open Bus Explorer, enter 54, and get a genuinely interesting collection of discoveries between Woolwich and Elmers End—including things such as Charlton House and the Bowie Bandstand—open any one for more information, and mark it as visited.

That is enough to establish whether Bus Explorer is actually a worthwhile product before we make it complicated.

The next step is probably to take this specification into Work/Code and build Milestone 0.1, rather than add more features now.
