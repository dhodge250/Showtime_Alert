# Scraper Coverage Roadmap

Tracks showtime scraper status for all IMAX theaters in the database. Source data: `seeds/_main_chains.csv`.

**North America total: 459 locations across 81 chains**

---

## Status Key

| Status | Meaning |
|--------|---------|
| ✅ Implemented | Scraper exists and is working |
| 🔧 Rebuilding | Scraper exists but is broken; tracked for rebuild |
| 🗓 Planned | Issue exists; not yet implemented |
| ❌ Not started | No issue yet |

---

## Phase 1 — Rebuild Broken Scrapers

| Chain | NA Locations | Status | Issue | File |
|-------|-------------|--------|-------|------|
| AMC | 188 | 🔧 Rebuilding | #130 | `app/scrapers/amc.py` |
| Regal | 96 | 🔧 Rebuilding | #131 | `app/scrapers/regal.py` |
| Cinemark | 15 | 🔧 Rebuilding | #132 | `app/scrapers/cinemark.py` |
| TCL Chinese Theatre | 1 | 🔧 Rebuilding | #133 | `app/scrapers/tcl.py` |

---

## Phase 2 — Working Scrapers

| Chain | NA Locations | Status | File |
|-------|-------------|--------|------|
| Cineplex | 29 | ✅ Implemented | `app/scrapers/cineplex.py` |
| Royal BC Museum | 1 | ✅ Implemented | `app/scrapers/royal_bc_museum.py` |

---

## Phase 2 — New North American Chains

### Multi-location chains (2+ locations)

| Chain | NA Locations | Status | Issue |
|-------|-------------|--------|-------|
| Landmark Cinemas | 6 | 🗓 Planned | #86 |
| Apple Cinemas | 6 | 🗓 Planned | #134 |
| Hooky | 5 | 🗓 Planned | #90 |
| CinemaWest | 5 | 🗓 Planned | #135 |
| Megaplex | 5 | ❌ Not started | — |
| CMX / CMX Cinemas | 5 | 🗓 Planned | #136 |
| Santikos | 4 | 🗓 Planned | #137 |
| Marcus | 3 | 🗓 Planned | #138 |
| Malco | 3 | 🗓 Planned | #139 |
| NCG | 3 | 🗓 Planned | #140 |
| Premiere Cinema | 3 | 🗓 Planned | #141 |
| Epic Theatres | 3 | 🗓 Planned | #142 |
| Galaxy Theatres | 3 | 🗓 Planned | #143 |
| MJR | 2 | 🗓 Planned | #87 |
| Celebration Cinema | 2 | 🗓 Planned | #88 |
| EVO Entertainment | 2–3 | 🗓 Planned | #89 |
| RC Theatres | 2 | 🗓 Planned | #144 |
| Penn Cinema | 2 | 🗓 Planned | #145 |
| Phoenix Theatres | 2 | 🗓 Planned | #146 |
| GQT | 2 | 🗓 Planned | #147 |
| Jordan's Furniture | 2 | 🗓 Planned | #148 |
| Cinepolis | 2 | 🗓 Planned | #149 |
| MetroLux Theatres | 2 | 🗓 Planned | #91 |

### Single-location commercial chains

| Chain | Status | Issue |
|-------|--------|-------|
| Harkins | 🗓 Planned | #85 |
| Esquire Theatre | 🗓 Planned | #91 |
| Branson's Entertainment | 🗓 Planned | #91 |
| Reading Cinemas | 🗓 Planned | #150 |
| Paragon | 🗓 Planned | #150 |
| Georgia Theatre Company | 🗓 Planned | #150 |
| AmStar Cinemas | 🗓 Planned | #150 |
| Royal Cinemas | 🗓 Planned | #150 |
| Showplace | 🗓 Planned | #150 |
| Fridley Theatres | 🗓 Planned | #150 |
| Emagine | 🗓 Planned | #150 |
| Brenden Theatres | 🗓 Planned | #150 |
| Tropicana | 🗓 Planned | #150 |
| Town Square Entertainment | 🗓 Planned | #150 |
| Southampton Playhouse | 🗓 Planned | #150 |
| Golden Ticket Cinemas | 🗓 Planned | #150 |
| Showcase Cinemas | 🗓 Planned | #150 |
| Showbiz Cinemas | 🗓 Planned | #150 |

### Museums & Science Centers

All museum/science center IMAX theaters are tracked in a single issue, grouped by ticketing platform. Depends on website URL population (#83) before implementation.

| Status | Issue |
|--------|-------|
| 🗓 Planned | #92 (US + Canada museums) |
| 🗓 Planned | #84 (Pacific Science Center, Seattle) |

---

## Phase 3 — Global Expansion

International chains tracked in epic #151. Major markets:

| Region | Key Chains |
|--------|-----------|
| UK / Europe | Odeon, Vue, Cineworld, Pathé, UCI, CinemaxX |
| Australia / NZ | Hoyts, Event Cinemas |
| India | PVR Cinemas, INOX |
| China | Wanda Cinemas, Jinyi Cinema, InJoy Bestar, Omnijoi |
| Japan | AEON Cinema, T-Joy, Toho Cinemas |
| Middle East | VOX Cinemas, Novo Cinemas |
| Latin America | Cinépolis (Mexico+), Cinemark (LATAM) |

---

## Coverage Summary

| Phase | Locations | % of NA Total |
|-------|-----------|--------------|
| ✅ Working | 30 | 7% |
| 🔧 Rebuild (Phase 1) | 300 | 65% |
| 🗓 Planned (Phase 2) | ~110 | ~24% |
| ❌ Not started | ~19 | ~4% |
| **NA Total** | **~459** | **100%** |
