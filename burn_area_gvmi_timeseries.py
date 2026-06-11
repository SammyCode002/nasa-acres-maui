"""
burn_area_gvmi_timeseries.py

NASA ACRES Maui - mean GVMI inside the 2023 Kula burn area vs an unburned
control, month by month.

This is the validation chart for Ana. If our vegetation-moisture maps are real,
then inside the area that burned in August 2023 the moisture signal should drop
relative to unburned land right at the fire (vegetation gone, charred ground),
then climb back as grass regrows. Seeing that gap open and close is direct
evidence the GVMI maps track real surface conditions.

We plot two lines: mean GVMI inside the burn, and mean GVMI in an unburned
control ring just outside the burn (same elevation and vegetation). Both cycle
with the dry season, so the ring is the baseline. The fire signal is the burn
line dropping below the ring right after August 2023, then climbing back as
grass regrows. That gap is what isolates the fire from the seasonal cycle, and
the ring (not the whole corridor) keeps the comparison at the same elevation.

It reads the burn polygon from data/burn_perimeter/kula_2023_perimeter.geojson
(made by dnbr_burnscar.py) and the corridor AOI from aoi/corridor.geojson, then
computes the monthly means straight from Earth Engine. WHY from Earth Engine and
not the exported tiles: the monthly tiles live in Google Drive, and computing
the means server-side from the same Sentinel-2 data gives identical numbers
without downloading 35 files by hand.

Output: data/figures/burn_area_gvmi_timeseries.pdf (and a .png)

Author : Samuel Dameg (SammyCode002)
Project: NASA ACRES Maui  (https://github.com/SammyCode002/nasa-acres-maui)

Run:
    uv run --with earthengine-api --with matplotlib python burn_area_gvmi_timeseries.py
"""

import functools
import json
import logging
import math
import os
import time
from datetime import date

import ee
import matplotlib

matplotlib.use("Agg")  # render to a file, no display needed
import matplotlib.pyplot as plt  # noqa: E402  (must follow the backend choice)
from matplotlib.dates import DateFormatter  # noqa: E402

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

EE_PROJECT = "ace-shine-392702"
AOI_PATH = "aoi/corridor.geojson"
BURN_PATH = "data/burn_perimeter/kula_2023_perimeter.geojson"

START_YEAR, START_MONTH = 2022, 1  # start a year+ before the fire for a baseline
_today = date.today()
END_YEAR, END_MONTH = _today.year, _today.month

COLLECTION_ID = "COPERNICUS/S2_SR_HARMONIZED"
MAX_CLOUD_PCT = 60
REDUCE_SCALE = 20  # meters
OUTPUT_PDF = "data/figures/burn_area_gvmi_timeseries.pdf"
FIRE_DATE = date(2023, 8, 8)  # ignition, marked on the chart

# --------------------------------------------------------------------------- #
# 4x4 debug logging
# --------------------------------------------------------------------------- #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("burn_area_gvmi_timeseries")


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
    return ee.Number(1).getInfo()


def _first_geometry(path, role=None):
    """Read a GeoJSON file and return one geometry dict.

    If role is given, prefer the feature tagged properties.role == role; else
    take the first feature. Works for the AOI (role 'aoi') and the single-feature
    burn perimeter.
    """
    with open(path, "r", encoding="utf-8") as handle:
        gj = json.load(handle)
    feats = gj.get("features", [])
    if role is not None:
        feat = next(
            (f for f in feats if f.get("properties", {}).get("role") == role),
            feats[0],
        )
    else:
        feat = feats[0]
    return feat["geometry"]


@debug_log
def load_geometries():
    """Load the corridor AOI, the burn polygon, and the unburned control.

    WHY a ring control (100 m to 1500 m outside the burn): the burn sits
    upcountry where the land is greener, and the lower corridor is much drier, so
    using the whole corridor as the baseline is apples-to-oranges. A ring hugs
    the same elevation and vegetation, so the only real difference is the fire.
    """
    aoi = ee.Geometry(_first_geometry(AOI_PATH, role="aoi"))
    burn = ee.Geometry(_first_geometry(BURN_PATH))
    control = burn.buffer(1500).difference(burn.buffer(100), maxError=1)
    return aoi, burn, control


def mask_s2_clouds(image):
    """Mask cloud / shadow / cirrus / defective pixels via the SCL band."""
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


def add_gvmi(image):
    """Add the Global Vegetation Moisture Index band.

    WHY divide B8/B11 by 10000: GVMI's additive constants (0.1, 0.02) assume
    reflectance in 0-1, but Sentinel-2 stores it as integers scaled by 10000.
        GVMI = ((NIR+0.1) - (SWIR+0.02)) / ((NIR+0.1) + (SWIR+0.02))
    """
    nir = image.select("B8").divide(10000)
    swir = image.select("B11").divide(10000)
    gvmi = image.expression(
        "((NIR + 0.1) - (SWIR + 0.02)) / ((NIR + 0.1) + (SWIR + 0.02))",
        {"NIR": nir, "SWIR": swir},
    ).rename("GVMI")
    return image.addBands(gvmi)


def _month_list(start_year, start_month, end_year, end_month):
    """Yield (year, month) from start to end inclusive. Plain Python."""
    y, m = start_year, start_month
    while (y, m) <= (end_year, end_month):
        yield y, m
        m += 1
        if m > 12:
            m, y = 1, y + 1


@debug_log
def gvmi_timeseries(aoi, burn, control):
    """Monthly mean GVMI inside the burn and in the control, computed server-side.

    WHY build all the months as ee Features then one getInfo: each month is a
    median composite reduced over two areas. Doing the whole list in one round
    trip is far faster than calling the server once per month.

    Returns a list of (date, burn_mean, control_mean); means may be None for a
    fully clouded month.
    """
    coll = (
        ee.ImageCollection(COLLECTION_ID)
        .filterBounds(aoi)
        .filterDate(
            f"{START_YEAR}-{START_MONTH:02d}-01", f"{END_YEAR}-{END_MONTH:02d}-28"
        )
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", MAX_CLOUD_PCT))
        .map(mask_s2_clouds)
        .map(add_gvmi)
    )

    feats = []
    for year, month in _month_list(START_YEAR, START_MONTH, END_YEAR, END_MONTH):
        start = ee.Date.fromYMD(year, month, 1)
        end = start.advance(1, "month")
        composite = coll.filterDate(start, end).select("GVMI").median()

        def mean_over(geom):
            return composite.reduceRegion(
                reducer=ee.Reducer.mean(),
                geometry=geom,
                scale=REDUCE_SCALE,
                maxPixels=int(1e9),
            ).get("GVMI")

        feats.append(
            ee.Feature(
                None,
                {
                    "date": start.format("YYYY-MM-dd"),
                    "burn": mean_over(burn),
                    "control": mean_over(control),
                },
            )
        )

    rows = ee.FeatureCollection(feats).getInfo()["features"]
    out = []
    for r in rows:
        p = r["properties"]
        out.append((date.fromisoformat(p["date"]), p.get("burn"), p.get("control")))
    return out


def _nan(v):
    """None becomes NaN so matplotlib leaves a gap instead of a fake zero."""
    return float("nan") if v is None else v


@debug_log
def plot_timeseries(series, output_pdf):
    """Plot burn vs control mean GVMI over time and save PDF and PNG."""
    dates = [d for d, _, _ in series]
    burn = [_nan(b) for _, b, _ in series]
    control = [_nan(c) for _, _, c in series]

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(
        dates,
        control,
        marker="o",
        ms=3,
        lw=1.6,
        color="#6c757d",
        label="Unburned corridor (control)",
    )
    ax.plot(
        dates,
        burn,
        marker="o",
        ms=3,
        lw=2.2,
        color="#c62828",
        label="Inside 2023 burn area",
    )
    ax.axvline(FIRE_DATE, color="#c62828", linestyle="--", linewidth=1.2)
    finite = [v for v in burn + control if not math.isnan(v)]
    ax.annotate(
        "Aug 2023 Kula fire",
        xy=(FIRE_DATE, max(finite)),
        xytext=(8, -2),
        textcoords="offset points",
        color="#c62828",
        fontsize=9,
        fontweight="bold",
    )
    ax.set_title("GVMI inside the 2023 Kula burn vs unburned corridor", fontsize=13)
    ax.set_ylabel("Mean GVMI (higher = moister)")
    ax.set_xlabel("Month")
    ax.xaxis.set_major_formatter(DateFormatter("%Y-%m"))
    ax.legend(loc="lower right", fontsize=9)
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()

    os.makedirs(os.path.dirname(output_pdf), exist_ok=True)
    fig.savefig(output_pdf)
    png = os.path.splitext(output_pdf)[0] + ".png"
    fig.savefig(png, dpi=150)
    plt.close(fig)
    return output_pdf, png


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def main():
    """Build the burn vs control GVMI time series and save the validation chart."""
    initialize_ee(EE_PROJECT)
    aoi, burn, control = load_geometries()
    series = gvmi_timeseries(aoi, burn, control)

    # Report the biggest gap (burn below control), the fire fingerprint.
    gaps = [(d, c - b) for d, b, c in series if b is not None and c is not None]
    if gaps:
        worst = max(gaps, key=lambda x: x[1])
        logger.info(
            "[SERIES] largest burn-below-control gap: %.3f at %s",
            worst[1],
            worst[0].isoformat(),
        )

    pdf, png = plot_timeseries(series, OUTPUT_PDF)
    logger.info("[DONE  ] saved chart -> %s and %s", pdf, png)


if __name__ == "__main__":
    main()
