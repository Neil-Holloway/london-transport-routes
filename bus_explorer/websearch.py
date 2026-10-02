"""Last-resort web search for places with no Wikipedia or Wikidata presence.

Some real, well-documented places (e.g. a local park covered by the council's
own website) simply have no Wikipedia article and no Wikidata item at all -
Wikipedia/Wikidata lookups in enrich.py correctly come back empty for these,
even though there is genuine information about the place elsewhere on the
web. This module searches for a real web page about the place and, if one is
found, returns its own text verbatim (never paraphrased or invented) plus a
link to it - same "no invented content" principle as the Wikipedia path.

Requires a Brave Search API key (https://api.search.brave.com) in the
BRAVE_API_KEY environment variable. If it isn't set, search() always returns
None, so the app works exactly as before without it configured.
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

API_KEY = os.environ.get("BRAVE_API_KEY")
USER_AGENT = "BusExplorer/0.1 (https://github.com/Neil-Holloway/london-transport-routes)"

_TAG_RE = re.compile(r"<[^>]+>")

# Domains that are never a description of the place itself, regardless of
# any name match - real-estate listings and generic shopping/travel
# aggregators. (Hit in practice: "Engine Block" -> a car-parts shop, "Royal
# Arsenal Thames Path Garden" -> an Agoda hotel listing, a Zoopla property
# listing.) Social media is deliberately NOT blocked here - a small
# organisation's own Facebook page (e.g. a church with no other web
# presence) can be a perfectly genuine source; see `confident` below for how
# that's distinguished from an unrelated same-named person's profile.
_BLOCKED_DOMAINS = (
    "agoda.com", "booking.com", "tripadvisor.", "airbnb.", "expedia.",
    "hotels.com", "flickr.com", "pinterest.", "amazon.", "ebay.",
    "etsy.com", "onlinecarparts.co.uk", "getyourguide.com", "viator.com",
    "zoopla.co.uk", "rightmove.co.uk",
)


def _strip_tags(s):
    """Brave's snippets highlight matched terms with <strong> tags."""
    return _TAG_RE.sub("", s)


def _normalise(s):
    return "".join(ch.lower() for ch in s if ch.isalnum())


def search(name, extra_terms="London"):
    """Returns {snippet, url, title, confident} for the best-matching
    result, or None.

    Two places can share a name (there's a Wellington Park in Somerset as
    well as London), and a search can surface a page that only mentions the
    name in passing rather than being about this place at all. Baseline
    filters that must always pass:
      - the result must not be from a known real-estate/aggregator domain
        (see _BLOCKED_DOMAINS) - never a description of the place itself
      - the place's name must appear in the result's title (not just buried
        in the snippet)
      - the title+snippet together must mention "london" somewhere, so a
        same-named place in a different city doesn't get accepted

    Beyond that, `confident` marks whether the result is strong enough to
    present as settled fact, or should be flagged as unverified:
      - confident=True when the place's name also appears in the result's
        own domain or URL path (e.g. "dantemayfair.com" for "Dante", or a
        Facebook page at "/woolwichevangelical" for "Woolwich Evangelical
        Church") - a vanity URL matching an organisation's name is strong
        evidence this is that organisation's own page, not a same-named
        person or an unrelated mention. Personal profiles essentially never
        have a vanity URL that happens to match someone else's business or
        landmark name.
      - confident=False otherwise - title-only matches (e.g. a blog post or
        directory listing that happens to mention the name) are plausible
        but not verifiable automatically, so the caller should present them
        as "possibly related" rather than fact.
    """
    if not API_KEY:
        return None

    target = _normalise(name)
    if not target:
        return None

    query = f'"{name}" {extra_terms}'.strip()
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
        {"q": query, "count": 5}
    )
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "X-Subscription-Token": API_KEY,
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except (OSError, ValueError, urllib.error.HTTPError):
        return None

    for result in data.get("web", {}).get("results", []):
        result_url = result.get("url", "")
        if any(domain in result_url for domain in _BLOCKED_DOMAINS):
            continue

        title = result.get("title", "")
        if target not in _normalise(title):
            continue

        snippet = _strip_tags(result.get("description", "")).strip()
        if not snippet:
            continue

        if "london" not in _normalise(title + snippet):
            continue

        parsed = urllib.parse.urlparse(result_url)
        domain_and_path = _normalise(parsed.netloc + parsed.path)
        confident = target in domain_and_path

        return {"snippet": snippet, "url": result_url, "title": title, "confident": confident}
    return None
