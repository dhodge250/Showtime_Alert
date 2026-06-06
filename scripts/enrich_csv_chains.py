#!/usr/bin/env python3
"""
One-time script: enrich/correct the Chain column in seeds/imax_theaters.csv.

Uses three sources in priority order:
  1. Explicit chain renames — normalizes suffixes ("Wanda Cinemas" -> "Wanda"),
     known mergers (Gaumont -> Pathé), acquisitions (Apple Cinemas -> Showcase),
     and inconsistencies (Malco Theatres -> Malco).  Applied to all rows
     regardless of website.
  2. Website URL domain — fills empty chains and corrects non-empty chains when
     the theater name does not contain a token from the current chain (which
     would indicate the name itself confirms the current chain is correct,
     helping avoid bad-URL propagation).
  3. Theater name prefix — fills chains that are still empty after sources 1-2.

Usage:
    python scripts/enrich_csv_chains.py              # dry run (default)
    python scripts/enrich_csv_chains.py --live       # apply changes
    python scripts/enrich_csv_chains.py --csv PATH   # alternate CSV
"""

import argparse
import csv
import re
import sys
import urllib.parse
from pathlib import Path

CSV_PATH = Path(__file__).parent.parent / "seeds" / "imax_theaters.csv"

# ---------------------------------------------------------------------------
# Source 1 — explicit renames
# Applied unconditionally.  Covers: suffix normalization, mergers/rebranding,
# acquisitions, and name inconsistencies in the existing CSV data.
# Chains where "Cinema/Theatre" is integral to the brand (Cinema City,
# Cinema Park, Cinema 5, Cinema Sunshine) are intentionally excluded.
# ---------------------------------------------------------------------------
_CHAIN_RENAMES: dict[str, str] = {
    # ── Suffix normalization ───────────────────────────────────────────────
    "Wanda Cinemas":            "Wanda",
    "Jinyi Cinema":             "Jinyi",
    "Bona Film Group":          "Bona",
    "MixC Cinema":              "MixC",
    "Cinema XXI":               "XXI",
    "Odeon Cinemas":            "Odeon",
    "VOX Cinemas":              "VOX",
    "SFC Cinema":               "SFC",
    "Toho Cinemas":             "Toho",
    "AEON Cinema":              "AEON",
    "United Cinemas":           "United",
    "Hengdian Cinema":          "Hengdian",
    "Lumière Cinema":           "Lumière",
    "Major Cineplex":           "Major",
    "Vie Show Cinemas":         "Vie Show",
    "Emperor Cinemas":          "Emperor",
    "Landmark Cinemas":         "Landmark",
    "Galaxy Theatres":          "Galaxy",
    "Shaw Theatres":            "Shaw",
    "Event Cinemas":            "Event",
    "TGV Cinemas":              "TGV",
    "CMX Cinemas":              "CMX",
    "CMX":                      "CMX",   # already simplified, keep consistent
    "Epic Theatres":            "Epic",
    "Village Cinemas":          "Village",
    "OSGH Cinemas":             "OSGH",
    "NOS Cinemas":              "NOS",
    "Nova Cinemas":             "Nova",
    "Filmhouse Cinemas":        "Filmhouse",
    "Vue Cinemas":              "Vue",
    "HBC Cinema":               "HBC",
    "Womei Cinema":             "Womei",
    "UCI Kinoplex":             "UCI",
    "Hengye Cinema":            "Hengye",
    "Saga Cinema":              "Saga",
    "Space Station Cinema":     "Space Station",
    "Sinake Cinema":            "Sinake",
    "Tonight Cinema":           "Tonight",
    "Tai Lai Cinema":           "Tai Lai",
    "Heping Cinema":            "Heping",
    "Cando Cinema":             "Cando",
    "Zose Cinema":              "Zose",
    "INSUN Cinema":             "INSUN",
    "Flying Cinema":            "Flying",
    "Aurora Cinema":            "Aurora",
    "Xingguangjiaying Cinema":  "Xingguangjiaying",
    "Fanyang Cinema":           "Fanyang",
    "InCity Cinema":            "InCity",
    "Cross Cinema":             "Cross",
    "AmStar Cinemas":           "AmStar",
    "Phoenix Theatres":         "Phoenix",
    "RC Theatres":              "RC",
    "Cathay Cineplexes":        "Cathay",
    "Celebration! Cinema":      "Celebration!",
    "Celebration Cinema":       "Celebration!",
    "Emagine Entertainment":    "Emagine",
    "Emagine":                  "Emagine",
    "EVO Entertainment":        "EVO",
    "Reading Cinemas":          "Reading",
    "MetroLux Theatres":        "MetroLux",
    "Empire Cinemas":           "Empire",
    "Palace Cinema":            "Palace",
    "Kronverk Cinema":          "Kronverk",
    "Paribu Cineverse":         "Paribu",
    "GSC Cinemas":              "GSC",
    "Showcase Cinemas":         "Showcase",
    "Marcus Theatres":          "Marcus",
    "Malco Theatres":           "Malco",
    "Dendy Cinemas":            "Dendy",
    "Filmpalast":               "Filmpalast",   # keep (German brand)
    "blue Cinema":              "blue",
    # ── Mergers / rebranding ─────────────────────────────────────────────
    "Gaumont":                  "Pathé",    # Gaumont/Pathé merged
    "La Géode":                 "Pathé",    # operated by Pathé
    "Cine Loire":               "Pathé",    # operated by Pathé
    "Golden Screen Cinemas":    "GSC",
    "Esplanade Cineplex":       "Major",    # rebranded to Major Cineplex
    # ── Acquisitions ─────────────────────────────────────────────────────
    "Apple Cinemas":            "Showcase", # acquired by Showcase Cinemas
    "IMAX Entertainment":       "Event",    # IMAX Sydney is operated by Event Cinemas
}

# ---------------------------------------------------------------------------
# Source 2 — domain → canonical chain name (simplified, no suffixes)
# None = known non-chain domain — skip.
# ---------------------------------------------------------------------------
_DOMAIN_TO_CHAIN: dict[str, str | None] = {
    # North America
    "amctheatres.com":              "AMC",
    "regmovies.com":                "Regal",
    "cinemark.com":                 "Cinemark",
    "cinemark.com.br":              "Cinemark",
    "cinemark.cl":                  "Cinemark",
    "cinemark.com.ar":              "Cinemark",
    "cineplex.com":                 "Cineplex",
    "marcustheatres.com":           "Marcus",
    "malco.com":                    "Malco",
    "landmarkcinemas.com":          "Landmark",
    "galaxytheatres.com":           "Galaxy",
    "epictheatres.com":             "Epic",
    "santikos.com":                 "Santikos",
    "ncgcinemas.com":               "NCG",
    "cinemawest.com":               "CinemaWest",
    "rctheatres.com":               "RC",
    "megaplextheatres.com":         "Megaplex",
    "megaplex.com":                 "Megaplex",
    "amstarcinemas.com":            "AmStar",
    "amstarcinemas.net":            "AmStar",
    "celebrationcinema.com":        "Celebration!",
    "emagine-entertainment.com":    "Emagine",
    "cmxcinemas.com":               "CMX",
    "phoenixtheatres.com":          "Phoenix",
    "showcasecinemas.com":          "Showcase",
    "showcasecinemas.co.uk":        "Showcase",
    "applecinemas.com":             "Showcase",   # acquired by Showcase
    "readingcinemas.com":           "Reading",
    "cinepolis.com":                "Cinepolis",
    "cinepolis.com.co":             "Cinepolis",
    "cinepolis.com.br":             "Cinepolis",
    "cinepolis.com.mx":             "Cinepolis",
    "cinepolis.com.gt":             "Cinepolis",
    "cinemex.com":                  "Cinemex",
    "supercines.com":               "Supercines",
    "imax.com":                     None,
    "imax.cn":                      None,
    # UK / Ireland / Europe
    "cineworld.co.uk":              "Cineworld",
    "cineworld.ie":                 "Cineworld",
    "odeon.co.uk":                  "Odeon",
    "myvue.com":                    "Vue",
    "cinestar.de":                  "CineStar",
    "uci-kinowelt.de":              "UCI",
    "kinepolis.com":                "Kinepolis",
    "kinepolis.nl":                 "Kinepolis",
    "kinepolis.be":                 "Kinepolis",
    "kinepolis.fr":                 "Kinepolis",
    "kinepolis.es":                 "Kinepolis",
    "pathe.fr":                     "Pathé",
    "pathe.be":                     "Pathé",
    "pathe.nl":                     "Pathé",
    "pathe.ch":                     "Pathé",
    "ugc.fr":                       "UGC",
    "mk2.com":                      "MK2",
    "cgr.fr":                       "CGR",
    "megarama.fr":                  "Megarama",
    "cineplexx.at":                 "CineplexX",
    "megaplex.at":                  "Hollywood Megaplex",
    "cinema-city.pl":               "Cinema City",
    "multikino.pl":                 "Multikino",
    "filmstaden.se":                "Filmstaden",
    "cinesa.es":                    "Cinesa",
    "cinemas.nos.pt":               "NOS",
    "bluecinema.ch":                "blue",
    "cinemaxxi.it":                 "CinemaXXI",
    "thespacecinema.it":            "The Space Cinema",
    # Asia-Pacific
    "hoyts.com.au":                 "Hoyts",
    "eventcinemas.com.au":          "Event",
    "villagecinemas.com.au":        "Village",
    "readingcinemas.com.au":        "Reading",
    "dendy.com.au":                 "Dendy",
    "pvrinox.com":                  "PVR INOX",
    "cgv.com.cn":                   "CGV",
    "cgv.co.kr":                    "CGV",
    "cgv.id":                       "CGV",
    "cgv.vn":                       "CGV",
    "cgv.com":                      "CGV",
    "wandacinemas.com":             "Wanda",
    "wandafilm.com":                "Wanda",
    "jycinema.com":                 "Jinyi",
    "omnijoi.com":                  "Omnijoi",
    "omnijoi.cn":                   "Omnijoi",
    "bonafilm.cn":                  "Bona",
    "gv.com.sg":                    "Golden Village",
    "gsc.com.my":                   "GSC",
    "tgvcinemas.com":               "TGV",
    "cathaycineplexes.com":         "Cathay",
    "shaw.sg":                      "Shaw",
    "shawtheatres.com":             "Shaw",
    "cinema21.co.id":               "XXI",
    "majorcineplex.com":            "Major",
    "tohotheater.jp":               "Toho",
    "unitedcinemas.jp":             "United",
    "aeoncinema.com":               "AEON",
    "109cinemas.net":               "109 Cinemas",
    "cinemasunshine.co.jp":         "Cinema Sunshine",
    "galaxycine.vn":                "Galaxy Cinema",   # Vietnam Galaxy ≠ US Galaxy
    "vieshow.com.tw":               "Vie Show",
    "hengdian.com":                 "Hengdian",
    "sh-sfc.com":                   "SFC",
    "emperorcinemas.com":           "Emperor",
    "hookyentertainment.com":       "Hooky",
    "paribucineverse.com":          "Paribu",
    # Middle East / Africa
    "voxcinemas.com":               "VOX",
    "sterkinekor.com":              "Ster-Kinekor",
    "numetro.co.za":                "Nu Metro",
    # Russia / CIS
    "kinomax.ru":                   "Kinomax",
    "kinopark.kz":                  "Kinopark",
    "formula-kino.ru":              "Formula Kino",
    "formula-cinema.ru":            "Formula Kino",
    "cinema5.ru":                   "Cinema 5",
    "kronverk.ru":                  "Kronverk",
    "planetakino.ua":               "Planeta Kino",
    # Other
    "themoviesaruba.com":           "The Movies",
    "cineart.com.br":               "Cineart",
    "cinesystem.com.br":            "Cinesystem",
    "ucinemas.com.br":              "UCI",
}

# ---------------------------------------------------------------------------
# Source 3 — theater name prefix → chain (fills empty chains only)
# ---------------------------------------------------------------------------
_NAME_PREFIXES: list[tuple[str, str]] = sorted([
    ("AMC Classic ",            "AMC"),
    ("AMC Dine-In ",            "AMC"),
    ("AMC ",                    "AMC"),
    ("Regal Edwards ",          "Regal"),
    ("Regal UA ",               "Regal"),
    ("Regal ",                  "Regal"),
    ("Cinemark Tinseltown ",    "Cinemark"),
    ("Cinemark ",               "Cinemark"),
    ("Cineworld ",              "Cineworld"),
    ("ODEON ",                  "Odeon"),
    ("Odeon ",                  "Odeon"),
    ("Vue ",                    "Vue"),
    ("Cineplex ",               "Cineplex"),
    ("Hoyts ",                  "Hoyts"),
    ("Kinepolis ",              "Kinepolis"),
    ("Pathé ",                  "Pathé"),
    ("Pathe ",                  "Pathé"),
    ("UGC ",                    "UGC"),
    ("MK2 ",                    "MK2"),
    ("Gaumont ",                "Pathé"),
    ("CGR ",                    "CGR"),
    ("Megarama ",               "Megarama"),
    ("CineStar ",               "CineStar"),
    ("Cinestar ",               "CineStar"),
    ("UCI ",                    "UCI"),
    ("Megaplex ",               "Megaplex"),
    ("Marcus ",                 "Marcus"),
    ("Galaxy Theatres ",        "Galaxy"),
    ("Galaxy ",                 "Galaxy"),
    ("Epic Theatres ",          "Epic"),
    ("Epic ",                   "Epic"),
    ("Santikos ",               "Santikos"),
    ("VOX ",                    "VOX"),
    ("Cinesystem ",             "Cinesystem"),
    ("Cineart ",                "Cineart"),
    ("Cinepolis ",              "Cinepolis"),
    ("Cinemex ",                "Cinemex"),
    ("CGV ",                    "CGV"),
    ("Wanda ",                  "Wanda"),
    ("PVR INOX ",               "PVR INOX"),
    ("PVR ",                    "PVR INOX"),
    ("INOX ",                   "PVR INOX"),
    ("Formula Kino ",           "Formula Kino"),
    ("Formula Cinema ",         "Formula Kino"),
    ("Kronverk ",               "Kronverk"),
    ("Planeta Kino ",           "Planeta Kino"),
    ("Kinomax ",                "Kinomax"),
    ("Kinopark ",               "Kinopark"),
    ("Showcase ",               "Showcase"),
    ("Reading Cinemas ",        "Reading"),
    ("Celebration! ",           "Celebration!"),
    ("Emagine ",                "Emagine"),
    ("AmStar ",                 "AmStar"),
    ("CMX ",                    "CMX"),
    ("Phoenix Theatres ",       "Phoenix"),
    ("Phoenix ",                "Phoenix"),
    ("Event Cinemas ",          "Event"),
    ("Village Cinemas ",        "Village"),
    ("Dendy ",                  "Dendy"),
    ("Major Cineplex ",         "Major"),
    ("Cinema XXI ",             "XXI"),
    ("Toho Cinemas ",           "Toho"),
    ("United Cinemas ",         "United"),
    ("AEON Cinema ",            "AEON"),
    ("Omnijoi ",                "Omnijoi"),
    ("Jinyi ",                  "Jinyi"),
    ("CineplexX ",              "CineplexX"),
    ("Cineplexx ",              "CineplexX"),
    ("Golden Village ",         "Golden Village"),
    ("GSC ",                    "GSC"),
    ("Cathay ",                 "Cathay"),
    ("Ster-Kinekor ",           "Ster-Kinekor"),
    ("Nu Metro ",               "Nu Metro"),
    ("Multikino ",              "Multikino"),
    ("Filmstaden ",             "Filmstaden"),
    ("Kinoplex ",               "UCI"),
    ("Supercines ",             "Supercines"),
    ("Landmark ",               "Landmark"),
    ("Malco ",                  "Malco"),
    ("RC Theatres ",            "RC"),
    ("CinemaWest ",             "CinemaWest"),
    ("Paribu ",                 "Paribu"),
    ("Reel Cinemas ",           "Reel Cinemas"),
], key=lambda x: -len(x[0]))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _chain_from_url(url: str) -> str | None:
    if not url:
        return None
    try:
        netloc = urllib.parse.urlparse(url).netloc.lower()
        netloc = re.sub(r"^(www\.|m\.)", "", netloc)
    except Exception:
        return None
    parts = netloc.split(".")
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:])
        if candidate in _DOMAIN_TO_CHAIN:
            return _DOMAIN_TO_CHAIN[candidate]
    return None


def _chain_from_name(name: str) -> str | None:
    for prefix, chain in _NAME_PREFIXES:
        if name.lower().startswith(prefix.lower()):
            return chain
    return None


def _name_confirms_chain(theater_name: str, chain: str) -> bool:
    """
    True if the theater name contains the primary token of the current chain,
    indicating the name itself is evidence the chain value is correct.
    Guards against bad-URL propagation (e.g. a wandacinemas.com URL on a
    Jinyi-branded theater should not overwrite "Jinyi Cinema" to "Wanda").
    """
    _STOP = {"the", "and", "cinemas", "cinema", "theatres",
             "theaters", "group", "film", "entertainment"}
    chain_words = re.sub(r"[^a-z0-9 ]", " ", chain.lower()).split()
    primary_tokens = [w for w in chain_words if len(w) >= 4 and w not in _STOP]
    if not primary_tokens:
        return False
    name_lower = theater_name.lower()
    return any(tok in name_lower for tok in primary_tokens[:2])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="Apply changes (default is dry run)")
    parser.add_argument("--csv", default=None, metavar="PATH")
    args = parser.parse_args()

    csv_path = Path(args.csv) if args.csv else CSV_PATH
    dry_run  = not args.live

    with open(csv_path, encoding="utf-8", newline="") as f:
        reader     = csv.DictReader(f)
        fieldnames = list(reader.fieldnames)
        rows       = list(reader)

    renamed     = 0
    filled_url  = 0
    fixed_url   = 0
    filled_name = 0
    unchanged   = 0

    for row in rows:
        name    = row.get("Location Name", "").strip()
        current = row.get("Chain", "").strip()
        website = row.get("Website", "").strip()

        new_chain = current
        tag       = None

        # ── Source 1: explicit rename ────────────────────────────────────
        if current in _CHAIN_RENAMES:
            new_chain = _CHAIN_RENAMES[current]
            if new_chain != current:
                tag = f"rename  {current!r} -> {new_chain!r}"
                renamed += 1
            else:
                unchanged += 1

        # ── Source 2: URL domain ─────────────────────────────────────────
        elif website:
            url_chain = _chain_from_url(website)
            if url_chain:
                if not current:
                    new_chain = url_chain
                    tag = f"filled-url  {url_chain!r}"
                    filled_url += 1
                elif current.lower() != url_chain.lower():
                    if not _name_confirms_chain(name, current):
                        new_chain = url_chain
                        tag = f"fixed-url  {current!r} -> {url_chain!r}"
                        fixed_url += 1
                    else:
                        unchanged += 1
                else:
                    unchanged += 1
            else:
                unchanged += 1

        # ── Source 3: name prefix (empty chains only) ────────────────────
        elif not current:
            name_chain = _chain_from_name(name)
            if name_chain:
                new_chain = name_chain
                tag = f"filled-name  {name_chain!r}"
                filled_name += 1
            else:
                unchanged += 1
        else:
            unchanged += 1

        if tag and dry_run:
            print(f"[{tag}]  {name[:55]}")

        if not dry_run:
            row["Chain"] = new_chain

    print()
    print(f"{'DRY RUN ' if dry_run else ''}Results:")
    print(f"  Renamed (explicit)  : {renamed}")
    print(f"  Filled from URL     : {filled_url}")
    print(f"  Fixed from URL      : {fixed_url}")
    print(f"  Filled from name    : {filled_name}")
    print(f"  Unchanged           : {unchanged}")
    print(f"  Total changed       : {renamed + filled_url + fixed_url + filled_name} / {len(rows)}")

    if dry_run:
        print("\nDry run — CSV not modified.  Re-run with --live to apply.")
        return

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nCSV written: {csv_path}")


if __name__ == "__main__":
    main()
