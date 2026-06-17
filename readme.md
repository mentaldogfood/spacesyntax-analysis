# Space Syntax Analysis Pipeline for Toronto BIAs

Runs depthmapX CLI segment analysis for spatial syntax (Normalized Angular Integration, NAIN, and Normalized Angular Choice, NACH) at BIA boundary scale (with a 800m buffer to reduce edge effects)

Scores are joined back to road geometry and exported as scored GeoJSON, along with other key statistics which have been produced for the entire set of road segments within a BIA file
A quick visualization of the ranked road network is exported as PNG and MIF for further processing in QGIS

## Requirements

```
pip install -r requirements.txt
```

DepthmapX CLI download: https://github.com/SpaceGroupUCL/depthmapX/releases
Add `depthmapXcli.exe` to PATH or pass `--depthmapx "C:/path/depthmapXcli.exe"`

## Usage

main.py — full Space Syntax pipeline for Toronto BIAs.

Orchestrates:
  1. extract_bia        : clip road centrelines, export as DXF
  2. depthmapx_segment  : run depthmapXcli angular segment analysis,
                          compute derived metrics, export .graph / MIF / CSV
  3. Summary stats      : avg NAIN, avg NACH, avg segment length, NAIN-NACH Pearson r
                          appended as a summary section in segment_scores.csv
  4. PNG maps           : NAIN spectral map, NACH spectral map, NAIN-NACH scatter plot

Usage:
```
python scripts/main.py --bia "Downtown Yonge"
python scripts/main.py --list-bias
python scripts/extract_bia.py --bia "Bloor West Village"
python scripts/extract_bia.py --list-bias
```
## Methodology

The DepthMapX CLI's built- in angular segment analysis tool returns 

Key statistical terms and formulas are derived from these pieces of literature

https://www.mdpi.com/2220-9964/12/6/216
https://www.mdpi.com/2071-1050/13/6/3394
https://cyprusconferences.org/14sss/wp-content/uploads/2024/05/sss142024_4.pdf 

**NAIN (Normalized Angular Integration)** 

How well integrated a street is with the rest of the road network, usually high for streets with commercial hubs. Normalized to be comparable across different catchment area sizes.

=(value("T1024 Node Count")^(1.2))/(value("T1024 Total Depth")+2)

**NACH (Normalized Angular Choice)**

How often is a street chosen in the commute path between two points within the system, usually high for street with heavy commute traffic. Normalized to be comparable across different catchment area sizes.

=log(value("T1024 Choice")+1)/log(value("T1024 Total Depth")+3)

**Segment Adjustment**

A penalization formula added to lower the NAIN and NACH scores of long segments, as normalized to the BIA range. 

norm_len     = (L - L_min) / (L_max - L_min)        # min-max within BIA, [0, 1]

length_penalty = 1 - (norm_len ^ 1.5) * 0.3

NAIN_adjusted  = nain  × length_penalty
NACH_adjusted  = nach  × length_penalty

**INCH (Correlation)**

Measure accessibility, or how closely aligned are the well-integrated streets with the high commuter-traffic streets.

=corr(NAIN, NACH)