"""
citywide_match.py — match local BIA segments to their citywide counterparts
and blend the two NAIN/NACH scores.

Local (per-BIA) and citywide segment analyses are two independent depthmapX
runs with no shared segment ID, so segments are matched by geometry: each
local segment's midpoint is joined to the nearest citywide segment midpoint
within a distance tolerance.

Usage (importable only):
    from citywide_match import load_citywide, attach_citywide, blend_scores
    citywide_gdf = load_citywide()
    gdf = attach_citywide(gdf, citywide_gdf)
    gdf = blend_scores(gdf)
"""
from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString

BASE         = Path(__file__).parent.parent
CITYWIDE_CSV = BASE / "inputs" / "citywide_Segment_Map.csv"
TORONTO_CRS  = "EPSG:26917"

DEFAULT_MATCH_TOLERANCE_M = 15.0
DEFAULT_LOCAL_WEIGHT      = 0.7


def load_citywide(csv_path: Path = CITYWIDE_CSV, crs: str = TORONTO_CRS) -> gpd.GeoDataFrame:
    """Load the citywide segment dataset as a GeoDataFrame of NAIN/NACH by geometry."""
    df = pd.read_csv(csv_path)
    df.columns = df.columns.str.strip()
    geometries = [
        LineString([(row.x1, row.y1), (row.x2, row.y2)])
        for row in df[["x1", "y1", "x2", "y2"]].itertuples()
    ]
    return gpd.GeoDataFrame(
        df[["NAIN", "NACH"]].rename(columns={"NAIN": "citywide_nain", "NACH": "citywide_nach"}),
        geometry=geometries,
        crs=crs,
    )


def attach_citywide(
    gdf:       gpd.GeoDataFrame,
    citywide:  gpd.GeoDataFrame,
    tolerance: float = DEFAULT_MATCH_TOLERANCE_M,
) -> gpd.GeoDataFrame:
    """
    Nearest-neighbour match each local segment (by midpoint) to its closest
    citywide segment (by midpoint), within `tolerance` metres.

    Adds citywide_nain, citywide_nach, citywide_match_distance_m, and a
    citywide_matched flag. Segments with no match within tolerance keep
    citywide_nain/nach as NaN -- blend_scores() falls back to 100% local
    weight for these.
    """
    gdf = gdf.copy()
    local_pts = gpd.GeoDataFrame(
        gdf.drop(columns="geometry"),
        geometry=gdf.geometry.interpolate(0.5, normalized=True),
        crs=gdf.crs,
    )
    cw_pts = gpd.GeoDataFrame(
        citywide.drop(columns="geometry"),
        geometry=citywide.geometry.interpolate(0.5, normalized=True),
        crs=citywide.crs,
    )

    joined = gpd.sjoin_nearest(
        local_pts, cw_pts,
        how="left", max_distance=tolerance,
        distance_col="citywide_match_distance_m",
    )
    joined = joined[~joined.index.duplicated(keep="first")]  # drop exact-distance ties

    gdf = gdf.join(joined[["citywide_nain", "citywide_nach", "citywide_match_distance_m"]])
    gdf["citywide_matched"] = gdf["citywide_match_distance_m"].notna()
    return gdf


def blend_scores(
    gdf:          gpd.GeoDataFrame,
    local_weight: float = DEFAULT_LOCAL_WEIGHT,
) -> gpd.GeoDataFrame:
    """
    Rename raw local nain/nach to local_nain/local_nach, then add
    blended_nain/blended_nach = local_weight * local + (1 - local_weight) * citywide.

    Segments with no citywide match (citywide_matched=False) fall back to
    100% local weight.
    """
    gdf = gdf.rename(columns={"nain": "local_nain", "nach": "local_nach"})
    cw_weight = 1.0 - local_weight

    has_cw = gdf["citywide_matched"]
    gdf["blended_nain"] = gdf["local_nain"]
    gdf["blended_nach"] = gdf["local_nach"]
    gdf.loc[has_cw, "blended_nain"] = (
        local_weight * gdf.loc[has_cw, "local_nain"] + cw_weight * gdf.loc[has_cw, "citywide_nain"]
    )
    gdf.loc[has_cw, "blended_nach"] = (
        local_weight * gdf.loc[has_cw, "local_nach"] + cw_weight * gdf.loc[has_cw, "citywide_nach"]
    )
    return gdf
