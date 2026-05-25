"""IMAX Alert Flask application factory."""
from flask import Flask
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def create_app(config_name="default"):
    """Application factory."""
    from config import config

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_object(config[config_name])

    db.init_app(app)

    with app.app_context():
        from app import models  # noqa: F401

        db.create_all()
        _seed_theaters(app)

    from app.routes import main_bp, api_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp, url_prefix="/api")

    return app


def _seed_theaters(app):
    """Seed initial IMAX theater data."""
    from app.models import Theater

    if Theater.query.count() > 0:
        return

    theaters = [
        Theater(
            name="AMC Lincoln Square 13",
            chain="AMC",
            address="1998 Broadway",
            city="New York",
            state="NY",
            zip_code="10023",
            latitude=40.7751,
            longitude=-73.9826,
            screen_size="76 x 97 feet",
            projector_type="IMAX with Laser",
            audio_system="IMAX 12-channel",
            website="https://www.amctheatres.com/movie-theatres/new-york/amc-lincoln-square-13",
            phone="(888) 262-4386",
            image_url="",
        ),
        Theater(
            name="TCL Chinese Theatre IMAX",
            chain="TCL",
            address="6925 Hollywood Blvd",
            city="Hollywood",
            state="CA",
            zip_code="90028",
            latitude=34.1022,
            longitude=-118.3404,
            screen_size="94 x 75 feet",
            projector_type="IMAX with Laser",
            audio_system="IMAX 12-channel",
            website="https://www.tclchinesetheatres.com",
            phone="(323) 461-3331",
            image_url="",
        ),
        Theater(
            name="Regal Edwards Ontario Palace IMAX",
            chain="Regal",
            address="4900 E 4th St",
            city="Ontario",
            state="CA",
            zip_code="91764",
            latitude=34.0633,
            longitude=-117.5636,
            screen_size="72 x 52 feet",
            projector_type="IMAX Digital",
            audio_system="IMAX 6-channel",
            website="https://www.regmovies.com",
            phone="(844) 462-7342",
            image_url="",
        ),
        Theater(
            name="Cinemark Century Aurora and XD",
            chain="Cinemark",
            address="14300 E Alameda Ave",
            city="Aurora",
            state="CO",
            zip_code="80012",
            latitude=39.7133,
            longitude=-104.8334,
            screen_size="70 x 50 feet",
            projector_type="IMAX Digital",
            audio_system="IMAX 6-channel",
            website="https://www.cinemark.com",
            phone="(303) 750-5771",
            image_url="",
        ),
        Theater(
            name="AMC Metreon 16",
            chain="AMC",
            address="135 4th St",
            city="San Francisco",
            state="CA",
            zip_code="94103",
            latitude=37.7830,
            longitude=-122.4037,
            screen_size="76 x 56 feet",
            projector_type="IMAX with Laser",
            audio_system="IMAX 12-channel",
            website="https://www.amctheatres.com/movie-theatres/san-francisco/amc-metreon-16",
            phone="(888) 262-4386",
            image_url="",
        ),
        Theater(
            name="Regal Fenway Stadium 13 & RPX",
            chain="Regal",
            address="201 Brookline Ave",
            city="Boston",
            state="MA",
            zip_code="02215",
            latitude=42.3446,
            longitude=-71.0983,
            screen_size="72 x 52 feet",
            projector_type="IMAX Digital",
            audio_system="IMAX 6-channel",
            website="https://www.regmovies.com",
            phone="(844) 462-7342",
            image_url="",
        ),
        Theater(
            name="AMC Navy Pier IMAX",
            chain="AMC",
            address="600 E Grand Ave",
            city="Chicago",
            state="IL",
            zip_code="60611",
            latitude=41.8917,
            longitude=-87.6048,
            screen_size="76 x 56 feet",
            projector_type="IMAX with Laser",
            audio_system="IMAX 12-channel",
            website="https://www.amctheatres.com",
            phone="(888) 262-4386",
            image_url="",
        ),
        Theater(
            name="Cinemark Houston 20 and XD",
            chain="Cinemark",
            address="7620 Katy Fwy",
            city="Houston",
            state="TX",
            zip_code="77024",
            latitude=29.7838,
            longitude=-95.4744,
            screen_size="70 x 50 feet",
            projector_type="IMAX Digital",
            audio_system="IMAX 6-channel",
            website="https://www.cinemark.com",
            phone="(713) 263-8900",
            image_url="",
        ),
    ]

    for theater in theaters:
        db.session.add(theater)
    db.session.commit()
