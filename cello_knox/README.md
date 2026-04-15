# Cello-Knox Adapter

This folder stages the first working `Path B` integration.

It is intended to live inside the Cello checkout root, for example:

`/Users/trieutran/RuleScape/cello/cello_knox`
Pipeline:

`Single_input_measurements.mat -> RuleScape-derived Cello inputs -> Cello scoring -> Knox CSV export`

## Layout

- `classic_single_input/upstream/`
  - local copies of the attached dataset and reference scripts
- `classic_single_input/output/cello/`
  - raw Cello scoring outputs written by the adapter
- `classic_single_input/output/knox/`
  - Knox-ready `designs.csv`, `part_library.csv`, `weight.csv`, and adapter metadata

The adapter now reads the CLASSIC source files directly from:

- `/Users/trieutran/RuleScape/dataio`

rather than maintaining duplicated staged copies under `cello_knox`.

## What The Adapter Does

`classic_single_input_adapter.py` runs only the Cello scoring path:

1. runs Yosys on the staged Verilog
2. loads the `CLASSIC` Verilog and UCF/input/output files directly from `RuleScape/dataio`
3. runs Cello compatibility checks and gate assignment
4. stops before the Eugene/DNA-design stages
5. extracts the selected `CLASSIC` gate and decodes its `design_assignment`
6. ranks the scored Cello candidates and writes the top `N` as Knox import CSVs

This avoids the current Eugene incompatibility in the `CLASSIC` custom UCF while still using Cello's scoring and gate-selection logic.

The exported Knox roles stay in the UCF-native `design_col_*` form on purpose. The exploratory `MAT -> CSV` script in `upstream/code.py` uses semantic labels, but those labels do not line up cleanly with the sparse indices in the current `CLASSIC_single_input_v6.UCF.json`. For `v1`, the adapter treats the UCF as the source of truth so the selected Cello design can be reproduced exactly.

## Run

```bash
python3 cello_knox/classic_single_input_adapter.py
```

Useful flags:

```bash
python3 cello_knox/classic_single_input_adapter.py --iterations 250 --verbose
```

```bash
python3 cello_knox/classic_single_input_adapter.py --top-n 5
```

Override the source folder explicitly if needed:

```bash
python3 cello_knox/classic_single_input_adapter.py --dataio-root /path/to/dataio --top-n 5
```

## Pipeline Endpoint

`pipeline_server.py` wraps the adapter in a small HTTP API so a React frontend can launch the single-input pipeline.

Start it from the Cello root:

```bash
python3 cello_knox/pipeline_server.py
```

Default address:

```text
http://127.0.0.1:8051
```

Available routes:

- `GET /api/pipeline/health`
- `POST /api/pipeline/cello-knox/run`

The run endpoint accepts either:

- `multipart/form-data`
  - recommended for a React upload form
- `application/json`
  - useful for local development when files already exist on disk

### React Upload Shape

Send a `FormData` body with:

- `verilog_file`
- `ucf_file`
- `input_file`
- `output_file`
- `topN`
- `iterations`
- optional `verbose`

Example:

```js
const formData = new FormData();
formData.append("verilog_file", verilogFile);
formData.append("ucf_file", ucfFile);
formData.append("input_file", inputFile);
formData.append("output_file", outputFile);
formData.append("topN", String(topN));
formData.append("iterations", String(iterations));

const response = await fetch("http://127.0.0.1:8051/api/pipeline/cello-knox/run", {
  method: "POST",
  body: formData,
});

const result = await response.json();
```

### JSON Path Shape

Example:

```json
{
  "verilogPath": "/Users/trieutran/RuleScape/dataio/classic_not_test.v",
  "ucfPath": "/Users/trieutran/RuleScape/dataio/CLASSIC_single_input_v6.UCF.json",
  "inputPath": "/Users/trieutran/RuleScape/dataio/CLASSIC_single_input.input.json",
  "outputPath": "/Users/trieutran/RuleScape/dataio/CLASSIC_single_input.output.json",
  "topN": 5,
  "iterations": 25
}
```

### Response Shape

Successful runs return:

- `requestId`
- `status`
- `runId`
- `requestedTopN`
- `returnedTopN`
- `bestScore`
- `celloOutputDir`
- `knoxOutputDir`
- `files.designs`
- `files.partLibrary`
- `files.weight`
- `files.summary`
- `summary.candidates`

Each request is staged under:

`cello_knox/pipeline_runs/<request-id>/`

## Current Output

After a successful run, the adapter writes:

- `classic_single_input/output/cello/<verilog>/`
  - raw Yosys and scoring-side Cello artifacts
- `classic_single_input/output/knox/<run-id>/designs.csv`
- `classic_single_input/output/knox/<run-id>/part_library.csv`
- `classic_single_input/output/knox/<run-id>/weight.csv`
- `classic_single_input/output/knox/<run-id>/adapter_result.json`

The current staged example produces:

- run id: `classic_not_test_CLASSIC_single_input_v6`
- best gate: `Switch_003`
- best score: `1.5052113891172192`
- optional ranked candidate list in `adapter_result.json`

## Import Into Knox

Knox already supports CSV import through `/import/csv` and `/merge/csv`.

For the current adapter output, use these three files together:

- `designs.csv`
- `part_library.csv`
- `weight.csv`

If you want a merged design space, use Knox's `/merge/csv` path. If you want per-design spaces, use `/import/csv`.

## Current V1 Limits

- only supports the staged `CLASSIC` single-input NOT example
- only supports a single selected gate in the best design
- exports the top `N` scored single-gate designs, with `N=1` by default
- keeps fixed input/output assignments in `adapter_result.json` metadata instead of expanding them into the CSV design row
- does not yet generate Goldbar or call Knox automatically

Those are deliberate `v1` limits so the adapter stays stable while the custom UCF format is still being refined.
