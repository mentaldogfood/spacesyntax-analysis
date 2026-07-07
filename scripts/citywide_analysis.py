"""
citywide_analysis.py — city-wide segment analysis with BIA attribution.

Pipeline
--------
1. Read inputs/citywide_Segment_Map.csv
2. Compute global summary stats (NAIN-NACH correlation, averages)
3. Build GeoDataFrame from x1,y1,x2,y2 coordinates
4. Spatial join against 800 m BIA buffers → area_name column;
   segments in multiple BIAs are duplicated (one row per BIA match)
5. Export outputs/!city-wide/citywide_segment_scores.csv
6. Render NAIN map, NACH map, correlation scatter PNG

Usage:
    python scripts/citywide_analysis.py
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

import geopandas as gpd
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.cm import ScalarMappable
from matplotlib.collections import LineCollection
from shapely.geometry import LineString

try:
    from scipy.stats import pearsonr
except ImportError:
    def pearsonr(x, y):  # type: ignore[misc]
        r = float(np.corrcoef(x, y)[0, 1])
        n = len(x)
        t = r * np.sqrt((n - 2) / (1 - r ** 2))
        from math import lgamma
        def _ibeta(a, b, x):
            if x <= 0: return 0.0
            if x >= 1: return 1.0
            lbeta = lgamma(a) + lgamma(b) - lgamma(a + b)
            qab, qap, qam = a + b, a + 1.0, a - 1.0
            c, d = 1.0, 1.0 - qab * x / qap
            d = 1e-30 if abs(d) < 1e-30 else d
            d = 1.0 / d; h = d
            for m in range(1, 300):
                m2 = 2 * m
                aa = m * (b - m) * x / ((qam + m2) * (a + m2))
                d, c = 1.0 + aa * d, 1.0 + aa / c
                d = 1e-30 if abs(d) < 1e-30 else d
                c = 1e-30 if abs(c) < 1e-30 else c
                d = 1.0 / d; h *= d * c
                aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
                d, c = 1.0 + aa * d, 1.0 + aa / c
                d = 1e-30 if abs(d) < 1e-30 else d
                c = 1e-30 if abs(c) < 1e-30 else c
                d = 1.0 / d; delta = d * c; h *= delta
                if abs(delta - 1.0) < 1e-12: break
            return np.exp(a * np.log(x) + b * np.log(1.0 - x) - lbeta) * h / a
        df2 = n - 2
        p = float(2.0 * _ibeta(df2 / 2.0, 0.5, df2 / (df2 + t * t)))
        return r, p


BASE       = Path(__file__).parent.parent
INPUT_CSV  = BASE / "inputs" / "citywide_Segment_Map.csv"
BIAS_FILE  = BASE / "inputs" / "all bia boundaries.geojson"
OUTPUT_DIR = BASE / "outputs" / "!city-wide"
CRS        = "EPSG:26917"
BIA_BUFFER_M = 800.0

NAIN_COL = "NAIN"
NACH_COL = "NACH"
LEN_COL  = "Segment Length"
SPECTRAL = plt.get_cmap("Spectral_r")


# ---------------------------------------------------------------------------
# Global summary stats
# ---------------------------------------------------------------------------

def compute_summary(df: pd.DataFrame) -> dict:
    def _clean(col):
        v = df[col].astype(float)
        return v[v.notna() & np.isfinite(v)].values

    nain_raw = _clean(NAIN_COL);  nach_raw = _clean(NACH_COL)
    r_raw, p_raw = pearsonr(nain_raw, nach_raw)

    return {
        "n_segments":                   len(df),
        "avg_NAIN_raw":                 round(float(nain_raw.mean()), 6),
        "avg_NACH_raw":                 round(float(nach_raw.mean()), 6),
        "avg_segment_length":           round(float(df[LEN_COL].mean()), 4),
        "nain_nach_pearson_r_raw":      round(r_raw, 6),
        "nain_nach_p_value_raw":        f"{p_raw:.4e}",
    }


# ---------------------------------------------------------------------------
# GeoDataFrame + BIA join
# ---------------------------------------------------------------------------

def build_geodataframe(df: pd.DataFrame) -> gpd.GeoDataFrame:
    geometries = [
        LineString([(row.x1, row.y1), (row.x2, row.y2)])
        for row in df[["x1", "y1", "x2", "y2"]].itertuples()
    ]
    drop = {"x1", "y1", "x2", "y2", "Axial Line Ref"}
    attr_cols = [c for c in df.columns if c not in drop]
    return gpd.GeoDataFrame(df[attr_cols], geometry=geometries, crs=CRS)


def join_bia_names(
    gdf:      gpd.GeoDataFrame,
    buffer_m: float = BIA_BUFFER_M,
) -> gpd.GeoDataFrame:
    """
    Spatial join segments against buffered BIA polygons.
    Returns a GeoDataFrame with an added 'area_name' column.
    Segments that intersect multiple BIA buffers are duplicated — one row
    per BIA match. Segments outside all buffers are dropped.
    """
    print(f"  Loading BIA boundaries and buffering by {buffer_m:.0f} m...")
    bias = gpd.read_file(BIAS_FILE).to_crs(CRS)
    bia_buffers = gpd.GeoDataFrame(
        {"area_name": bias["AREA_NAME"]},
        geometry=bias.geometry.buffer(buffer_m),
        crs=CRS,
    )

    print(f"  Joining {len(gdf):,} segments against {len(bia_buffers)} BIA buffers...")
    joined = gpd.sjoin(gdf, bia_buffers, how="inner", predicate="intersects")
    joined = joined.drop(columns=["index_right"])
    print(f"  {len(joined):,} segment-BIA rows ({len(joined['area_name'].unique())} BIAs matched)")
    return joined


# ---------------------------------------------------------------------------
# PNG maps
# ---------------------------------------------------------------------------

def render_map_png(
    gdf:      gpd.GeoDataFrame,
    col:      str,
    title:    str,
    out_path: Path,
) -> None:
    vals   = gdf[col].values.astype(float)
    finite = vals[np.isfinite(vals)]
    if len(finite) == 0:
        print(f"  SKIP '{col}': no finite values"); return

    vmin = np.percentile(finite, 2)
    vmax = np.percentile(finite, 98)
    norm = mcolors.Normalize(vmin=vmin, vmax=vmax)

    segs, colours = [], []
    for geom, val in zip(gdf.geometry, vals):
        if geom is None or geom.is_empty: continue
        coords = list(geom.coords)
        for i in range(len(coords) - 1):
            segs.append([coords[i], coords[i + 1]])
            colours.append(SPECTRAL(norm(val)) if np.isfinite(val) else (0.3, 0.3, 0.3, 0.4))

    fig, ax = plt.subplots(figsize=(16, 14), facecolor="white")
    ax.set_aspect("equal"); ax.axis("off")
    ax.add_collection(
        LineCollection(segs, colors=colours, linewidths=0.4, zorder=2, rasterized=True)
    )
    sm = ScalarMappable(norm=norm, cmap=SPECTRAL); sm.set_array([])
    cb = fig.colorbar(sm, ax=ax, fraction=0.02, pad=0.01, shrink=0.5)
    cb.set_label(col, fontsize=9); cb.ax.tick_params(labelsize=8)
    ax.set_title(title, fontsize=13, fontweight="bold", pad=12)
    ax.autoscale_view()
    fig.savefig(out_path, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  PNG: {out_path.name}")


def render_correlation_png(df: pd.DataFrame, out_path: Path, summary: dict) -> None:
    mask = df[NAIN_COL].notna() & df[NACH_COL].notna() \
         & np.isfinite(df[NAIN_COL]) & np.isfinite(df[NACH_COL])
    nain = df.loc[mask, NAIN_COL].values
    nach = df.loc[mask, NACH_COL].values
    m, b  = np.polyfit(nain, nach, 1)
    x_fit = np.linspace(nain.min(), nain.max(), 300)
    r = summary["nain_nach_pearson_r_raw"]
    p = float(summary["nain_nach_p_value_raw"])

    fig, ax = plt.subplots(figsize=(7, 6), facecolor="white")
    ax.scatter(nain, nach, s=2, alpha=0.2, color="steelblue", linewidths=0, rasterized=True)
    ax.plot(x_fit, m * x_fit + b, color="crimson", linewidth=1.5)
    ax.set_xlabel("NAIN", fontsize=10); ax.set_ylabel("NACH", fontsize=10)
    ax.set_title(
        f"Citywide NAIN vs NACH  (r = {r:.3f},  p = {p:.2e},  n = {mask.sum():,})",
        fontsize=10,
    )
    fig.tight_layout()
    fig.savefig(out_path, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  PNG: {out_path.name}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Citywide BIA attribution + NAIN/NACH correlation",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--input",    type=Path, default=INPUT_CSV)
    p.add_argument("--buffer",   type=float, default=BIA_BUFFER_M,
                   help="BIA buffer radius in metres")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if not args.input.exists():
        sys.exit(f"ERROR: input not found:\n  {args.input}")

    # -- 1. Load ---------------------------------------------------------------
    print(f"Reading {args.input.name}...")
    df = pd.read_csv(args.input)
    df.columns = df.columns.str.strip()
    print(f"  {len(df):,} segments")

    for col in (NAIN_COL, NACH_COL, LEN_COL, "x1", "y1", "x2", "y2"):
        if col not in df.columns:
            sys.exit(f"ERROR: expected column '{col}' not found.\n"
                     f"Available: {list(df.columns)}")

    # -- 2. Global stats -------------------------------------------------------
    print("Computing summary stats...")
    summary = compute_summary(df)
    print("\n--- Summary (citywide) ---")
    for k, v in summary.items():
        print(f"  {k:<36} {v}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # -- 3. Build GeoDataFrame -------------------------------------------------
    print("\nBuilding GeoDataFrame...")
    gdf = build_geodataframe(df)

    # -- 4. BIA spatial join ---------------------------------------------------
    print("\nJoining BIA names...")
    joined = join_bia_names(gdf, buffer_m=args.buffer)

    # -- 5. Export CSV ---------------------------------------------------------
    csv_path = OUTPUT_DIR / "citywide_segment_scores.csv"
    # Drop geometry before writing; keep all attribute columns + area_name
    out_df = pd.DataFrame(joined.drop(columns=["geometry"]))
    # Move area_name to second column (after Ref) for readability
    cols = out_df.columns.tolist()
    cols.remove("area_name")
    ref_pos = cols.index("Ref") + 1 if "Ref" in cols else 0
    cols.insert(ref_pos, "area_name")
    out_df = out_df[cols]

    try:
        out_df.to_csv(csv_path, index_label="row_id")
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            f.write("\nmetric,value\n")
            for k, v in summary.items():
                f.write(f"{k},{v}\n")
        print(f"\n  CSV: {csv_path}  ({len(out_df):,} rows)")
    except PermissionError:
        print(f"\n  CSV skipped — file is open elsewhere")

    # -- 6. PNG maps (rendered from full city GDF, before BIA join) ------------
    print("\nRendering maps...")
    render_map_png(gdf, NAIN_COL, "Citywide NAIN", OUTPUT_DIR / "citywide_nain.png")
    render_map_png(gdf, NACH_COL, "Citywide NACH", OUTPUT_DIR / "citywide_nach.png")
    render_correlation_png(df, OUTPUT_DIR / "citywide_correlation.png", summary)

    print("\nDone.")


if __name__ == "__main__":
    main()
