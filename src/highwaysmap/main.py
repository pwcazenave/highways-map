"""
Web app to map the highways agency road closures.

"""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from datetime import datetime
from dateutil.relativedelta import relativedelta

import flask
import folium
import requests
from flask_compress import Compress
from waitress import serve


logging.basicConfig(
    level=os.environ.get("LOGLEVEL", "INFO"),
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# Make the app globally available and put the subscription key in it
app = flask.Flask(__name__)
app.key = os.environ.get("SUBSCRIPTION_KEY")
if app.key is None:
    raise ValueError("No National Highways Agency API key provided.")

# Enable compression on rendered pages
Compress(app)


def run() -> None:
    """
    Simple function to initialise the Flask app.

    """

    if os.environ.get("LOGLEVEL") == "DEBUG":
        app.run(
            host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", 5000)),
            use_reloader=bool(os.environ.get("RELOADER", True)),
        )
    else:
        serve(
            app,
            host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", 5000)),
            threads=os.environ.get("THREADS", 50),
        )


@dataclass
class Closure:
    """
    A single road closure object with the associated information for plotting.
    
    Args:
        location (list): List of API closure output.
        cause (str): The cause of the current closure.
        start (str): The start time of the closure (ISO format)
        end (str): The end time of the closure (ISO format)
        comment (list[dict]): Any comments as a list of dictionaries: {"comment": "The comment"}.

    Methods:
        process: Process the closures.
        from_dict: Populate an instance of this object with data from a dictionary.

    """

    location: list
    cause: str
    start: str
    end: str
    comment: list[dict]

    info: dict = field(init=False, default_factory=dict)
    road_names: list = field(init=False, default_factory=list)

    # For sections with some open lanes, make them more see through; otherwise, make them opaque.
    opacity: dict = field(default_factory=lambda: {"open": 0.25, "closed": 1})

    def __post_init__(self):
        """
        Process the location data on initialisation.

        """
        self.time = {"start": self.start, "end": self.end}

        if self.location:
            self.process(self.location)

    def process(self, location):
        total_road_names = len(location["locationReferencingPointLocation"]["pointAlongLinearElement"])
        for iiii, point in enumerate(location["locationReferencingPointLocation"]["pointAlongLinearElement"], 1):
            logger.debug("      Processing %d of %d road names", iiii, total_road_names)
            if point["linearElement"]["roadName"] not in self.road_names:
                self.road_names.append(point["linearElement"]["roadName"])
        flat_coordinates = location["locationReferencingLinearLocation"]["gmlLineString"]["posList"].split()
        lanes = {"open": [], "closed": []}
        total_carriageways = len(location["locationReferencingLinearLocation"]["supplementaryPositionalDescription"]["carriageway"])
        for iiiii, carriageway in enumerate(location["locationReferencingLinearLocation"]["supplementaryPositionalDescription"]["carriageway"], 1):
            logger.debug("      Processing %d of %d carriageways", iiiii, total_carriageways)
            if "_carriagewayExtensionG" in carriageway:
                lanes["open"].append(carriageway["_carriagewayExtensionG"]["numberOfOperationalLanes"])
                lanes["closed"].append(carriageway["_carriagewayExtensionG"]["numberOfLanesRestricted"])

        if lanes["open"]:
            for key, value in lanes.items():
                if lanes[key]:
                    lanes[key] = max(value)
                else:
                    lanes[key] = "unknown"

        if lanes["open"] != "unknown" and lanes["open"] == 0:
            self.alpha = self.opacity["closed"]
        else:
            self.alpha = self.opacity["open"]

        # Create a formatted tooltip using HTML
        self.info["name"] = " ".join(self.road_names)
        self.info["description"] = " ".join([i["comment"] for i in self.comment])
        self.info["start"] = self.time["start"]
        self.info["end"] = self.time["end"]
        self.info["cause"] = self.cause
        self.info["open"] = lanes["open"]
        self.info["closed"] = lanes["closed"]

        # Flip to lat/lon pairs for the folium stuff
        self.coordinates = [
            [float(j) for j in flat_coordinates[i : i + 2][::-1]]
            for i in range(0, len(flat_coordinates), 2)
        ]

    def from_dict(self, dictionary):
        """
        Load closures from a dictionary into an instance of this object.
        
        Args:
            dictionary (dict): The dictionary from which to re-populate the attributes of this dataclass.

        """

        for k, v in dictionary.items():
            setattr(self, k, v)


@dataclass
class Closures:
    """
    Closures from the National Highways Agency API with some convenience methods.
    
    Args:
        key (str): The subscription key for the API.
        api_url (str, optional): The URL from which to fetch the closures.
        closures_file (Path, optional): File to store the closure JSON in. Defaults to closures.json.
        processed_file (Path, optional) = File to store the processed closure JSON in. Defaults to processed.json.
        time_format (str, optional): The time string formatting for the tooltip. Defaults to "%d/%m/%Y %H:%M".

    Methods:
        refresh_closures: Reload closures from the API or load from disk.
        process_closures: Extract the closures of interest and save to disk.
        load_closures: Load processed closures from disk and populate object.    

    """

    key: str
    api_url: Optional[str] = ("https://api.data.nationalhighways.co.uk/roads/v1.0/closures")
    closures_payload: dict = field(default_factory=dict)
    closures_file: Optional[Path] = field(default=Path("closures.json"))
    processed_file: Optional[Path] = field(default=Path("processed.json"))
    closures: Optional[list[dict]] = field(default_factory=list)
    time_format: Optional[str] = "%d/%m/%Y %H:%M"

    # Store whether we've fetched new data from the API data or not
    refreshed: bool = field(default=False, init=False)

    # Give some default colours for various closure types.
    colours: dict = field(
        init=False,
        default_factory=lambda: {
            "authorityOperation": "orange",
            "constructionWork": "red",
            "other": "darkblue",
            "roadMaintenance": "red",
        },
    )

    pretty_causes: dict = field(
        init=False,
        default_factory=lambda: {
            "authorityOperation": "Local authority works",
            "constructionWork": "Construction work",
            "other": "Other",
            "roadMaintenance": "Road maintenance",
        },
    )

    def __post_init__(self):
        """
        Set up the closures as needed by either pulling from the API or loading from disk.

        Once we have the closures, process them if we haven't already.

        """
        self.refresh_closures()
        # To speed up rendering, we can, in the case where we haven't refreshed the closures from the API, use the
        # saved processed closures.
        if self.refreshed or not self.processed_file.exists():
            self.process_closures()
        else:
            self.load_closures()

        self.total_closures = len(self.closures)

    def refresh_closures(self):
        """
        Check the validity of the closures data and refresh if necessary.

        """
        now = datetime.now(ZoneInfo("Europe/London"))

        if self.closures_file.exists() and self.closures_file.stat().st_size == 0:
            self.closures_file.unlink()

        if self.closures_file.exists():
            closures_updated_time = datetime.fromtimestamp(self.closures_file.stat().st_ctime, ZoneInfo("Europe/London"))
        else:
            closures_updated_time = now

        headers = {
            "X-Response-MediaType": "application/json",
            "X-Djson-Format": "DATEXII",
            "Cache-Control": "no-cache",
            "Ocp-Apim-Subscription-Key": self.key,
        }

        if not self.closures_file.exists():
            logger.info("Initial API raw closure fetch")
            self.closures_payload = requests.get(self.api_url, headers=headers).json()
            self.closures_file.write_text(json.dumps(self.closures_payload))
            self.refreshed = True
        elif self.closures_file.exists() and closures_updated_time < now - relativedelta(days=1):
            logger.info("Fresh raw closures API fetch")
            self.closures_payload = requests.get(self.api_url, headers=headers).json()
            self.closures_file.write_text(json.dumps(self.closures_payload))
            self.refreshed = True
        else:
            logger.info("Loading existing raw closures from JSON")
            self.closures_payload = json.loads(self.closures_file.read_text())
            logger.info("Loaded existing raw closures from JSON")

    def process_closures(self) -> dict:
        """
        Process the closures to pull out the relevant information for mapping.

        We:
            - Find relevant types of closure
            - Set opacities for segments based on carriageways open
            - Set tooltips for each segment
            - Create a set of coordinates

        Args:
            closures_payload (dict): The raw closure payload from the upstream API.

        Returns:
            processed_closures (dict): The processed closures.

        """

        logger.info("Processing closures")

        now = datetime.now(ZoneInfo("Europe/London"))

        total_situations = len(self.closures_payload["D2Payload"]["situation"])
        for i, situation in enumerate(self.closures_payload["D2Payload"]["situation"], 1):
            logger.debug("Processing %d of %d situations", i, total_situations)
            total_situation_records = len(situation["situationRecord"])
            for ii, locations in enumerate(situation["situationRecord"], 1):
                logger.debug("  Processing %d of %d situation records", ii, total_situation_records)
                # Only display those that are certain to occur
                validity = locations["probabilityOfOccurrence"].lower()
                if validity not in ("certain"):
                    break

                comment = locations["generalPublicComment"]
                # Now we know it's valid, check for the time window
                in_time = locations["validity"]["validityStatus"]
                start_time, end_time = [locations["validity"]["validityTimeSpecification"][i] for i in ("overallStartTime", "overallEndTime")]
                # Replace 'Z' with +00:00 to make a valid ISO spec time
                start_time = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                end_time = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                if in_time == "definedByValidityTimeSpec":
                    if not start_time < now < end_time:
                        break

                cause = locations["cause"]["causeType"]

                total_groups = len(locations["locationReference"]["locationReferencingLocationGroupByList"]["locationContainedInGroup"])
                for iii, location_group in enumerate(locations["locationReference"]["locationReferencingLocationGroupByList"]["locationContainedInGroup"], 1):
                    logger.debug("    Processing %d of %d location groups", iii, total_groups)
                    self.closures.append(
                        Closure(
                            location_group,
                            cause,
                            start_time.strftime(self.time_format),
                            end_time.strftime(self.time_format),
                            comment,
                        )
                    )
                logger.debug("    Finished processing location group")
            logger.debug("  Finished processing situation records")
        logger.debug("Finished processing situations")

        self.processed_file.write_text(json.dumps([i.__dict__ for i in self.closures]))

        logger.info("Processed closures")

    def load_closures(self):
        """
        Load processed closures from JSON and into a list of closures.

        """
        logger.info("Loading processed closures from JSON")
        if self.processed_file.exists():
            closures = json.loads(self.processed_file.read_text())
            for closure in closures:
                # logger.info(closure)
                c = Closure({}, "", "", "", [{"comment": ""}])
                c.from_dict(closure)
                self.closures.append(c)
        logger.info("Loaded processed closures from JSON")


@app.route("/")
async def map() -> str:
    """
    Render a map.

    Returns:
        page (str): The rendered HTML for the page template.

    """

    m = folium.Map(
        # Start focused on London
        location=[51.509865, -0.118092],
        zoom_start=7,  # most of the country
    )

    closures = Closures(app.key)

    if closures.refreshed:
        for i, closure in enumerate(closures.closures, 1):
            logger.debug("Processing %d of %d closures", i, closures.total_closures)

            tooltip_content = f"""
            <div style="font-family: Arial, sans-serif; font-size: 14px; line-height: 1.4;">
                <b>Name:</b> {closure.info['name']}<br>
                <b>Description:</b> {closure.info['description']}<br>
                <b>From:</b> {closure.info['start']}<br>
                <b>To:</b> {closure.info['end']}<br>
                <b>Cause:</b> {closures.pretty_causes[closure.cause]}<br>
                <b>Open carriageways:</b> {closure.info['open']}<br>
                <b>Closed carriageways:</b> {closure.info['closed']}
            </div>
            """

            folium.PolyLine(
                locations=closure.coordinates,
                color=closures.colours[closure.cause],
                weight=5,
                opacity=closure.alpha,
                tooltip=folium.Tooltip(tooltip_content),
            ).add_to(m)

        logger.info("Rendering HTML string")
        map_html = m.get_root().render()
        logger.info("Rendered HTML string")
        logger.info("Saving HTML to disk")
        m.save(Path("map.html"))
        logger.info("Saved HTML to disk")
    else:
        logger.info("Loading HTML from disk")
        map_html = Path("map.html").read_text()
        logger.info("Loaded HTML from disk")

    # Render the template with the map
    response = flask.make_response(
        flask.render_template("index.html", map_html=map_html)
    )
    response.headers["Cache-Control"] = "public, max-age=3600"  # Cache for 1 hour

    return response


@app.route("/contact")
@app.route("/data")
@app.route("/map")
@app.route("/")
async def placeholder() -> None:
    """
    Placeholder function which just calls the map page.

    Returns:
        map (str): The map HTML.
    """

    return await map()


if __name__ == "__main__":
    run()
