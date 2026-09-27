#!/usr/bin/env python3
"""Rebuild the U.S. display mask from Census state polygons.

The jurisdiction polygons in state-boundary.json are landmass outlines. They
are useful for feed clipping, but dissolving them for the presentation mask
cuts the U.S. side of the Great Lakes out of the country. Census's regular
State_County layer includes those waters. This is a build-time script; the
website only serves the resulting static GeoJSON.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

from shapely import set_precision
from shapely.geometry import MultiPolygon, Point, Polygon, mapping, shape
from shapely.ops import unary_union


ROOT = Path(__file__).resolve().parent.parent
BOUNDARY_FILE = ROOT / "country-boundary.json"
STATES_FILE = ROOT / "state-boundary.json"
CENSUS_STATES = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/"
    "TIGERweb/State_County/MapServer/0/query"
)


def fetch_state(geoid):
    query = urlencode(
        {
            "where": f"GEOID='{geoid}'",
            "outFields": "GEOID",
            "outSR": "4326",
            "f": "geojson",
        }
    )
    with urlopen(f"{CENSUS_STATES}?{query}", timeout=45) as response:
        result = json.load(response)
    features = result.get("features", [])
    if len(features) != 1 or features[0]["properties"]["GEOID"] != geoid:
        raise RuntimeError(f"Unexpected Census response for state {geoid}")
    geometry = shape(features[0]["geometry"])
    if not geometry.is_valid:
        raise RuntimeError(f"Invalid Census geometry for state {geoid}")
    return geometry


def polygons(geometry):
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    raise RuntimeError(f"Unexpected dissolved geometry: {geometry.geom_type}")


def main():
    states = json.loads(STATES_FILE.read_text())
    lower_48_ids = sorted(
        feature["properties"]["GEOID"]
        for feature in states["features"]
        if feature["properties"]["COUNTRY"] == "US"
        and feature["properties"]["STUSAB"] not in {"AK", "HI"}
    )
    if len(lower_48_ids) != 49:  # 48 states plus Washington, D.C.
        raise RuntimeError(f"Expected 49 contiguous jurisdictions, got {len(lower_48_ids)}")

    with ThreadPoolExecutor(max_workers=6) as pool:
        state_geometries = list(pool.map(fetch_state, lower_48_ids))

    # Dissolve BEFORE simplifying so shared state lines cannot turn into gaps.
    mainland = unary_union(state_geometries)
    if not mainland.is_valid:
        raise RuntimeError("Dissolved mainland boundary is invalid")
    mainland = set_precision(mainland.simplify(0.002, preserve_topology=True), 0.00001)
    if not mainland.is_valid:
        raise RuntimeError("Simplified mainland boundary is invalid")

    # The previous source already splits Alaska's Aleutian islands safely at
    # the antimeridian. Preserve those and Hawaii while replacing the faulty
    # lower-48 landmass silhouette.
    country = json.loads(BOUNDARY_FILE.read_text())
    usa = next(
        feature for feature in country["features"]
        if feature["properties"].get("name") == "United States"
    )
    remote_parts = []
    for coords in usa["geometry"]["coordinates"]:
        part = Polygon(coords[0], coords[1:])
        west, south, east, north = part.bounds
        if east < -129 and (south > 50 or north < 24):
            remote_parts.append(part)

    rebuilt = MultiPolygon(remote_parts + polygons(mainland))
    if not rebuilt.is_valid:
        raise RuntimeError("Rebuilt U.S. presentation boundary is invalid")

    # The marked Lake Superior wedge must remain visible on the U.S. side;
    # nearby Canadian water must stay covered.
    for longitude, latitude in [(-91.0, 47.25), (-90.5, 47.3), (-90.0, 47.3)]:
        if not rebuilt.covers(Point(longitude, latitude)):
            raise RuntimeError("U.S. Lake Superior water is missing from mask")
    if rebuilt.covers(Point(-90.5, 48.5)):
        raise RuntimeError("Canadian territory leaked into U.S. mask")

    usa["geometry"] = mapping(rebuilt)
    BOUNDARY_FILE.write_text(json.dumps(country, separators=(",", ":")) + "\n")
    print(
        f"Wrote {BOUNDARY_FILE.name}: {len(lower_48_ids)} mainland jurisdictions, "
        f"{len(remote_parts)} Alaska/Hawaii parts, {len(polygons(mainland))} mainland parts"
    )


if __name__ == "__main__":
    main()
