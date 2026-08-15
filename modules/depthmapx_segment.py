"""
DXF centerlines -> depthmapXcli -> angular segment analysis

Takes a *_centerlines.dxf produced by extract_bia.py, runs the four-step
depthmapXcli pipeline (IMPORT → MAPCONVERT → SEGMENT → EXPORT), computes
derived metrics, then exports three formats:

  *_depthmap_map.csv   — depthmapX native format (Ref, x1, y1, x2, y2 + all metrics)
  *_segment_map.mif    — MapInfo Interchange Format (line geometry + all metrics)
  *_segment_scores.csv — tabular scores only (no coordinate columns)

Derived metrics:
  nain       = (T1024 Node Count ^ 1.2) / (T1024 Total Depth + 2)
  nach       = log(T1024 Choice + 1) / log(T1024 Total Depth + 3)
  integrated = T1024 Integration * log(T1024 Choice + 2)

Usage:
    python depthmapx_segment.py --bia "Downtown Yonge"
    python depthmapx_segment.py --dxf outputs/Downtown_Yonge/Downtown_Yonge_centerlines.dxf

depthmapXcli pipeline:
  1. IMPORT   -f <dxf> -o <graph> -it drawing
  2. MAPCONVERT -f <graph> -co segment -con "Segment Map"
  3. SEGMENT  -st tulip -sr n -srt angular -sic -stb 1024
  4. EXPORT   -em shapegraph-map-csv -> Ref,x1,y1,x2,y2,T1024 Choice,...
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE        = Path(__file__).parent.parent
OUTPUT_ROOT = BASE / "outputs"
TORONTO_CRS = "EPSG:26917"

_RESEARCH   = BASE.parent

_CLI_CANDIDATES = [
    BASE / "depthmapXcli_win64.exe",
    _RESEARCH / "depthmapXcli_win64.exe",
    Path.home() / "Downloads" / "depthmapXcli_win64.exe",
]
DEFAULT_EXE = next((p for p in _CLI_CANDIDATES if p.exists()), _CLI_CANDIDATES[-1])

# Columns to pull from depthmapXcli output into the GeoDataFrame
_DMX_COLS = [
    "T1024 Choice",
    "T1024 Integration",
    "T1024 Node Count",
    "T1024 Total Depth",
    "Angular Connectivity",
    "Connectivity",
    "Segment Length",
]

# ---------------------------------------------------------------------------
# depthmapXcli invocation
# ---------------------------------------------------------------------------

def run_depthmapxcli(
    dxf_path:  Path,
    exe:       Path,
    label:     str,
    out_graph: Path | None = None,
    timeout:   float = 600,
) -> pd.DataFrame:
    """
    Run the four-step depthmapXcli pipeline on a DXF file:
      1. IMPORT   DXF as drawing map
      2. MAPCONVERT drawing → segment map (splits polylines at intersections)
      3. SEGMENT  Angular Tulip 1024 bins, radius n, include Choice
      4. EXPORT   segment map as CSV (Ref, x1, y1, x2, y2, T1024 metrics)

    If out_graph is given, the analysed .graph is copied there before cleanup.
    Returns a DataFrame with all depthmapXcli output columns.
    """
    print(f"\n  Running depthmapXcli  [{label}]...")
    t0 = time.time()

    with tempfile.TemporaryDirectory() as tmp:
        graph = Path(tmp) / "analysis.graph"
        csv   = Path(tmp) / "result.csv"

        steps = [
            (
                [str(exe), "-m", "IMPORT",
                 "-f", str(dxf_path), "-o", str(graph),
                 "-it", "drawing"],
                "Import",
            ),
            (
                [str(exe), "-m", "MAPCONVERT",
                 "-f", str(graph), "-o", str(graph),
                 "-co", "segment", "-con", "Segment Map"],
                "Convert",
            ),
            (
                [str(exe), "-m", "SEGMENT",
                 "-f", str(graph), "-o", str(graph),
                 "-st", "tulip", "-sr", "n",
                 "-srt", "angular", "-sic", "-stb", "1024"],
                "Analyse",
            ),
            (
                [str(exe), "-m", "EXPORT",
                 "-f", str(graph), "-o", str(csv),
                 "-em", "shapegraph-map-csv"],
                "Export",
            ),
        ]

        for cmd, step_name in steps:
            try:
                r = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"depthmapXcli {step_name} timed out after {timeout:.0f} s")
            except FileNotFoundError:
                raise RuntimeError(f"Executable not found: {exe}")

            if r.returncode != 0:
                stderr = (r.stderr or r.stdout or "")[:1500]
                raise RuntimeError(
                    f"depthmapXcli {step_name} failed (exit {r.returncode}):\n{stderr}"
                )

        if not csv.exists():
            raise RuntimeError("depthmapXcli export did not produce a CSV")

        if out_graph is not None:
            shutil.copy2(graph, out_graph)

        elapsed = time.time() - t0
        print(f"    Completed in {elapsed:.1f}s")

        df = pd.read_csv(csv)
        df.columns = df.columns.str.strip()
        return df


# ---------------------------------------------------------------------------
# Post-processing: derived metrics
# ---------------------------------------------------------------------------

def add_derived_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute and append NAIN, NACH, and integrated to the result DataFrame.

    nain       = (Node Count ^ 1.2) / (Total Depth + 2)
    nach       = log(Choice + 1) / log(Total Depth + 3)
    integrated = Integration * log(Choice + 2)
    """
    nc  = df["T1024 Node Count"].astype(float)
    td  = df["T1024 Total Depth"].astype(float)
    ch  = df["T1024 Choice"].astype(float)
    itg = df["T1024 Integration"].astype(float)

    df = df.copy()
    df["nain"]       = (nc ** 1.2) / (td + 2)
    df["nach"]       = np.log(ch + 1) / np.log(td + 3)
    df["integrated"] = itg * np.log(ch + 2)
    return df

# ---------------------------------------------------------------------------
# Build GeoDataFrame from result coordinates
# ---------------------------------------------------------------------------

_ALL_SCORE_COLS = _DMX_COLS + ["nain", "nach", "integrated"]


def build_geodataframe(result_df: pd.DataFrame, crs: str) -> gpd.GeoDataFrame:
    """
    Build a GeoDataFrame from the depthmapXcli result DataFrame.
    Geometry is derived from the x1,y1,x2,y2 coordinate columns.
    """
    geometries = [
        LineString([(row.x1, row.y1), (row.x2, row.y2)])
        for row in result_df[["x1", "y1", "x2", "y2"]].itertuples()
    ]

    available = [c for c in _ALL_SCORE_COLS if c in result_df.columns]
    missing   = [c for c in _ALL_SCORE_COLS if c not in result_df.columns]
    if missing:
        print(f"  Warning: columns not in result: {missing}")

    return gpd.GeoDataFrame(result_df[available], geometry=geometries, crs=crs)


# ---------------------------------------------------------------------------
# Export: three formats
# ---------------------------------------------------------------------------

def export_results(
    result_df:  pd.DataFrame,
    gdf:        gpd.GeoDataFrame,
    out_dir:    Path,
    slug:       str,
    graph_path: Path | None = None,
    summary:    dict | None = None,
) -> None:
    """
    Save analysis results:

    1. depthmapX .graph  (*_segment_map.graph) — native format, re-openable in depthmapX GUI
    2. MIF file  (*_segment_map.mif / .mid)   — MapInfo with line geometry + all scores
    3. CSV  (*_segment_scores.csv)             — per-segment scores, then a blank-row
                                                 separator followed by summary stats
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    if graph_path and graph_path.exists():
        print(f"  depthmapX graph : {graph_path.name}")

    mif_path = out_dir / f"{slug}_segment_map.mif"
    try:
        gdf.to_file(str(mif_path), driver="MapInfo File")
        print(f"  MIF           : {mif_path.name}")
    except Exception as exc:
        print(f"  MIF export failed ({exc}); skipping")

    gpkg_path = out_dir / f"{slug}_segment_scores.gpkg"
    if gpkg_path.exists():
        try:
            gpkg_path.unlink()
        except PermissionError:
            pass  # file locked (e.g. open in QGIS); overwrite in place
    try:
        gdf.to_file(gpkg_path, driver="GPKG")
        print(f"  GPKG          : {gpkg_path.name}")
    except Exception as exc:
        print(f"  GPKG export failed ({exc}); skipping")

    score_cols = [c for c in gdf.columns if c != "geometry"]
    csv_path = out_dir / f"{slug}_segment_scores.csv"
    try:
        gdf[score_cols].to_csv(csv_path, index_label="segment_id")
        if summary:
            with open(csv_path, "a", newline="", encoding="utf-8") as f:
                f.write("\n")
                f.write("metric,value\n")
                for k, v in summary.items():
                    f.write(f"{k},{v}\n")
        print(f"  CSV           : {csv_path.name}")
    except PermissionError:
        print(f"  CSV           : skipped (file locked — close it in Excel first)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run depthmapXcli angular segment analysis on a centerlines DXF",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--dxf", type=Path,
        help="Path to an existing *_centerlines.dxf file",
    )
    src.add_argument(
        "--bia",
        help="BIA name -- resolves outputs/<slug>/<slug>_centerlines.dxf",
    )
    p.add_argument(
        "--depthmapxcli", type=Path, default=DEFAULT_EXE,
        help="Path to depthmapXcli_win64.exe",
    )
    p.add_argument(
        "--output-dir", type=Path, default=None,
        help="Output directory (default: same folder as the DXF)",
    )
    p.add_argument(
        "--crs", default=TORONTO_CRS,
        help="CRS of the DXF coordinates",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if args.dxf:
        dxf  = args.dxf.resolve()
        slug = dxf.stem.replace("_centerlines", "")
    else:
        slug = args.bia.strip().replace(" ", "_").replace("/", "-")
        dxf  = OUTPUT_ROOT / slug / f"{slug}_centerlines.dxf"
        if not dxf.exists():
            sys.exit(
                f"ERROR: DXF not found at:\n  {dxf}\n"
                "Run extract_bia.py first."
            )

    if not dxf.exists():
        sys.exit(f"ERROR: DXF not found: {dxf}")

    exe = args.depthmapxcli
    if not exe.exists():
        sys.exit(
            f"ERROR: depthmapXcli not found:\n  {exe}\n"
            "Pass --depthmapxcli <path> to override."
        )

    out_dir = args.output_dir or dxf.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  Angular Segment Analysis -- {slug.replace('_', ' ')}")
    print(f"  DXF : {dxf.name}")
    print(f"  Exe : {exe.name}")
    print(f"  Out : {out_dir}")
    print(f"{'='*60}\n")

    out_graph = out_dir / f"{slug}_segment_map.graph"

    try:
        result_df = run_depthmapxcli(dxf, exe, label=slug, out_graph=out_graph)
    except RuntimeError as e:
        sys.exit(f"\nFAILED: {e}")

    print(f"  {len(result_df):,} segments\n")

    print("Computing derived metrics...")
    result_df = add_derived_metrics(result_df)

    print("Building GeoDataFrame...")
    gdf = build_geodataframe(result_df, args.crs)
    print(f"  {len(gdf):,} segments | columns: {[c for c in gdf.columns if c != 'geometry']}")

    print("\nExporting results...")
    export_results(result_df, gdf, out_dir, slug, graph_path=out_graph)

    print("\nDone.")


if __name__ == "__main__":
    main()
