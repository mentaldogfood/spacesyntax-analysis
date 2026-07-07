from __future__ import annotations

import argparse
import contextlib
import io
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
    get_bia_names,
    list_bias,
)
from depthmapx_segment import (
    DEFAULT_EXE,
    add_derived_metrics,
    build_geodataframe,
    export_results,
    run_depthmapxcli,
)
from citywide_match import (
    CITYWIDE_CSV,
    DEFAULT_LOCAL_WEIGHT,
    DEFAULT_MATCH_TOLERANCE_M,
    attach_citywide,
    blend_scores,
    load_citywide,
)

SPECTRAL = plt.get_cmap("Spectral_r")  # blue=low, red=high (classic syntax palette)

# --all-bias summary columns to rank across BIAs, 1 = best. ascending=False
# means higher is better (rank 1 = highest); segment length flips this,
# since a lower average segment length ranks best.
RANK_COLUMNS = [
    ("avg_blended_nain",            False),
    ("avg_blended_nach",            False),
    ("avg_segment_length",          True),
    ("blended_nain_nach_pearson_r", False),
]


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def compute_summary(gdf: gpd.GeoDataFrame) -> tuple[dict, float, float]:
    """
    Return (summary_dict, r_blend, p_blend).

    r_blend / p_blend are the blended NAIN-NACH Pearson values used for the
    scatter plot title.
    """
    def _finite(col: str):
        v = gdf[col]
        return gdf.loc[v.notna() & np.isfinite(v), col].values

    local_nain = _finite("local_nain")
    local_nach = _finite("local_nach")
    cw_nain    = _finite("citywide_nain")
    cw_nach    = _finite("citywide_nach")
    blend_nain = _finite("blended_nain")
    blend_nach = _finite("blended_nach")
    r_blend, p_blend = pearsonr(blend_nain, blend_nach)

    summary = {
        "avg_local_nain":              round(float(local_nain.mean()), 6),
        "avg_local_nach":               round(float(local_nach.mean()), 6),
        "avg_citywide_nain":            round(float(cw_nain.mean()), 6),
        "avg_citywide_nach":            round(float(cw_nach.mean()), 6),
        "avg_blended_nain":             round(float(blend_nain.mean()), 6),
        "avg_blended_nach":             round(float(blend_nach.mean()), 6),
        "avg_segment_length":           round(float(gdf["Segment Length"].mean()), 4),
        "blended_nain_nach_pearson_r":  round(r_blend, 6),
        "blended_nain_nach_p_value":    f"{p_blend:.4e}",
        "pct_citywide_matched":         round(float(gdf["citywide_matched"].mean()) * 100, 2),
    }
    return summary, r_blend, p_blend


def render_correlation_png(
    gdf:      gpd.GeoDataFrame,
    out_path: Path,
    r:        float,
    p:        float,
) -> None:
    """Scatter plot of blended NAIN vs blended NACH with regression line."""
    mask = (
        gdf["blended_nain"].notna() & gdf["blended_nach"].notna()
        & np.isfinite(gdf["blended_nain"]) & np.isfinite(gdf["blended_nach"])
    )
    nain = gdf.loc[mask, "blended_nain"].values
    nach = gdf.loc[mask, "blended_nach"].values

    m, b  = np.polyfit(nain, nach, 1)
    x_fit = np.linspace(nain.min(), nain.max(), 300)

    fig, ax = plt.subplots(figsize=(7, 6), facecolor="white")
    ax.scatter(nain, nach, s=4, alpha=0.35, color="steelblue", linewidths=0)
    ax.plot(x_fit, m * x_fit + b, color="crimson", linewidth=1.5)
    ax.set_xlabel("NAIN (blended)", fontsize=10)
    ax.set_ylabel("NACH (blended)", fontsize=10)
    ax.set_title(
        f"NAIN vs NACH, blended  (r = {r:.3f},  p = {p:.2e},  n = {mask.sum():,})",
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
    g.add_argument("--all-bias",  action="store_true",
                   help="Run the full pipeline for every BIA in the dataset")
    g.add_argument("--list-bias", action="store_true",
                   help="Print available BIA names and exit")
    p.add_argument("--depthmapxcli", type=Path, default=DEFAULT_EXE,
                   help="Path to depthmapXcli_win64.exe")
    p.add_argument("--buffer", type=float, default=BIA_BUFFER_M,
                   help="Buffer distance around BIA boundary (metres)")
    p.add_argument("--output-dir", type=Path, default=None,
                   help="Override output directory (single-BIA mode only)")
    p.add_argument("--citywide-csv", type=Path, default=CITYWIDE_CSV,
                   help="Path to the citywide segment CSV used for blending")
    p.add_argument("--local-weight", type=float, default=DEFAULT_LOCAL_WEIGHT,
                   help="Weight given to the local score vs. citywide (0-1)")
    p.add_argument("--citywide-tolerance", type=float, default=DEFAULT_MATCH_TOLERANCE_M,
                   help="Max distance (m) to match a local segment to its citywide counterpart")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Per-BIA pipeline
# ---------------------------------------------------------------------------

def _run_one(
    bia_name:     str,
    args:         argparse.Namespace,
    citywide_gdf: gpd.GeoDataFrame,
    quiet:        bool = False,
) -> tuple[dict, gpd.GeoDataFrame] | tuple[None, None]:
    """
    Run the full pipeline for a single BIA.
    Returns (summary_dict, scored_gdf) on success, (None, None) on failure.
    scored_gdf carries a "bia" column for merging across BIAs.
    When quiet=True all stdout from sub-steps is suppressed.
    """
    slug       = bia_name.strip().replace(" ", "_").replace("/", "-")
    output_dir = args.output_dir or (OUTPUT_ROOT / slug)
    exe        = args.depthmapxcli

    _sink = io.StringIO() if quiet else sys.stdout
    try:
        with contextlib.redirect_stdout(_sink):
            dxf, _, bia_gdf = extract_bia(bia_name, output_dir, buffer_m=args.buffer)

            out_graph = output_dir / f"{slug}_segment_map.graph"
            result_df = run_depthmapxcli(dxf, exe, label=slug, out_graph=out_graph)
            print(f"  {len(result_df):,} segments\n")

            print("\nComputing derived metrics...")
            result_df = add_derived_metrics(result_df)

            print("Building GeoDataFrame...")
            gdf = build_geodataframe(result_df, TORONTO_CRS)

            print("Matching against citywide segments...")
            gdf = attach_citywide(gdf, citywide_gdf, tolerance=args.citywide_tolerance)
            gdf = blend_scores(gdf, local_weight=args.local_weight)

            print("\nComputing summary stats...")
            summary, r, p = compute_summary(gdf)
            print(f"  avg NAIN = {summary['avg_blended_nain']:.4f}  |  "
                  f"avg NACH = {summary['avg_blended_nach']:.4f}  |  "
                  f"avg length = {summary['avg_segment_length']:.1f} m  |  "
                  f"r(NAIN,NACH) = {r:.4f}  |  "
                  f"citywide match = {summary['pct_citywide_matched']:.1f}%")

            print("\nExporting results...")
            export_results(result_df, gdf, output_dir, slug,
                           graph_path=out_graph, summary=summary)

            print("\nRendering maps...")
            render_correlation_png(gdf, output_dir / f"{slug}_correlation.png", r, p)

            gdf = gdf.copy()
            gdf.insert(0, "bia", bia_name)

        return summary, gdf

    except Exception as e:
        print(f"  FAILED [{bia_name}]: {e}", file=sys.stderr)
        return None, None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    if args.list_bias:
        list_bias()
        sys.exit(0)

    exe = args.depthmapxcli
    if not exe.exists():
        sys.exit(
            f"ERROR: depthmapXcli not found:\n  {exe}\n"
            "Pass --depthmapxcli <path> to override."
        )

    if not args.citywide_csv.exists():
        sys.exit(f"ERROR: citywide segment CSV not found:\n  {args.citywide_csv}")
    print(f"Loading citywide segment dataset ({args.citywide_csv.name})...")
    citywide_gdf = load_citywide(args.citywide_csv)
    print(f"  {len(citywide_gdf):,} citywide segments loaded\n")

    if args.all_bias:
        names = get_bia_names()
        print(f"Running pipeline for all {len(names)} BIAs...\n")
        rows, gdfs, failed = [], [], []
        w = len(str(len(names)))
        for i, name in enumerate(names, 1):
            print(f"[{i:>{w}}/{len(names)}]  {name}...", end="", flush=True)
            summary, gdf = _run_one(name, args, citywide_gdf, quiet=True)
            if summary is not None:
                print(f"  ok  "
                      f"(NAIN={summary['avg_blended_nain']:.4f}, "
                      f"NACH={summary['avg_blended_nach']:.4f}, "
                      f"r={summary['blended_nain_nach_pearson_r']:.4f}, "
                      f"match={summary['pct_citywide_matched']:.0f}%)")
                rows.append({"bia": name, **summary})
                gdfs.append(gdf)
            else:
                print("  FAILED", flush=True)
                failed.append(name)

        if rows:
            summary_df = pd.DataFrame(rows)
            for col, ascending in RANK_COLUMNS:
                summary_df[f"{col}_rank"] = summary_df[col].rank(ascending=ascending, method="min").astype(int)

            summary_csv = OUTPUT_ROOT / "all_bia_summary.csv"
            summary_df.to_csv(summary_csv, index=False)
            print(f"\nSummary written to: {summary_csv}")

        if gdfs:
            merged = gpd.GeoDataFrame(pd.concat(gdfs, ignore_index=True), crs=gdfs[0].crs)
            merged_gpkg = OUTPUT_ROOT / "all_bia_segment_scores.gpkg"
            if merged_gpkg.exists():
                try:
                    merged_gpkg.unlink()
                except PermissionError:
                    pass
            try:
                merged.to_file(merged_gpkg, driver="GPKG")
                print(f"Merged scored layer written to: {merged_gpkg}  ({len(merged):,} rows)")
            except Exception as exc:
                print(f"Merged GPKG export failed ({exc}); skipping")

        print(f"\n{'='*60}")
        print(f"  Done.  {len(rows)} succeeded, {len(failed)} failed.")
        if failed:
            print("  Failed BIAs:")
            for n in failed:
                print(f"    - {n}")
        sys.exit(0 if not failed else 1)

    if not args.bia:
        sys.exit("ERROR: --bia or --all-bias is required.  Use --list-bias to see options.")

    summary, _ = _run_one(args.bia, args, citywide_gdf, quiet=False)
    if summary is None:
        sys.exit(1)
    print("\nAll done.")


if __name__ == "__main__":
    main()