#!/usr/bin/env python3
"""
One-time script: enrich seeds/imax_theaters.csv with theater website URLs.

Uses three sources in priority order:
  1. OSM Overpass — cinema nodes often carry website / contact:website tags.
     Reuses the same city-based fuzzy-name search as enrich_csv_addresses.
  2. DuckDuckGo HTML search — scrapes DDG's no-JS HTML endpoint to find the
     theater's official website without requiring a browser or API key.
  3. DuckDuckGo local search (requires playwright + chromium) — intercepts the
     local.js JSONP response, which includes a 'url' field per place.
     Enable with --use-browser.  Falls back gracefully if playwright is not
     installed.

Usage:
    python scripts/enrich_csv_websites.py                         # live run
    python scripts/enrich_csv_websites.py --dry-run               # preview
    python scripts/enrich_csv_websites.py --dry-run --max 20
    python scripts/enrich_csv_websites.py --dry-run --sample 40   # random
    python scripts/enrich_csv_websites.py --dry-run --country "United States"
    python scripts/enrich_csv_websites.py --dry-run --chain "AMC"
    python scripts/enrich_csv_websites.py --dry-run --use-browser
    python scripts/enrich_csv_websites.py --csv seeds/_main_chains.csv
"""

import argparse
import csv
import difflib
import json
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

CSV_PATH = Path(__file__).parent.parent / "seeds" / "imax_theaters.csv"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL  = "https://overpass-api.de/api/interpreter"
DDG_HTML_URL  = "https://html.duckduckgo.com/html/"

NOMINATIM_DELAY = 1.1   # Nominatim ToS: max 1 req/sec
OVERPASS_DELAY  = 2.0   # polite delay between Overpass requests
DDG_DELAY       = 2.5   # polite delay between DDG HTML requests

USER_AGENT  = "IMAX_Alert_CSV_Enrichment/1.0 (github.com/dhodge250/IMAX_Alert)"
BROWSER_UA  = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# Domains that are aggregators, review sites, social media, or map services.
# URLs from these are rejected even if they appear first in search results.
_EXCLUDED_DOMAINS = frozenset({
    "fandango.com",
    "yelp.com",
    "google.com", "google.co",
    "facebook.com", "fb.com",
    "twitter.com", "x.com",
    "instagram.com",
    "tripadvisor.com",
    "yellowpages.com",
    "mapquest.com",
    "foursquare.com",
    "imdb.com",
    "rottentomatoes.com",
    "movietickets.com",
    "atom.com",
    "wikipedia.org",
    "linkedin.com",
    "youtube.com",
    "tiktok.com",
    "eventbrite.com",
    "ticketmaster.com",
    "showtimeapi.com",
    "movieglu.com",
    "internationalshowtimes.com",
    "maps.apple.com",
    "bing.com",
    "duckduckgo.com",
    "movio.co",          # cinema marketing platform, not a theater site
    "filmaffinity.com",
    "metacritic.com",
    "allstays.com",
    "imax.com",          # IMAX's own listing page, not the theater's website
    "flicks.co.uk",      # UK aggregator
    "atomtickets.com",   # ticketing aggregator
    "tributemovies.com", # movie times aggregator
    "showtimes.com",     # aggregator
    "cinemaclock.com",   # Canadian movie times aggregator
    "screendollars.com", # industry box-office analytics, not theater website
    "fandangonow.com",
    "vudu.com",
    "moviefone.com",
    "boxofficemojo.com",
    "the-numbers.com",
    "a-better-place.com",
    "realtor.com",
    "mapbox.com",
    "findglocal.com",    # business directory
    "londonnet.co.uk",   # local London directory
    "beekingintelligence.com",
    "cylex.co.uk", "cylex.fr", "cylex.de", "cylex.us",
    "hotfrog.co.uk",
    "localbusinesslistings.org",
    "yell.com",          # UK business directory
    "opentable.com",
    "timeout.com",
    "wikimapia.org",
    "whereis.com",
    "businesslist.com",
    "chamberofcommerce.com",
    "storeboard.com",
    "mapsofindia.com",
    "justdial.com",      # Indian business directory
    "sulekha.com",
    "grubhub.com",
    "doordash.com",
    "zomato.com",
    "movieinsider.com",
    "comingsoon.net",
    "screenrant.com",
    "cinemaholic.com",
    "cinematreasures.org",   # cinema history database, not a theater's own site
    # International aggregators / directories
    "allocine.fr",       # French movie database/aggregator
    "cinefil.com",       # French cinema listings aggregator
    "autour-de-moi.com", # French location-based cinema locator
    "cinema.autour-de-moi.com",
    "ouest-france.fr",   # French regional newspaper
    "20minutes.fr",      # French newspaper
    "lemonde.fr",
    "lefigaro.fr",
    "leparisien.fr",
    "kino.de",           # German movie database
    "filmstarts.de",     # German aggregator
    "filmstart.no",      # Norwegian aggregator
    "filmweb.no",        # Norwegian aggregator
    "sfanytime.com",     # Nordic streaming, not a theater site
    "cineman.ch",        # Swiss aggregator
    "cinechronicle.com",
    "movie.co.uk",       # UK aggregator
    "cinemasearch.com.au",
    "ticketek.com.au",   # Australian ticketing aggregator
    "session.com.au",    # Australian cinema session times aggregator
    "movietimes.com",
    "movieguide.org",
    "christiancinema.com",
    "flixtix.com",
    "goshow.in",         # Indian movie times
    "bookmyshow.com",    # Indian ticketing aggregator
    "paytm.com",         # Indian payments / ticketing
    "maoyan.com",        # Chinese movie ticketing
    "mtime.com",         # Chinese movie database
    "douban.com",        # Chinese review site
    "taopiaopiao.com",   # Chinese ticketing (Alibaba)
    "gewara.com",        # Chinese ticketing
    "dianping.com",      # Chinese business reviews
    "24cinema.ru",
    "kinopoisk.ru",      # Russian movie database
    "kino-teatr.ru",
    "afisha.ru",         # Russian event listing
    "filmweb.pl",        # Polish aggregator
    "cinema.com.my",     # Malaysian aggregator
    "sistic.com.sg",     # Singapore ticketing aggregator
    "webtickets.co.za",  # South African ticketing
    "iheartradio.com",
    "eventful.com",
    "washington.org",    # DC tourism portal, not a theater site
    "choosechicago.com", # Chicago tourism portal
    "nycgo.com",         # NYC tourism portal
    "discoverlosangeles.com",
    "visitphilly.com",
    # City guide / expat / tourism blog domains (not theater websites)
    "guangzhoutime.com",
    "echinacities.com",
    "beijingxpress.com",
    "citiesinsider.com",
    "chinaholiday.com",
    "city8.com",           # Chinese local listings
    "jadwalnonton.com",    # Indonesian movie times aggregator
    "thebeijinger.com",
    "smartshanghai.com",
    "timeoutbeijing.com",
    "timeoutshanghai.com",
    "chengdu-expat.com",
    "locator.hk",          # Hong Kong locator directory
    "yahoo.com",           # news/finance articles, not theater websites
    "trip.com",            # travel booking site
    "tourismsaskatchewan.com",
    "tourismnewbrunswick.ca",
    "tourismvancouver.com",
    "tourismtoronto.com",
    "ontariotravel.net",
    "visitolia.com",       # Saudi/Gulf tourism aggregator
    "gulf-insider.com",
    "expatica.com",        # expat lifestyle site
    "thelocal.fr", "thelocal.de", "thelocal.es", "thelocal.it",
    "angloinfo.com",       # expat directory
    "yandex.ru",           # Russian Yandex (Afisha, Maps, etc. — not official theater sites)
    "yandex.com",
    "wanderboat.ai",       # AI travel planning, not a theater site
    "dnb.com",             # Dun & Bradstreet business data
    "district.in",         # Indian ticketing app
    "yappe.in",            # Indian business directory
    "onepluspartnership.com",
    "cafeshanghai.com",
    "qingdaochinaguide.com",
})

# Tokens to strip before fuzzy name comparison
_NAME_NOISE = re.compile(
    r"\b(imax|imax[\s\-]?laser|laser|dine[\s\-]?in|& imax|"
    r"\d{1,3}|theatres?|theaters?|cinemas?|multiplex|"
    r"amc dine-in|amc dine in)\b",
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[^a-z0-9 ]")


# ---------------------------------------------------------------------------
# Name normalisation / scoring (shared with enrich_csv_addresses)
# ---------------------------------------------------------------------------

def _normalize(name: str) -> str:
    name = name.lower()
    name = _NAME_NOISE.sub(" ", name)
    name = _PUNCT.sub(" ", name)
    return " ".join(name.split())


def _name_score(a: str, b: str) -> float:
    n1, n2 = _normalize(a), _normalize(b)
    if not n1 or not n2:
        return 0.0
    if n1 in n2 or n2 in n1:
        shorter = min(len(n1.split()), len(n2.split()))
        longer  = max(len(n1.split()), len(n2.split()))
        return 0.5 + 0.4 * (shorter / longer)
    return difflib.SequenceMatcher(None, n1, n2).ratio()


# ---------------------------------------------------------------------------
# URL validation helpers
# ---------------------------------------------------------------------------

def _url_domain(url: str) -> str:
    """Return the registered domain (e.g. 'amctheatres.com') from a URL."""
    try:
        netloc = urllib.parse.urlparse(url).netloc.lower()
        netloc = netloc.lstrip("www.")
        return netloc
    except Exception:
        return ""


def _is_excluded(url: str) -> bool:
    """Return True if the URL is from an aggregator or excluded domain."""
    domain = _url_domain(url)
    if not domain:
        return True
    for ex in _EXCLUDED_DOMAINS:
        if domain == ex or domain.endswith("." + ex):
            return True
    return False


def _clean_url(url: str) -> str:
    """Strip tracking params and normalise the URL."""
    try:
        p = urllib.parse.urlparse(url)
        # Drop common tracking query params
        qs = urllib.parse.parse_qs(p.query, keep_blank_values=False)
        for k in list(qs):
            if k.lower().startswith(("utm_", "ref", "source", "campaign", "fbclid")):
                del qs[k]
        clean_qs = urllib.parse.urlencode({k: v[0] for k, v in qs.items()})
        return urllib.parse.urlunparse(p._replace(query=clean_qs, fragment=""))
    except Exception:
        return url


# ---------------------------------------------------------------------------
# Overpass helpers  (identical structure to enrich_csv_addresses)
# ---------------------------------------------------------------------------

def _nominatim_city_center(city: str, state: str, country: str) -> "tuple[float,float]|None":
    country_lower = country.strip().lower()
    is_us = country_lower in {"united states", "usa", "us"}
    if is_us:
        q = f"{city}, {state}, USA" if state else f"{city}, USA"
    else:
        q = f"{city}, {state}, {country}" if state else f"{city}, {country}"
    params = urllib.parse.urlencode({"q": q, "format": "jsonv2", "limit": 1, "addressdetails": 0})
    req = urllib.request.Request(
        f"{NOMINATIM_URL}?{params}", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            if data:
                return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None


def _overpass_cinemas(lat: float, lon: float, radius_m: int = 35_000) -> list:
    query = (
        f"[out:json][timeout:30];\n"
        f"(\n"
        f'  node["amenity"="cinema"](around:{radius_m},{lat},{lon});\n'
        f'  way["amenity"="cinema"](around:{radius_m},{lat},{lon});\n'
        f");\n"
        f"out body;\n"
    )
    req = urllib.request.Request(
        OVERPASS_URL,
        data=query.encode(),
        headers={"User-Agent": USER_AGENT, "Content-Type": "text/plain"},
    )
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            data = json.loads(resp.read().decode())
            return data.get("elements", [])
    except Exception:
        return []


def _overpass_website(
    csv_name: str,
    csv_city: str,
    elements: list,
    chain: str = "",
    threshold: float = 0.55,
) -> str:
    """
    Fuzzy-match the CSV theater name against OSM cinema elements and return
    the website tag of the best match.  Returns "" on no match.

    A city-match guard rejects elements whose addr:city is present but doesn't
    match csv_city — this prevents a nearby same-chain theater from being
    returned when the specific theater isn't in OSM.
    """
    best_score = 0.0
    best_tags: dict = {}

    csv_city_norm = _normalize(csv_city)

    for el in elements:
        tags = el.get("tags", {})
        osm_name = tags.get("name", "")
        if not osm_name:
            continue

        # City guard: if OSM has an addr:city that clearly doesn't match, skip.
        osm_city = tags.get("addr:city", "").strip()
        if osm_city:
            osm_city_norm = _normalize(osm_city)
            # Allow if either string is contained in the other (handles
            # "Ashton Under Lyne" vs "Ashton-under-Lyne" variations)
            if (csv_city_norm not in osm_city_norm
                    and osm_city_norm not in csv_city_norm
                    and difflib.SequenceMatcher(None, csv_city_norm, osm_city_norm).ratio() < 0.6):
                continue

        score = _name_score(csv_name, osm_name)
        if score > best_score:
            best_score = score
            best_tags = tags

    if best_score < threshold or not best_tags:
        return ""

    # OSM uses several tags for websites; try them in priority order.
    for key in ("website", "contact:website", "url", "contact:url"):
        val = best_tags.get(key, "").strip()
        if not val or not val.startswith("http"):
            continue
        # Chain-domain guard: if we know the chain name, the returned URL's
        # domain should contain at least one meaningful token from that name.
        # This catches cases like "Cineworld Birmingham" matching a Vue entry
        # that shares only the city name.
        if chain:
            chain_toks = [
                t for t in re.sub(r"[^a-z0-9]+", "-", chain.lower()).split("-")
                if len(t) >= 4
            ]
            if chain_toks:
                domain = _url_domain(val)
                if not any(t in domain for t in chain_toks):
                    return ""  # URL doesn't belong to this chain
        return _clean_url(val)
    return ""


# ---------------------------------------------------------------------------
# Chain → canonical domain mapping
# Used for site:-operator searches and chain-domain scoring.
# ---------------------------------------------------------------------------

_CHAIN_DOMAINS: dict[str, str] = {
    # North America
    "amc":                  "amctheatres.com",
    "regal":                "regmovies.com",
    "cinemark":             "cinemark.com",
    "cineplex":             "cineplex.com",
    "marcus":               "marcustheatres.com",
    "marcus theatres":      "marcustheatres.com",
    "malco":                "malco.com",
    "malco theatres":       "malco.com",
    "landmark":             "landmarkcinemas.com",
    "landmark cinemas":     "landmarkcinemas.com",
    # Galaxy Theatres (US) intentionally excluded from site: search — "Galaxy" also names
    # a separate Vietnam chain (galaxycine.vn), so a US-domain lookup causes cross-country
    # false matches.  Both are handled by chain-aware scoring in ddg_html_search.
    "epic theatres":        "epictheatres.com",
    "santikos":             "santikos.com",
    "ncg":                  "ncgcinemas.com",
    "cinemawest":           "cinemawest.com",
    "cinema west":          "cinemawest.com",
    "rc theatres":          "rctheatres.com",
    "megaplex":             "megaplextheatres.com",
    "megaplex theatres":    "megaplextheatres.com",
    "cinepolis":            "cinepolis.com",
    "cmx":                  "cmxcinemas.com",
    "cmx cinemas":          "cmxcinemas.com",
    "phoenix theatres":     "phoenixtheatres.com",
    "showcase cinemas":     "showcasecinemas.com",
    "emagine":              "emagine-entertainment.com",
    "emagine entertainment":"emagine-entertainment.com",
    "celebration cinema":   "celebrationcinema.com",
    "amstar":               "amstarcinemas.com",
    "amstar cinemas":       "amstarcinemas.com",
    "reading cinemas":      "readingcinemas.com",
    # UK / Ireland / Europe
    "cineworld":            "cineworld.co.uk",
    "odeon":                "odeon.co.uk",
    "odeon cinemas":        "odeon.co.uk",
    "vue":                  "myvue.com",
    "showcase":             "showcasecinemas.co.uk",
    "showcase cinemas uk":  "showcasecinemas.co.uk",
    "uci":                  "uci-kinowelt.de",
    "uci kinowelt":         "uci-kinowelt.de",
    "cinestar":             "cinestar.de",
    "kinepolis":            "kinepolis.com",
    # Gaumont and Pathé France merged; all French cinemas now use pathe.fr
    "gaumont":              "pathe.fr",
    "pathe":                "pathe.fr",
    "pathé":                "pathe.fr",
    "pathe nl":             "pathe.nl",   # Dutch Pathé is a separate operation
    "ugc":                  "ugc.fr",
    "mk2":                  "mk2.com",
    "cgr":                  "cgr.fr",
    "megarama":             "megarama.fr",
    "cinemaxxi":            "cinemaxxi.it",
    "the space cinema":     "thespacecinema.it",
    "cinecitta world":      "cinecittaworld.it",
    # Asia-Pacific
    "hoyts":                "hoyts.com.au",
    "village cinemas":      "villagecinemas.com.au",
    "event cinemas":        "eventcinemas.com.au",
    "reading cinemas au":   "readingcinemas.com.au",
    "pvr inox":             "pvrinox.com",
    "pvr":                  "pvrinox.com",
    "inox":                 "pvrinox.com",
    # CGV intentionally omitted: it uses country-specific domains (cgv.vn, cgv.co.kr, cgv.id, etc.)
    # so a single site: lookup causes cross-country false matches. Handled by chain-aware HTML scoring.
    "golden village":       "gv.com.sg",
    "gv":                   "gv.com.sg",
    "carnival cinemas":     "carnivalcinemas.com",
    # Asia-Pacific (additional)
    "golden screen cinemas":"gsc.com.my",
    "gsc":                  "gsc.com.my",
    "cathay cineplexes":    "cathaycineplexes.com",
    "shaw theatres":        "shawtheatres.com",
    "multikino":            "multikino.pl",
    "numetro":              "numetro.co.za",
    "nu metro":             "numetro.co.za",
    "ster-kinekor":         "sterkinekor.com",
    "ster kinekor":         "sterkinekor.com",
    # Wanda intentionally omitted: site: search only returns the mobile homepage
    # (m.wandacinemas.com/index/main.do), never theater-specific pages. Handled by HTML scoring.
    # Latin America
    "cinepolis mx":         "cinepolis.com",
    "cinemex":              "cinemex.com",
    "cinemark cl":          "cinemark.cl",
    "cinemark ar":          "cinemark.com.ar",
    "cineplex ca":          "cineplex.com",
}

# Minimum path depth to accept as a theater-level URL (not just homepage)
# e.g. /theatres/mi-ypsilanti/cinemark-ann-arbor → depth 3 → accepted
_MIN_PATH_DEPTH = 2


def _chain_domain(chain: str) -> str:
    """Return the canonical domain for a chain name, or ''."""
    return _CHAIN_DOMAINS.get(chain.strip().lower(), "")


# ---------------------------------------------------------------------------
# DuckDuckGo site: search  (targeted, for known chains)
# ---------------------------------------------------------------------------

def ddg_site_search(
    csv_name: str,
    city: str,
    state: str,
    chain: str,
    threshold: float = 0.35,
) -> str:
    """
    Search DDG with site:CHAIN_DOMAIN to find the theater-level page directly.
    Returns "" if the chain is unknown or no suitable result is found.
    """
    domain = _chain_domain(chain)
    if not domain:
        return ""

    # Build query: site:domain + stripped theater name + city
    stripped = re.sub(r"[^a-z0-9 ]+", " ", csv_name.lower())
    stripped = re.sub(r"\s+", " ", stripped).strip()
    loc = f"{city} {state}".strip() if state else city
    query = f"site:{domain} {stripped} {loc}"

    params = urllib.parse.urlencode({"q": query, "kl": "us-en"})
    req = urllib.request.Request(
        f"{DDG_HTML_URL}?{params}",
        headers={
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return ""

    seen: set[str] = set()
    candidates: list[tuple[float, str]] = []

    for m in _DDG_REDIRECT_RE.finditer(html):
        url = urllib.parse.unquote(m.group(1))
        if not url.startswith("http"):
            continue
        url = _clean_url(url)
        if url in seen:
            continue
        seen.add(url)

        # Must be on the expected chain domain
        if _url_domain(url) != domain and not _url_domain(url).endswith("." + domain):
            continue

        parsed_path = urllib.parse.urlparse(url).path
        depth = len([p for p in parsed_path.strip("/").split("/") if p])
        if depth < _MIN_PATH_DEPTH:
            continue  # skip homepages and shallow listing pages

        # Score by name match in path
        path_lower = parsed_path.lower()
        name_slug  = re.sub(r"[^a-z0-9]+", "-", csv_name.lower()).strip("-")
        city_slug  = re.sub(r"[^a-z0-9]+", "-", city.lower()).strip("-")

        score = 0.2  # baseline: on the right domain
        if name_slug[:10] in path_lower:
            score += 0.5
        if city_slug[:8] in path_lower:
            score += 0.2
        candidates.append((score, url))

    if not candidates:
        return ""

    candidates.sort(key=lambda x: x[0], reverse=True)
    best_score, best_url = candidates[0]
    return best_url if best_score >= threshold else ""


# ---------------------------------------------------------------------------
# DuckDuckGo HTML search  (no browser required)
# ---------------------------------------------------------------------------

# DDG wraps every result URL in a redirect like:
#   href="//duckduckgo.com/l/?uddg=PERCENT_ENCODED_URL&amp;rut=HASH"
# The HTML entity &amp; means we stop at & not &amp; when splitting.
_DDG_REDIRECT_RE = re.compile(
    r'//duckduckgo\.com/l/\?uddg=([^&"<\s]+)',
    re.IGNORECASE,
)



def ddg_html_search(
    csv_name: str,
    city: str,
    state: str,
    country: str,
    chain: str = "",
    threshold: float = 0.25,
) -> str:
    """
    Search DDG HTML for the theater and return the most likely official website
    URL, or "" if nothing suitable is found.

    Scoring strongly prefers URLs whose domain contains the chain name
    (e.g. cineworld.co.uk for chain="Cineworld"), so official sites rank above
    local directories even when the directory appears first in search results.
    """
    country_lower = country.strip().lower()
    is_us = country_lower in {"united states", "usa", "us"}

    # Build a targeted query.
    # Use "cinema" internationally — "movie theater" is US English and causes DDG
    # to deprioritise non-English official websites (e.g. gaumont.fr for France).
    loc = f"{city} {state}".strip() if state else f"{city} {country}".strip()
    query_suffix = "movie theater" if is_us else "cinema"
    query = f"{csv_name} {loc} {query_suffix}"

    params = urllib.parse.urlencode({
        "q": query,
        "kl": "us-en" if is_us else "wt-wt",
        "ia": "web",
    })
    req = urllib.request.Request(
        f"{DDG_HTML_URL}?{params}",
        headers={
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"  DDG HTML error: {exc}", file=sys.stderr)
        return ""

    # Extract URLs from DDG's redirect wrappers (the only form DDG uses)
    seen: set[str] = set()
    candidates: list[tuple[float, str]] = []

    for m in _DDG_REDIRECT_RE.finditer(html):
        url = urllib.parse.unquote(m.group(1))
        if not url.startswith("http"):
            continue
        url = _clean_url(url)
        if url in seen:
            continue
        seen.add(url)

        if _is_excluded(url):
            continue

        # Score this URL against the theater name + chain
        domain = _url_domain(url)
        path   = urllib.parse.urlparse(url).path.lower()

        # Slug the name and chain for path/domain comparison
        name_slug  = re.sub(r"[^a-z0-9]+", "-", csv_name.lower()).strip("-")
        city_slug  = re.sub(r"[^a-z0-9]+", "-", city.lower()).strip("-")
        chain_slug = re.sub(r"[^a-z0-9]+", "-", chain.lower()).strip("-") if chain else ""

        score = 0.0
        # ── Highest priority: domain or path contains the chain name ─────
        # e.g. cineworld.co.uk contains "cineworld"; amctheatres.com contains "amc"
        # Also catches regmovies.com/theatres/regal-xxx where "regal" is in the path.
        if chain_slug:
            chain_toks = [t for t in chain_slug.split("-") if len(t) >= 3]
            full_url_lower = domain + path
            chain_domain_match = False
            if chain_toks and all(t in domain for t in chain_toks[:2]):
                # Country-TLD guard: don't credit an AU/UK/etc. chain domain when the
                # theater is in a different country.  e.g. hoyts.com.au should not
                # match a Chinese "Village Hoyts" theater.
                _ccTLDs = {".com.au": "australia", ".co.uk": "united kingdom",
                           ".co.nz": "new zealand", ".co.in": "india",
                           ".com.cn": "china", ".com.mx": "mexico",
                           ".com.br": "brazil", ".com.sg": "singapore",
                           ".com.my": "malaysia"}
                wrong_country = any(
                    domain.endswith(tld) and country.strip().lower() != expected
                    for tld, expected in _ccTLDs.items()
                )
                if not wrong_country:
                    score += 1.0   # all tokens in domain — best case
                    chain_domain_match = True
            if not chain_domain_match:
                if chain_toks and any(t in domain for t in chain_toks):
                    score += 0.6   # partial domain match
                elif chain_toks and any(t in full_url_lower for t in chain_toks):
                    score += 0.4   # chain token found in path
        # ── Secondary: domain contains a meaningful part of the theater name
        for tok in _normalize(csv_name).split():
            if len(tok) >= 5 and tok in domain:
                score += 0.3
                break
        # ── Path contains the theater name slug
        if name_slug[:12] in path:
            score += 0.3
        # ── Path contains the city
        if city_slug[:8] in path:
            score += 0.1
        # ── Baseline: any non-excluded URL is a candidate
        score += 0.05

        candidates.append((score, url))

    if not candidates:
        return ""

    candidates.sort(key=lambda x: x[0], reverse=True)
    best_score, best_url = candidates[0]

    if best_score < threshold:
        return ""

    return best_url


# ---------------------------------------------------------------------------
# DuckDuckGo local search via real Chromium (optional, --use-browser)
# ---------------------------------------------------------------------------

_DDG_JSONP_RE = re.compile(r'DDG\.duckbar\.add_local\((.+)\)\s*;?\s*$', re.DOTALL)


def _ddg_browser_places(query: str, locale: str = "wt-wt") -> list:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    captured: list[str] = []

    def _on_response(response) -> None:
        if "local.js" in response.url and "duckduckgo.com" in response.url:
            try:
                captured.append(response.text())
            except Exception:
                pass

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(locale="en-US", timezone_id="America/New_York")
            page = context.new_page()
            page.on("response", _on_response)
            page.goto(
                "https://duckduckgo.com/?q=" + query.replace(" ", "+") + f"&kl={locale}",
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(5000)
            browser.close()
    except Exception:
        return []

    places = []
    for body in captured:
        m = _DDG_JSONP_RE.search(body)
        if not m:
            continue
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        places.extend(data.get("results", []))
    return places


def ddg_browser_website(
    csv_name: str, city: str, country: str, threshold: float = 0.45
) -> str:
    """
    Use a real Chromium browser to search DDG local and return the best-matching
    place's URL.  Returns "" on no match or if playwright is not installed.
    """
    country_lower = country.strip().lower()
    is_us = country_lower in {"united states", "usa", "us"}
    locale = "us-en" if is_us else "wt-wt"

    query = f"{csv_name} {city} {country}"
    places = _ddg_browser_places(query, locale=locale)
    if not places:
        return ""

    best_score, best_url, best_place = 0.0, "", None
    for pl in places:
        score = _name_score(csv_name, pl.get("name", ""))
        url   = pl.get("url", "").strip()
        if score > best_score and url and url.startswith("http") and not _is_excluded(url):
            best_score = score
            best_url   = url
            best_place = pl

    if best_score < threshold or not best_url or best_place is None:
        return ""

    # Reject cross-country false matches: if Apple Maps says US but theater is not US, skip.
    place_country = (best_place.get("country_code") or "").upper()
    if place_country and not is_us and place_country == "US":
        return ""

    return best_url


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run",    action="store_true",
                        help="Print what would change without writing the CSV")
    parser.add_argument("--max",        type=int, default=None, metavar="N",
                        help="Process at most N theaters")
    parser.add_argument("--sample",     type=int, default=None, metavar="N",
                        help="Randomly sample N theaters from the missing-website set")
    parser.add_argument("--country",    default=None, metavar="NAME",
                        help='Limit to theaters in this country (e.g. "United States")')
    parser.add_argument("--chain",      default=None, metavar="NAME",
                        help='Limit to theaters belonging to this chain (e.g. "AMC")')
    parser.add_argument("--use-browser",action="store_true",
                        help="Enable DDG local search via Chromium (requires playwright)")
    parser.add_argument("--csv",        default=None, metavar="PATH",
                        help="Path to CSV file (default: seeds/imax_theaters.csv)")
    args = parser.parse_args()

    csv_path = Path(args.csv) if args.csv else CSV_PATH

    # --- Read CSV ---
    with open(csv_path, encoding="utf-8", newline="") as f:
        reader     = csv.DictReader(f)
        fieldnames = list(reader.fieldnames)
        rows       = list(reader)

    # Build candidate list: rows missing a Website value
    missing = [r for r in rows if not r.get("Website", "").strip()]
    if args.country:
        missing = [r for r in missing
                   if r.get("Country", "").strip().lower() == args.country.strip().lower()]
    if args.chain:
        missing = [r for r in missing
                   if r.get("Chain", "").strip().lower() == args.chain.strip().lower()]
    if args.sample:
        missing = random.sample(missing, min(args.sample, len(missing)))
    elif args.max:
        missing = missing[:args.max]

    total = len(missing)
    print(f"CSV             : {csv_path}")
    print(f"Rows to process : {total}")
    print(f"Mode            : {'DRY RUN' if args.dry_run else 'LIVE'}\n")

    updated_overpass = updated_ddg_site = updated_ddg_html = updated_ddg_browser = skipped = 0
    nom_req_count = 0

    # Cache Overpass results per city — identical to enrich_csv_addresses
    _overpass_cache: dict[str, list] = {}

    for i, row in enumerate(missing):
        name    = row.get("Location Name", "").strip()
        city    = row.get("City", "").strip()
        state   = row.get("State/Province", "").strip()
        country = row.get("Country", "").strip()
        chain   = row.get("Chain", "").strip()

        prefix = f"[{i + 1}/{total}] {name}, {city} ({country})"
        print(f"{prefix} ... ", end="", flush=True)

        website = ""
        source  = None

        # ------------------------------------------------------------------
        # Source 1: Overpass city search + fuzzy name match → website tag
        # ------------------------------------------------------------------
        city_key = f"{city}|{state}|{country}".lower()
        if city_key not in _overpass_cache:
            if nom_req_count > 0:
                time.sleep(NOMINATIM_DELAY)
            center = _nominatim_city_center(city, state, country)
            nom_req_count += 1
            if center:
                time.sleep(OVERPASS_DELAY)
                _overpass_cache[city_key] = _overpass_cinemas(*center)
            else:
                _overpass_cache[city_key] = []

        elements = _overpass_cache[city_key]
        if elements:
            w = _overpass_website(name, city, elements, chain=chain)
            if w:
                website = w
                source  = "overpass"

        # ------------------------------------------------------------------
        # Source 2: DuckDuckGo site: search (for known chains)
        # ------------------------------------------------------------------
        if not website:
            time.sleep(DDG_DELAY)
            w = ddg_site_search(name, city, state, chain)
            if w:
                website = w
                source  = "ddg-site"

        # ------------------------------------------------------------------
        # Source 3: DuckDuckGo HTML search (general fallback)
        # ------------------------------------------------------------------
        if not website:
            time.sleep(DDG_DELAY)
            w = ddg_html_search(name, city, state, country, chain=chain)
            if w:
                website = w
                source  = "ddg-html"

        # ------------------------------------------------------------------
        # Source 3: DuckDuckGo local search via real browser (optional)
        # ------------------------------------------------------------------
        if not website and args.use_browser:
            w = ddg_browser_website(name, city, country)
            if w:
                website = w
                source  = "ddg-browser"

        # ------------------------------------------------------------------
        # Record result
        # ------------------------------------------------------------------
        if website:
            print(f"OK [{source}]  {website}")
            if not args.dry_run:
                row["Website"] = website
            if source == "overpass":
                updated_overpass += 1
            elif source == "ddg-site":
                updated_ddg_site += 1
            elif source == "ddg-html":
                updated_ddg_html += 1
            else:
                updated_ddg_browser += 1
        else:
            print("no match")
            skipped += 1

    total_updated = updated_overpass + updated_ddg_site + updated_ddg_html + updated_ddg_browser
    print(f"\n{'DRY RUN ' if args.dry_run else ''}Results:")
    print(f"  Found (overpass)     : {updated_overpass}")
    print(f"  Found (ddg-site)     : {updated_ddg_site}")
    print(f"  Found (ddg-html)     : {updated_ddg_html}")
    print(f"  Found (ddg-browser)  : {updated_ddg_browser}")
    print(f"  Not found            : {skipped}")
    print(f"  Total updated        : {total_updated} / {total}")

    if args.dry_run:
        print("\nDry run complete — CSV not modified.")
        return

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nCSV written: {csv_path}")


if __name__ == "__main__":
    main()
