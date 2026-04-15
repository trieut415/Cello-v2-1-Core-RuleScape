import scipy.io as sio
import numpy as np
import pandas as pd

# ── The only hardcoded values — mat file and table names ─────────────────────
MAT_FILE       = "Single_input_measurements.mat"
MAT_KEY_ASSIGN = "eu_ordered_assign_split"
MAT_KEY_EXP    = "ordered_eu_exp"

# ── Load the mat file ─────────────────────────────────────────────────────────
mat    = sio.loadmat(MAT_FILE)
assign = mat[MAT_KEY_ASSIGN]
exp    = mat[MAT_KEY_EXP]

# ── Automatically generate generic column names from the data ─────────────────
# Figures out how many columns there are and how many unique parts per column
# then names them generically: col0_part1, col0_part2, col1_part1, etc.
n_cols = assign.shape[1]

COL_DEFS = []
for col_idx in range(n_cols):
    n_levels   = len(np.unique(assign[:, col_idx]))
    col_name   = f"col{col_idx}"
    parts_list = [f"col{col_idx}_part{i+1}" for i in range(n_levels)]
    COL_DEFS.append((col_name, parts_list))

# ── Build translation dictionaries: index number -> part name ─────────────────
col_maps = {}
for col_idx, (col_name, parts_list) in enumerate(COL_DEFS):
    col_maps[col_name] = {i + 1: name for i, name in enumerate(parts_list)}

# ── Build the main dataframe ──────────────────────────────────────────────────
df = pd.DataFrame(assign, columns=[name for name, _ in COL_DEFS])

for col_name, parts_list in COL_DEFS:
    df[col_name] = df[col_name].map(col_maps[col_name])

df["raw_off"]    = exp[:, 0]
df["raw_on"]     = exp[:, 1]
df["expression"] = exp[:, 2]
df["read_count"] = exp[:, 3].astype(int)

DESIGN_COLS = [name for name, _ in COL_DEFS]

# ── CSV 1: designs.csv ────────────────────────────────────────────────────────
designs = df.drop_duplicates(subset=DESIGN_COLS)[DESIGN_COLS].reset_index(drop=True)
designs.to_csv("gdesigns.csv", index=False)

# ── CSV 2: part_library.csv ───────────────────────────────────────────────────
parts_rows = []
for col_name, parts_list in COL_DEFS:
    for part_id in parts_list:
        parts_rows.append({"id": part_id, "role": col_name})

part_lib_out = (pd.DataFrame(parts_rows)
                  .drop_duplicates(subset="id")
                  .sort_values(["role", "id"])
                  .reset_index(drop=True))

part_lib_out.to_csv("gpart_library.csv", index=False)

# ── CSV 3: expression_scores.csv ─────────────────────────────────────────────
mean_expr = df.groupby(DESIGN_COLS)["expression"].mean().reset_index()
scores = designs.merge(mean_expr, on=DESIGN_COLS, how="left")[["expression"]]
scores.to_csv("gexpression_scores.csv", index=False)

print("Done!")
print(f"  designs:  {designs.shape[0]} rows")
print(f"  parts:    {len(part_lib_out)} unique parts")
print(f"  scores:   {scores.shape[0]} rows, {(scores['expression'] > 0).sum()} with measured expression")