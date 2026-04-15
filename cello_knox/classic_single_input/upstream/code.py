import scipy.io as sio
import numpy as np
import pandas as pd

# ── Load files ───────────────────────────────────────────────────────────────
mat = sio.loadmat("Single_input_measurements.mat")

assign = mat["eu_ordered_assign_split"]  # design matrix  (207360 x 9)
exp    = mat["ordered_eu_exp"]           # measurements   (207360 x 4)

# ── Part name mappings ───────────────────────────────────────────────────────
# Source: Figure 2B of Rai*, O'Connell* et al. (2025)
# All 10 diversified component categories for the single-input synTF circuit

# synTF protein domains (N to C terminus)
TA_PARTS   = ["VPR", "VP64", "VP16", "p65"]              # 4 activation domains
IDP_PARTS  = ["FusN", "DDX4", "HNR", "SRSF", "none"]    # 4 IDPs + no-IDP option
ZF_PARTS   = ["high_WT", "med_dC", "low_dC6x"]           # 3 ZF affinity mutants

# synTF coding expression unit
PROM_PARTS = ["CMV", "hEF1a1", "RSV", "hPGK"]           # 4 promoters

# col 4 is Term(4) x Spacer1(3) combined into a single 12-level index
# so we generate all 12 combinations in order
TERMS   = ["T3", "T4", "T6", "T7"]
SPACER1 = ["0bp", "250bp", "500bp"]
TERM_SPACER_PARTS = [f"{t}_{s}" for t in TERMS for s in SPACER1]  # 12 combos

# Reporter expression unit
BM_PARTS        = ["n=2", "n=4", "n=8", "n=12"]          # 4 binding motif counts
COREPROM_PARTS  = ["mCMV", "ybTATA", "miniTK"]           # 3 core promoters
SPACER2_PARTS   = ["0bp", "250bp", "500bp"]              # 3 spacer lengths

# Circuit-level
ORIENT_PARTS    = ["coding_then_reporter", "reporter_then_coding"]  # 2 orientations

# ── Map each assign column to its part list ───────────────────────────────────
COL_DEFS = [
    ("TA",           TA_PARTS),
    ("IDP",          IDP_PARTS),
    ("ZF",           ZF_PARTS),
    ("synTF_Prom",   PROM_PARTS),
    ("Term_Spacer1", TERM_SPACER_PARTS),   # combined column
    ("BM_num",       BM_PARTS),
    ("Core_Prom",    COREPROM_PARTS),
    ("Spacer2",      SPACER2_PARTS),
    ("Orientation",  ORIENT_PARTS),        # this doubles as the train/test split
]

# ── Build translation dictionaries: index -> part name ───────────────────────
col_maps = {}
for col_idx, (col_name, parts_list) in enumerate(COL_DEFS):
    n_levels = len(np.unique(assign[:, col_idx]))

    if len(parts_list) != n_levels:
        print(f"WARNING: col {col_idx} ({col_name}) has {n_levels} levels in data "
              f"but {len(parts_list)} names provided. Check part list!")

    col_maps[col_name] = {i + 1: name for i, name in enumerate(parts_list)}

# ── Build the main dataframe ──────────────────────────────────────────────────
df = pd.DataFrame(assign, columns=[name for name, _ in COL_DEFS])

for col_name, parts_list in COL_DEFS:
    df[col_name] = df[col_name].map(col_maps[col_name])

# Attach measurements
df["raw_off"]    = exp[:, 0]   # raw fluorescence, uninduced
df["raw_on"]     = exp[:, 1]   # raw fluorescence, induced (4-OHT)
df["expression"] = exp[:, 2]   # normalized expression (raw_on / raw_off)
df["read_count"] = exp[:, 3].astype(int)  # sequencing read count

DESIGN_COLS = [name for name, _ in COL_DEFS]

# ── CSV 1: designs.csv ────────────────────────────────────────────────────────
# Use orientation == "coding_then_reporter" as one set of unique designs
# (both orientations are real designs, so we keep all 103,680 unique ones)
designs = df.drop_duplicates(subset=DESIGN_COLS)[DESIGN_COLS].reset_index(drop=True)
designs.to_csv("single_input_designs.csv", index=False)

# ── CSV 2: part_library.csv ───────────────────────────────────────────────────
parts_rows = []
for col_name, parts_list in COL_DEFS:
    for part_id in parts_list:
        parts_rows.append({"id": part_id, "role": col_name})

part_lib_out = (pd.DataFrame(parts_rows)
                  .drop_duplicates(subset="id")
                  .sort_values(["role", "id"])
                  .reset_index(drop=True))

part_lib_out.to_csv("single_input_part_library.csv", index=False)

# ── CSV 3: expression_scores.csv ──────────────────────────────────────────────
mean_expr = df.groupby(DESIGN_COLS)["expression"].mean().reset_index()
scores = designs.merge(mean_expr, on=DESIGN_COLS, how="left")[["expression"]]
scores.to_csv("single_input_expression_scores.csv", index=False)

print("Done!")
print(f"  designs:  {designs.shape[0]} rows")
print(f"  parts:    {len(part_lib_out)} unique parts")
print(f"  scores:   {scores.shape[0]} rows, {(scores['expression'] > 0).sum()} with measured expression")