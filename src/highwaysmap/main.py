"""
Web app to map the highways agency road closures.

"""

import json
import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from datetime import datetime
from dateutil.relativedelta import relativedelta

import flask
import folium
import requests


logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"),
                    format="%(asctime)s - %(levelname)s - %(message)s",
                    handlers=[logging.StreamHandler()])
logger = logging.getLogger(__name__)

# Make the app globally available and put the subscription key in it
app = flask.Flask(__name__)
app.key = os.environ.get("SUBSCRIPTION_KEY")
if app.key is None:
    raise ValueError("No National Highways Agency API key provided.")


def run() -> None:
    """
    Simple function to initialise the Flask app.

    """

    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)), use_reloader=bool(os.environ.get("RELOADER", True)))


@app.route("/map")
async def map() -> str:
    """
    Render a map.

    Returns:
        page (str): The rendered HTML for the page template.

    """

    now = datetime.now(ZoneInfo('Europe/London'))

    closures_url = "https://api.data.nationalhighways.co.uk/roads/v1.0/closures"
    closures_file = Path('closures.json')
    if not closures_file.exists() or closures_file.stat().st_size == 0 or datetime.fromtimestamp(closures_file.stat().st_ctime, ZoneInfo('Europe/London')) < now - relativedelta(days=1):
        logger.info('Fetching fresh closures JSON')
        closures_payload = requests.get(closures_url, headers={"X-Response-MediaType": "application/json", "X-Djson-Format": "DATEXII", "Cache-Control": "no-cache", "Ocp-Apim-Subscription-Key": app.key}).json()
        closures_file.write_text(json.dumps(closures_payload))
    else:
        logger.info('Loading existing closures JSON')
        closures_payload = json.loads(closures_file.read_text())
        
    m = folium.Map(
        # Start focused on London
        location=[51.509865, -0.118092],
        zoom_start=7  # most of the country
    )

    colours = {"authorityOperation": "orange",
               "constructionWork": "red",
               "other": "darkblue",
               "roadMaintenance": "red"}
    
    # For sections with some open lanes, make them more see through; otherwise, make them opaque.
    opacity = {"open": 0.25,
               "closed": 1}
    
    pretty_causes = {"authorityOperation": "Local authority works",
                     "constructionWork": "Construction work",
                     "other": "Other",
                     "roadMaintenance": "Road maintenance"}

    for situation in closures_payload["D2Payload"]["situation"]:
        for locations in situation["situationRecord"]:
            info = {}
            # Only display those that are certain to occur
            validity = locations["probabilityOfOccurrence"].lower()
            if validity not in ('certain'):
                break

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

            for location_group in locations["locationReference"]["locationReferencingLocationGroupByList"]["locationContainedInGroup"]:
                road_names = []
                for point in location_group["locationReferencingPointLocation"]["pointAlongLinearElement"]:
                    if point["linearElement"]["roadName"] not in road_names:
                        road_names.append(point["linearElement"]["roadName"])
                flat_coordinates = location_group["locationReferencingLinearLocation"]["gmlLineString"]["posList"].split()
                lanes = {'open': [], 'closed': []}
                for carriageway in location_group["locationReferencingLinearLocation"]["supplementaryPositionalDescription"]["carriageway"]:
                    if "_carriagewayExtensionG" in carriageway:
                        lanes['open'].append(carriageway["_carriagewayExtensionG"]["numberOfOperationalLanes"])
                        lanes['closed'].append(carriageway["_carriagewayExtensionG"]["numberOfLanesRestricted"])

                if lanes['open']:
                    for key, value in lanes.items():
                        if lanes[key]:
                            lanes[key] = max(value)
                        else:
                            lanes[key] = "unknown"

                if lanes['open'] != "unknown" and lanes['open'] == 0:
                    alpha = opacity['closed']
                else:
                    alpha = opacity['open']

                # Create a formatted tooltip using HTML
                info["name"] = ' '.join(road_names)
                info["description"] = ' '.join([i["comment"] for i in locations["generalPublicComment"]])
                info["start"] = start_time.strftime(r"%d/%m/%Y %H:%M")
                info["end"] = end_time.strftime(r"%d/%m/%Y %H:%M")
                info["cause"] = pretty_causes[cause]
                info["open"] = lanes['open']
                info["closed"] = lanes['closed']

                tooltip_content = f"""
                <div style="font-family: Arial, sans-serif; font-size: 14px; line-height: 1.4;">
                    <b>Name:</b> {info['name']}<br>
                    <b>Description:</b> {info['description']}<br>
                    <b>From:</b> {info['start']}<br>
                    <b>To:</b> {info['end']}<br>
                    <b>Cause:</b> {info['cause']}<br>
                    <b>Open carriageways:</b> {info['open']}<br>
                    <b>Closed carriageways:</b> {info['closed']}
                </div>
                """

                # Flip to lat/lon for the folium stuff
                coordinates = [[float(j) for j in flat_coordinates[i:i+2][::-1]] for i in range(0, len(flat_coordinates), 2)]

                folium.PolyLine(
                    locations=coordinates,
                    color=colours[cause],
                    weight=5,
                    opacity=alpha,
                    tooltip=folium.Tooltip(tooltip_content)
                ).add_to(m)

    map_html = m._repr_html_()

    # Render the template with the map
    return flask.render_template("index.html", map_html=map_html)


if __name__ == "__main__":

    run()
