"""
dnbr_burnscar.py

NASA ACRES Maui - map the 2023 Kula/Upcountry burn scar from Sentinel-2, and
derive a clean burn-perimeter polygon.

No public agency published a polygon for the 2023 Upcountry (Kula) fire, so we
derive the burn extent straight from satellite data. We compute the differenced
Normalized Burn Ratio (dNBR) between a pre-fire and a post-fire Sentinel-2
composite over the corridor. Fire kills vegetation and chars the ground, which
drops the Normalized Burn Ratio, so the pre-minus-post difference lights up
where it burned.

Then we turn that into one polygon: threshold dNBR at a burn-severity break,
drop tiny specks, keep the single largest contiguous patch, and vectorize it.
That largest patch is the fire (seasonal grass browning shows up as scattered
small specks, the fire is one big connected blob).

Outputs:
    Drive: dNBR_kula_2023.tif    (bands dNBR, NBR_pre, NBR_post)
    Local: data/burn_perimeter/kula_2023_perimeter.geojson  (the polygon)

WHY a tight post-fire window (Aug 13-28, 2023): this corridor is dry grass that
browns through the dry season, and that browning also lowers NBR. A short window
right after the fire keeps the seasonal signal small so the burn stands out.

USGS dNBR burn-severity breakpoints:
    < 0.10  unburned        0.27 - 0.44  moderate-low
    0.10 - 0.27  low        0.44 - 0.66  moderate-high      > 0.66  high

Author : Samuel Dameg (SammyCode002)
Project: NASA ACRES Maui  (https://github.com/SammyCode002/nasa-acres-maui)

Run:
    uv run --with earthengine-api python dnbr_burnscar.py
"""

import functools
import json
import logging
import os
import time

import ee

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

EE_PROJECT = "ace-shine-392702"
AOI_PATH = "aoi/corridor.geojson"
AOI_FALLBACK_BBOX = [-156.470, 20.670, -156.290, 20.830]

# The Upcountry/Kula fire ignited Aug 8, 2023. Sentinel-2 imaged Maui on Aug 8
# (pre) and Aug 13 (post). The post window stays tight to limit seasonal drift.
PRE_START, PRE_END = "2023-07-10", "2023-08-08"
POST_START, POST_END = "2023-08-13", "2023-08-28"

COLLECTION_ID = "COPERNICUS/S2_SR_HARMONIZED"
MAX_CLOUD_PCT = 60
EXPORT_SCALE = 20  # meters; matches Sentinel-2 SWIR native resolution
DRIVE_FOLDER = "nasa_acres_maui_gvmi"
EXPORT_NAME = "dNBR_kula_2023"

# Burn-perimeter settings.
BURN_THRESHOLD = 0.27  # dNBR moderate-low and up = "burned" for the perimeter
MIN_PATCH_PIXELS = 50  # drop patches smaller than this (~5 acres of specks)
PERIMETER_LOCAL = "data/burn_perimeter/kula_2023_perimeter.geojson"

# --------------------------------------------------------------------------- #
# 4x4 debug logging
# --------------------------------------------------------------------------- #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("dnbr_burnscar")


def _short(obj, limit=120):
    """Truncate a repr so server-side ee objects don't flood the log."""
    text = repr(obj)
    return text if len(text) <= limit else text[:limit] + "..."


def debug_log(func):
    """Log inputs, outputs, timing, and status on every call.

    WHY a decorator: every step self-reports the same way, so if a run dies the
    log already names the function, its inputs, and how long it ran.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        logger.info(
            "[INPUT ] %s args=%s kwargs=%s", func.__name__, _short(args), _short(kwargs)
        )
        try:
            result = func(*args, **kwargs)
            elapsed = time.perf_counter() - start
            logger.info("[OUTPUT] %s -> %s", func.__name__, _short(result))
            logger.info("[TIME  ] %s took %.3fs", func.__name__, elapsed)
            logger.info("[STATUS] %s OK", func.__name__)
            return result
        except Exception as exc:  # noqa: BLE001  (re-raise after logging)
            elapsed = time.perf_counter() - start
            logger.exception(
                "[STATUS] %s FAILED after %.3fs: %s", func.__name__, elapsed, exc
            )
            raise

    return wrapper


# --------------------------------------------------------------------------- #
# Core steps
# --------------------------------------------------------------------------- #


@debug_log
def initialize_ee(project):
    """Initialize Earth Engine (auth must already be done once on this machine)."""
    ee.Initialize(project=project)
    return ee.Number(1).getInfo()  # tiny round-trip to confirm the link is live


def _extract_geometry(geojson):
    """Pull the AOI geometry from the shared GeoJSON (prefers role == 'aoi')."""
    kind = geojson.get("type")
    if kind == "FeatureCollection":
        features = geojson["features"]
        aoi = next(
            (f for f in features if f.get("properties", {}).get("role") == "aoi"),
            features[0],
        )
        return aoi["geometry"]
    if kind == "Feature":
        return geojson["geometry"]
    return geojson


@debug_log
def load_aoi(path, fallback_bbox):
    """Load the corridor boundary as an ee.Geometry from the shared file."""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            geojson = json.load(handle)
        logger.info("[AOI   ] loaded boundary from %s", path)
        return ee.Geometry(_extract_geometry(geojson))
    logger.warning("[AOI   ] %s not found, using fallback bbox", path)
    west, south, east, north = fallback_bbox
    return ee.Geometry.Rectangle([west, south, east, north])


def mask_s2_clouds(image):
    """Mask cloud / shadow / cirrus / defective pixels via the SCL band.

    (Not decorated: it runs once per image inside a server-side .map().)
    """
    scl = image.select("SCL")
    keep = (
        scl.neq(1)
        .And(scl.neq(3))
        .And(scl.neq(8))
        .And(scl.neq(9))
        .And(scl.neq(10))
        .And(scl.neq(11))
    )
    return image.updateMask(keep)


def add_nbr(image):
    """Add the Normalized Burn Ratio band.

    WHY B8 and B12: NBR uses NIR and the longer SWIR (about 2.2 microns), the
    pair most sensitive to charred ground and lost vegetation. As a normalized
    difference it is a pure ratio, so the integer scaling cancels out.
        NBR = (B8 - B12) / (B8 + B12)
    """
    return image.addBands(image.normalizedDifference(["B8", "B12"]).rename("NBR"))


@debug_log
def nbr_composite(aoi, start, end):
    """Median NBR composite over a date window, plus the clear-scene count.

    WHY median: single dates are cloud-gapped; the per-pixel median across the
    window fills holes and ignores stray bad pixels. Returns (image, count).
    """
    coll = (
        ee.ImageCollection(COLLECTION_ID)
        .filterBounds(aoi)
        .filterDate(start, end)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", MAX_CLOUD_PCT))
        .map(mask_s2_clouds)
        .map(add_nbr)
    )
    count = coll.size().getInfo()
    return coll.select("NBR").median().clip(aoi), count


@debug_log
def export_to_drive(image, name, aoi):
    """Kick off a Drive export for the dNBR stack as a float GeoTIFF."""
    task = ee.batch.Export.image.toDrive(
        image=image.toFloat(),
        description=name,
        folder=DRIVE_FOLDER,
        fileNamePrefix=name,
        region=aoi,
        scale=EXPORT_SCALE,
        crs="EPSG:4326",
        maxPixels=int(1e13),
    )
    task.start()
    return task


@debug_log
def derive_burn_perimeter(dnbr, aoi, threshold, min_patch):
    """Turn the dNBR raster into one clean burn-perimeter polygon.

    Steps, and WHY each one:
    1. threshold dNBR so anything at moderate-low severity or higher counts as
       burned (a binary mask).
    2. connectedPixelCount measures how big each connected blob is, and we drop
       blobs smaller than min_patch. That removes the scattered specks that
       seasonal grass browning leaves behind.
    3. reduceToVectors turns the surviving blobs into polygons.
    4. we sort by area and keep the single largest, which is the fire itself.

    Returns (geojson_feature_collection_dict, area_acres).
    """
    burn = dnbr.gte(threshold).selfMask()
    patch = burn.connectedPixelCount(maxSize=1024, eightConnected=True)
    big = burn.updateMask(patch.gte(min_patch))

    vectors = big.reduceToVectors(
        geometry=aoi,
        scale=EXPORT_SCALE,
        geometryType="polygon",
        eightConnected=True,
        labelProperty="burn",
        crs="EPSG:4326",
        maxPixels=int(1e10),
    )
    if vectors.size().getInfo() == 0:
        return None, 0.0

    # Area in acres, then keep the biggest polygon (the fire).
    vectors = vectors.map(
        lambda f: f.set("acres", f.geometry().area(maxError=1).divide(4046.8564224))
    )
    largest = ee.Feature(vectors.sort("acres", False).first())
    acres = largest.getNumber("acres").getInfo()

    fc = {
        "type": "FeatureCollection",
        "name": "kula_2023_perimeter",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "name": "2023 Kula/Upcountry burn (dNBR-derived)",
                    "method": f"Sentinel-2 dNBR >= {threshold}, largest contiguous patch",
                    "post_window": f"{POST_START} to {POST_END}",
                    "acres": round(acres, 1),
                },
                "geometry": largest.geometry().getInfo(),
            }
        ],
    }
    return fc, acres


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def main():
    """Compute dNBR over the corridor, export it, and derive the burn perimeter."""
    initialize_ee(EE_PROJECT)
    aoi = load_aoi(AOI_PATH, AOI_FALLBACK_BBOX)

    nbr_pre, n_pre = nbr_composite(aoi, PRE_START, PRE_END)
    nbr_post, n_post = nbr_composite(aoi, POST_START, POST_END)
    logger.info("[SCENES] pre-fire clear=%d, post-fire clear=%d", n_pre, n_post)
    if n_pre == 0 or n_post == 0:
        logger.error("Not enough clear scenes in one of the windows; widen the dates.")
        return

    dnbr = nbr_pre.subtract(nbr_post).rename("dNBR")
    stack = dnbr.addBands(nbr_pre.rename("NBR_pre")).addBands(
        nbr_post.rename("NBR_post")
    )
    task = export_to_drive(stack, EXPORT_NAME, aoi)
    logger.info("[QUEUED] %s task=%s -> Drive/%s", EXPORT_NAME, task.id, DRIVE_FOLDER)

    fc, acres = derive_burn_perimeter(dnbr, aoi, BURN_THRESHOLD, MIN_PATCH_PIXELS)
    if fc is None:
        logger.warning("[PERIM ] no burn patch found above the threshold")
        return
    os.makedirs(os.path.dirname(PERIMETER_LOCAL), exist_ok=True)
    with open(PERIMETER_LOCAL, "w", encoding="utf-8") as handle:
        json.dump(fc, handle)
    logger.info(
        "[PERIM ] largest burn patch = %.1f acres -> %s", acres, PERIMETER_LOCAL
    )
    logger.info(
        "[CHECK ] cross-check this against the known 2023 Upcountry footprint; "
        "the Kula/Olinda fires burned on the order of 1,000 acres"
    )
    logger.info("[DONE  ] dNBR queued to Drive, perimeter saved locally")


if __name__ == "__main__":
    main()
