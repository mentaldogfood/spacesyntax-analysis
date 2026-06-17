"""
main.py — full Space Syntax pipeline for Toronto BIAs.

Orchestrates:
  1. extract_bia        : clip road centrelines, export DXF
  2. depthmapx_segment  : run depthmapXcli angular segment analysis,
                          compute derived metrics, export .graph / MIF / CSV
  3. Summary stats      : avg NAIN, avg NACH, avg segment length, NAIN-NACH Pearson r
                          appended as a summary section in segment_scores.csv
  4. PNG maps           : NAIN spectral map, NACH spectral map, NAIN-NACH scatter plot

Usage:
    python scripts/main.py --bia "Downtown Yonge"
    python scripts/main.py --list-bias
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.cm import ScalarMappable
from matplotlib.collections import LineCollection
try:
    from scipy.stats import pearsonr
except ImportError:
    def pearsonr(x, y):  # type: ignore[misc]
        """numpy-only fallback — used when scipy is not installed."""
        r = float(np.corrcoef(x, y)[0, 1])
        n = len(x)
        t = r * np.sqrt((n - 2) / (1 - r ** 2))
        # two-tailed p-value via regularised incomplete beta
        from math import lgamma
        def _ibeta(a, b, x):
            if x <= 0:
                return 0.0
            if x >= 1:
                return 1.0
            lbeta = lgamma(a) + lgamma(b) - lgamma(a + b)
            # Lentz continued-fraction for I_x(a, b)
            qab = a + b
            qap = a + 1.0
            qam = a - 1.0
            c, d = 1.0, 1.0 - qab * x / qap
            if abs(d) < 1e-30:
                d = 1e-30
            d = 1.0 / d
            h = d
            for m in range(1, 300):
                m2 = 2 * m
                aa = m * (b - m) * x / ((qam + m2) * (a + m2))
                d = 1.0 + aa * d
                c = 1.0 + aa / c
                if abs(d) < 1e-30:
                    d = 1e-30
                if abs(c) < 1e-30:
                    c = 1e-30
                d = 1.0 / d
                h *= d * c
                aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
                d = 1.0 + aa * d
                c = 1.0 + aa / c
                if abs(d) < 1e-30:
                    d = 1e-30
                if abs(c) < 1e-30:
                    c = 1e-30
                d = 1.0 / d
                delta = d * c
                h *= delta
                if abs(delta - 1.0) < 1e-12:
                    break
            return np.exp(a * np.log(x) + b * np.log(1.0 - x) - lbeta) * h / a
        df2 = n - 2
        p = float(2.0 * _ibeta(df2 / 2.0, 0.5, df2 / (df2 + t * t)))
        return r, p

from extract_bia import (
    OUTPUT_ROOT,
    TORONTO_CRS,
    BIA_BUFFER_M,
    extract_bia,
    list_bias,
)
from depthmapx_segment import (
    DEFAULT_EXE,
    add_derived_metrics,
    apply_length_penalty,
    build_geodataframe,
    export_results,
    run_depthmapxcli,
)

SPECTRAL = plt.get_cmap("Spectral_r")  # blue=low, red=high (classic syntax palette)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def compute_summary(gdf: gpd.GeoDataFrame) -> tuple[dict, float, float]:
    """
    Return (summary_dict, r_raw, p_raw).

    summary_dict contains parallel raw and adjusted stat blocks, ready to
    pass to export_results. r_raw / p_raw are the raw NAIN-NACH Pearson
    values used for the scatter plot title.
    """
    def _finite(col: str):
        v = gdf[col]
        return gdf.loc[v.notna() & np.isfinite(v), col].values

    nain_raw = _finite("nain")
    nach_raw = _finite("nach")
    r_raw, p_raw = pearsonr(nain_raw, nach_raw)

    nain_adj = _finite("NAIN_adjusted")
    nach_adj = _finite("NACH_adjusted")
    r_adj, p_adj = pearsonr(nain_adj, nach_adj)

    summary = {
        # --- raw ---
        "avg_nain_raw":                 round(float(nain_raw.mean()), 6),
        "avg_nach_raw":                 round(float(nach_raw.mean()), 6),
        "avg_segment_length":           round(float(gdf["Segment Length"].mean()), 4),
        "nain_nach_pearson_r_raw":      round(r_raw, 6),
        "nain_nach_p_value_raw":        f"{p_raw:.4e}",
        # --- length-penalty adjusted ---
        "avg_nain_adjusted":            round(float(nain_adj.mean()), 6),
        "avg_nach_adjusted":            round(float(nach_adj.mean()), 6),
        "nain_nach_pearson_r_adjusted": round(r_adj, 6),
        "nain_nach_p_value_adjusted":   f"{p_adj:.4e}",
    }
    return summary, r_raw, p_raw


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_map_png(
    gdf:      gpd.GeoDataFrame,
    bia:      gpd.GeoDataFrame,
    col:      str,
    title:    str,
    out_path: Path,
) -> None:
    """Spectral line map for a single metric, saved as PNG."""
    vals   = gdf[col].values.astype(float)
    finite = vals[np.isfinite(vals)]
    if len(finite) == 0:
        print(f"  SKIP '{col}': no finite values")
        return

    vmin = np.percentile(finite, 2)
    vmax = np.percentile(finite, 98)
    norm = mcolors.Normalize(vmin=vmin, vmax=vmax)

    segs, colours = [], []
    for geom, val in zip(gdf.geometry, vals):
        if geom is None or geom.is_empty:
            continue
        coords = list(geom.coords)
        for i in range(len(coords) - 1):
            segs.append([coords[i], coords[i + 1]])
            c = SPECTRAL(norm(val)) if np.isfinite(val) else (0.3, 0.3, 0.3, 0.4)
            colours.append(c)

    fig, ax = plt.subplots(figsize=(10, 10), facecolor="white")
    ax.set_aspect("equal")
    ax.axis("off")
    ax.add_collection(LineCollection(segs, colors=colours, linewidths=0.8, zorder=2))
    bia.to_crs(gdf.crs).boundary.plot(
        ax=ax, color="crimson", linewidth=1.0, linestyle="--", zorder=5
    )
    sm = ScalarMappable(norm=norm, cmap=SPECTRAL)
    sm.set_array([])
    cb = fig.colorbar(sm, ax=ax, fraction=0.03, pad=0.02, shrink=0.6)
    cb.set_label(col, fontsize=8)
    cb.ax.tick_params(labelsize=7)
    ax.set_title(title, fontsize=11, fontweight="bold", pad=10)
    ax.autoscale_view()
    fig.savefig(out_path, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  PNG: {out_path.name}")


def render_correlation_png(
    gdf:      gpd.GeoDataFrame,
    out_path: Path,
    r:        float,
    p:        float,
) -> None:
    """Scatter plot of NAIN vs NACH with regression line."""
    mask = (
        gdf["nain"].notna() & gdf["nach"].notna()
        & np.isfinite(gdf["nain"]) & np.isfinite(gdf["nach"])
    )
    nain = gdf.loc[mask, "nain"].values
    nach = gdf.loc[mask, "nach"].values

    m, b  = np.polyfit(nain, nach, 1)
    x_fit = np.linspace(nain.min(), nain.max(), 300)

    fig, ax = plt.subplots(figsize=(7, 6), facecolor="white")
    ax.scatter(nain, nach, s=4, alpha=0.35, color="steelblue", linewidths=0)
    ax.plot(x_fit, m * x_fit + b, color="crimson", linewidth=1.5)
    ax.set_xlabel("NAIN", fontsize=10)
    ax.set_ylabel("NACH", fontsize=10)
    ax.set_title(
        f"NAIN vs NACH  (r = {r:.3f},  p = {p:.2e},  n = {mask.sum():,})",
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
        description="Space Syntax pipeline — extract BIA, run segment analysis, render maps",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--bia",       help="BIA name, e.g. 'Downtown Yonge'")
    g.add_argument("--list-bias", action="store_true", help="Print available BIA names and exit")
    p.add_argument("--depthmapxcli", type=Path, default=DEFAULT_EXE,
                   help="Path to depthmapXcli_win64.exe")
    p.add_argument("--buffer", type=float, default=BIA_BUFFER_M,
                   help="Buffer distance around BIA boundary (metres)")
    p.add_argument("--output-dir", type=Path, default=None,
                   help="Override output directory")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    if args.list_bias:
        list_bias()
        sys.exit(0)

    if not args.bia:
        sys.exit("ERROR: --bia is required.  Use --list-bias to see options.")

    bia_name   = args.bia
    slug       = bia_name.strip().replace(" ", "_").replace("/", "-")
    output_dir = args.output_dir or (OUTPUT_ROOT / slug)
    exe        = args.depthmapxcli

    if not exe.exists():
        sys.exit(
            f"ERROR: depthmapXcli not found:\n  {exe}\n"
            "Pass --depthmapxcli <path> to override."
        )

    # -- Step 1: Extract -------------------------------------------------------
    dxf, _, bia_gdf = extract_bia(bia_name, output_dir, buffer_m=args.buffer)

    # -- Step 2: Segment analysis ----------------------------------------------
    out_graph = output_dir / f"{slug}_segment_map.graph"
    try:
        result_df = run_depthmapxcli(dxf, exe, label=slug, out_graph=out_graph)
    except RuntimeError as e:
        sys.exit(f"\nFAILED: {e}")

    print(f"  {len(result_df):,} segments\n")

    print("\nComputing derived metrics...")
    result_df = add_derived_metrics(result_df)

    print("Applying length penalty...")
    result_df = apply_length_penalty(result_df)

    print("Building GeoDataFrame...")
    gdf = build_geodataframe(result_df, TORONTO_CRS)

    # -- Step 3: Summary stats (computed before export so they go into the CSV) -
    print("\nComputing summary stats...")
    summary, r, p = compute_summary(gdf)
    print(f"  avg NAIN = {summary['avg_nain_raw']:.4f}  |  "
          f"avg NACH = {summary['avg_nach_raw']:.4f}  |  "
          f"avg length = {summary['avg_segment_length']:.1f} m  |  "
          f"r(NAIN,NACH) = {r:.4f}")

    # -- Step 4: Export --------------------------------------------------------
    print("\nExporting results...")
    export_results(result_df, gdf, output_dir, slug,
                   graph_path=out_graph, summary=summary)

    # -- Step 5: PNG maps ------------------------------------------------------
    print("\nRendering maps...")
    render_map_png(gdf, bia_gdf, "nain", "NAIN", output_dir / f"{slug}_nain.png")
    render_map_png(gdf, bia_gdf, "nach", "NACH", output_dir / f"{slug}_nach.png")
    render_correlation_png(gdf, output_dir / f"{slug}_correlation.png", r, p)

    print("\nAll done.")


if __name__ == "__main__":
    main()