"""Flask CLI commands for IMAX Alert."""
import click as _click

from app import db


def register_cli(app):
    """Register custom Flask CLI commands on *app*."""

    @app.cli.command("cleanup-chains")
    @_click.option("--dry-run", is_flag=True, default=False,
                   help="Preview changes without writing to the database.")
    def _cleanup_chains_cmd(dry_run):
        """Merge legacy chain name variants and populate chain homepage URLs.

        Two operations are performed in sequence:

        1. MERGE — For every old suffixed name that now has a simplified
           equivalent in the database (e.g. "Wanda Cinemas" + "Wanda"),
           all theater rows are re-pointed to the simplified chain and the
           old record is deleted.  If only the old name exists (no new
           counterpart yet), it is renamed in place.

        2. WEBSITES — Sets the homepage URL on every chain that currently
           has no website, using a built-in name → URL mapping.
        """
        import click as _ck
        from app.models import Chain, Theater

        # Old name → simplified name (mirrors _CHAIN_RENAMES in enrich_csv_chains.py)
        _RENAMES: dict[str, str] = {
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
            "AmStar Cinemas":           "AmStar",
            "Phoenix Theatres":         "Phoenix",
            "RC Theatres":              "RC",
            "Cathay Cineplexes":        "Cathay",
            "Celebration! Cinema":      "Celebration!",
            "Celebration Cinema":       "Celebration!",
            "Emagine Entertainment":    "Emagine",
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
            "Golden Screen Cinemas":    "GSC",
            "Esplanade Cineplex":       "Major",
            "Apple Cinemas":            "Showcase",
            "IMAX Entertainment":       "Event",
            "Gaumont":                  "Pathé",
            "La Géode":                 "Pathé",
            "Cine Loire":               "Pathé",
            "blue Cinema":              "blue",
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
        }

        # Simplified chain name → homepage URL
        _WEBSITES: dict[str, str] = {
            # North America
            "AMC":              "https://www.amctheatres.com",
            "Regal":            "https://www.regmovies.com",
            "Cinemark":         "https://www.cinemark.com",
            "Cineplex":         "https://www.cineplex.com",
            "Marcus":           "https://www.marcustheatres.com",
            "Malco":            "https://www.malco.com",
            "Landmark":         "https://www.landmarkcinemas.com",
            "Galaxy":           "https://www.galaxytheatres.com",
            "Epic":             "https://www.epictheatres.com",
            "Santikos":         "https://www.santikos.com",
            "NCG":              "https://www.ncgcinemas.com",
            "CinemaWest":       "https://www.cinemawest.com",
            "RC":               "https://www.rctheatres.com",
            "Megaplex":         "https://www.megaplextheatres.com",
            "AmStar":           "https://www.amstarcinemas.com",
            "Celebration!":     "https://www.celebrationcinema.com",
            "Emagine":          "https://www.emagine-entertainment.com",
            "CMX":              "https://www.cmxcinemas.com",
            "Phoenix":          "https://www.phoenixtheatres.com",
            "Showcase":         "https://www.showcasecinemas.com",
            "Reading":          "https://www.readingcinemas.com",
            "Cinepolis":        "https://www.cinepolis.com",
            "Cinemex":          "https://www.cinemex.com",
            "Supercines":       "https://www.supercines.com",
            # UK / Ireland / Europe
            "Cineworld":        "https://www.cineworld.co.uk",
            "Odeon":            "https://www.odeon.co.uk",
            "Vue":              "https://www.myvue.com",
            "CineStar":         "https://www.cinestar.de",
            "UCI":              "https://www.uci-kinowelt.de",
            "Kinepolis":        "https://kinepolis.com",
            "Pathé":            "https://www.pathe.fr",
            "UGC":              "https://www.ugc.fr",
            "MK2":              "https://mk2.com",
            "CGR":              "https://www.cgr.fr",
            "Megarama":         "https://www.megarama.fr",
            "CineplexX":        "https://www.cineplexx.at",
            "Hollywood Megaplex":"https://www.megaplex.at",
            "Cinema City":      "https://www.cinema-city.pl",
            "Multikino":        "https://www.multikino.pl",
            "Filmstaden":       "https://www.filmstaden.se",
            "Cinesa":           "https://www.cinesa.es",
            "NOS":              "https://cinemas.nos.pt",
            "blue":             "https://www.bluecinema.ch",
            # Asia-Pacific
            "Hoyts":            "https://www.hoyts.com.au",
            "Event":            "https://www.eventcinemas.com.au",
            "Village":          "https://www.villagecinemas.com.au",
            "Dendy":            "https://www.dendy.com.au",
            "PVR INOX":         "https://www.pvrinox.com",
            "CGV":              "https://www.cgv.com.cn",
            "Wanda":            "https://www.wandacinemas.com/en/",
            "Jinyi":            "https://www.jycinema.com",
            "Omnijoi":          "https://www.omnijoi.com",
            "Bona":             "https://www.bonafilm.cn",
            "Golden Village":   "https://www.gv.com.sg",
            "GSC":              "https://www.gsc.com.my",
            "TGV":              "https://www.tgvcinemas.com",
            "Cathay":           "https://www.cathaycineplexes.com",
            "Shaw":             "https://www.shaw.sg",
            "XXI":              "https://www.cinema21.co.id",
            "Major":            "https://www.majorcineplex.com",
            "Toho":             "https://www.tohotheater.jp",
            "United":           "https://www.unitedcinemas.jp",
            "AEON":             "https://www.aeoncinema.com",
            "SFC":              "https://www.sh-sfc.com",
            "Emperor":          "https://www.emperorcinemas.com",
            "Paribu":           "https://www.paribucineverse.com",
            # Middle East / Africa
            "VOX":              "https://www.voxcinemas.com",
            "Ster-Kinekor":     "https://www.sterkinekor.com",
            "Nu Metro":         "https://www.numetro.co.za",
            # Russia / CIS
            "Kinomax":          "https://www.kinomax.ru",
            "Kinopark":         "https://www.kinopark.kz",
            "Formula Kino":     "https://www.formula-kino.ru",
            "Kronverk":         "https://www.kronverk.ru",
            "Planeta Kino":     "https://planetakino.ua",
        }

        merged_count = 0
        website_count = 0

        # ── Step 1: merge old → simplified ────────────────────────────
        _ck.echo("── Merging legacy chain names ──")
        for old_name, new_name in _RENAMES.items():
            old = Chain.query.filter(
                db.func.lower(Chain.name) == old_name.lower()
            ).first()
            if not old:
                continue

            new = Chain.query.filter(
                db.func.lower(Chain.name) == new_name.lower()
            ).first()

            theater_count = Theater.query.filter_by(chain_id=old.id).count()

            if new and new.id != old.id:
                # Both old and simplified exist — re-point theaters and delete old
                _ck.echo(f"  Merge  {old.name!r} -> {new.name!r}"
                         f"  ({theater_count} theaters re-pointed)")
                if not dry_run:
                    Theater.query.filter_by(chain_id=old.id).update(
                        {"chain_id": new.id, "chain": new.name},
                        synchronize_session=False,
                    )
                    db.session.delete(old)
            else:
                # Only old name exists — rename in place
                _ck.echo(f"  Rename {old.name!r} -> {new_name!r}"
                         f"  ({theater_count} theaters updated)")
                if not dry_run:
                    Theater.query.filter_by(chain_id=old.id).update(
                        {"chain": new_name}, synchronize_session=False
                    )
                    old.name = new_name

            merged_count += 1

        if not dry_run:
            db.session.commit()
            _ck.echo(f"  Committed {merged_count} merge/rename operations.\n")
        else:
            _ck.echo(f"  (dry run — {merged_count} would change)\n")

        # ── Step 2: populate chain websites ───────────────────────────
        _ck.echo("── Populating chain websites ──")
        for chain_name, homepage in _WEBSITES.items():
            chain = Chain.query.filter(
                db.func.lower(Chain.name) == chain_name.lower()
            ).first()
            if not chain:
                continue
            if chain.website:
                continue  # already set — don't overwrite
            _ck.echo(f"  {chain.name:<30}  {homepage}")
            if not dry_run:
                chain.website = homepage
            website_count += 1

        if not dry_run:
            db.session.commit()
            _ck.echo(f"  Committed {website_count} website updates.\n")
        else:
            _ck.echo(f"  (dry run — {website_count} would be set)\n")

        _ck.echo(f"Done.  Merged/renamed: {merged_count}  Websites set: {website_count}"
                 + ("  [DRY RUN]" if dry_run else ""))
