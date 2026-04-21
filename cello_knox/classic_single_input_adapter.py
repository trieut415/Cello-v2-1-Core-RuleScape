import argparse
import csv
import json
import sys
import types
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CELLO_ROOT = PROJECT_ROOT if (PROJECT_ROOT / "core_algorithm").exists() else PROJECT_ROOT / "Cello-v2-1-Core"
CLASSIC_ROOT = Path(__file__).resolve().parent / "classic_single_input"
DEFAULT_DATAIO_ROOT = PROJECT_ROOT.parent / "dataio" if (PROJECT_ROOT.parent / "dataio").exists() else CLASSIC_ROOT / "cello_inputs"
DEFAULT_UCF = "CLASSIC_single_input_v6.UCF"
DEFAULT_INPUT = "CLASSIC_single_input.input"
DEFAULT_OUTPUT = "CLASSIC_single_input.output"
DEFAULT_VERILOG = "classic_not_test"
DEFAULT_SEARCH = "exhaustive"


def normalize_search_mode(value: str | None) -> str:
    normalized = str(value or DEFAULT_SEARCH).strip().lower()
    if normalized in {"exhaustive", "annealing"}:
        return normalized
    raise ValueError(f"Unsupported search mode: {value}")


@dataclass(frozen=True)
class CandidateResult:
    score: float
    selected_gate: str
    selected_gate_model: str
    selected_parts: tuple[str, ...]
    selected_roles: tuple[str, ...]
    input_assignments: tuple[dict[str, Any], ...]
    gate_assignments: tuple[dict[str, Any], ...]
    output_assignments: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class AdapterResult:
    run_id: str
    requested_top_n: int
    candidates: tuple[CandidateResult, ...]
    part_library_rows: tuple[tuple[str, ...], ...]
    cello_output_dir: Path
    knox_output_dir: Path
    search_mode: str


def ensure_cello_imports() -> None:
    stubbed_modules = {
        "core_algorithm.utils.make_eugene_script": {},
        "core_algorithm.utils.dna_design": {},
        "core_algorithm.utils.sbol_plot": {"plotter": lambda *args, **kwargs: None},
        "core_algorithm.utils.response_plot": {"plot_bars": lambda *args, **kwargs: None},
        "core_algorithm.utils.sbol": {},
    }
    for module_name, attributes in stubbed_modules.items():
        if module_name not in sys.modules:
            module = types.ModuleType(module_name)
            for key, value in attributes.items():
                setattr(module, key, value)
            sys.modules[module_name] = module

    if "scipy" not in sys.modules:
        try:
            import scipy  # noqa: F401
            import scipy.optimize  # noqa: F401
        except ImportError:
            scipy_shim = types.ModuleType("scipy")
            optimize_shim = types.ModuleType("scipy.optimize")

            class Bounds:
                def __init__(self, *args, **kwargs):
                    self.args = args
                    self.kwargs = kwargs

            def dual_annealing(*args, **kwargs):
                raise RuntimeError(
                    "dual_annealing is unavailable in this adapter runtime. "
                    "Install scipy in the same Python environment that runs pipeline_server.py"
                )

            optimize_shim.Bounds = Bounds
            optimize_shim.dual_annealing = dual_annealing
            scipy_shim.optimize = optimize_shim
            sys.modules["scipy"] = scipy_shim
            sys.modules["scipy.optimize"] = optimize_shim

    if "threadpoolctl" not in sys.modules:
        try:
            import threadpoolctl  # noqa: F401
        except ImportError:
            shim = types.ModuleType("threadpoolctl")

            @contextmanager
            def threadpool_limits(*args, **kwargs):
                yield

            def threadpool_info():
                return [{"num_threads": 1}]

            shim.threadpool_limits = threadpool_limits
            shim.threadpool_info = threadpool_info
            sys.modules["threadpoolctl"] = shim

    cello_path = str(CELLO_ROOT)
    if cello_path not in sys.path:
        sys.path.insert(0, cello_path)


def patch_cello_eval_helpers() -> None:
    ensure_cello_imports()
    from core_algorithm.utils import gate_assignment as gate_assignment_module

    if getattr(gate_assignment_module, "_cello_knox_eval_patch", False):
        return

    Input = gate_assignment_module.Input
    Output = gate_assignment_module.Output
    Gate = gate_assignment_module.Gate
    debug_print = gate_assignment_module.debug_print
    log = gate_assignment_module.log

    def patched_input_add_eval_params(self, functions, params):
        try:
            self.functions = functions
            self.resp_func_eq = None
            self.tandem_func_eq = None
            self.params = params
            self.ymax = params["ymax"]
            self.ymin = params["ymin"]
            try:
                self.resp_func_eq = self.functions.get("response_function", "").replace("$", "")
                self.tandem_func_eq = self.functions.get("tandem_interference_factor", "").replace("$", "")
                self.tandem_func_eq = self.tandem_func_eq.replace("^", "**")

                base_env = dict(params)
                for level, value in self.states.items():
                    env = dict(base_env)
                    env["STATE"] = value
                    self.out_scores[level] = eval(self.resp_func_eq, {}, env)
                    if self.tandem_func_eq:
                        self.tandem_scores[level] = eval(self.tandem_func_eq, {}, env)
            except Exception as exc:
                debug_print(
                    f"ERROR calculating input score for {str(self)}, with function {self.resp_func_eq}\n{exc}"
                )
        except Exception as exc:
            debug_print(f"Error adding evaluation parameters to {str(self)}\n{self.resp_func_eq} | {params}\n{exc}")

    def patched_output_eval_output(self, input_score):
        env = {"x": input_score, "c": self.unit_conversion}
        env.update(self.params)
        self.out_score = eval(self.function, {}, env)
        return self.out_score

    def patched_gate_eval_gate(self, gate_name, in_comp):
        eval_params = self.gate_params[gate_name]
        env = dict(eval_params)
        env["x"] = in_comp
        result = eval(self.response_func, {}, env)
        tandem = 0
        if self.tandem_factor_func:
            tandem = eval(self.tandem_factor_func, {}, env)
        return result, gate_name, tandem

    Input.add_eval_params = patched_input_add_eval_params
    Output.eval_output = patched_output_eval_output
    Gate.eval_gate = patched_gate_eval_gate
    gate_assignment_module._cello_knox_eval_patch = True


def build_runner(
    verilog_name: str,
    ucf_name: str,
    input_name: str,
    output_name: str,
    input_root: Path,
    cello_output_root: Path,
    iterations: int,
    search_mode: str,
    verbose: bool,
):
    ensure_cello_imports()
    from core_algorithm.celloAlgo import CELLO3

    if (input_root / "verilogs").exists():
        verilogs_path = input_root / "verilogs"
    else:
        verilogs_path = input_root

    if (input_root / "constraints").exists():
        constraints_path = input_root / "constraints"
    else:
        constraints_path = input_root

    runner = CELLO3.__new__(CELLO3)
    runner.verbose = verbose
    runner.print_iters = False
    runner.exhaustive = normalize_search_mode(search_mode) == "exhaustive"
    runner.test_configs = False
    runner.log_overwrite = True
    runner.total_iters = iterations
    runner.verilogs_path = str(verilogs_path.resolve())
    runner.constraints_path = str(constraints_path.resolve())
    runner.out_path = str(cello_output_root.resolve())
    runner.verilog_name = verilog_name
    runner.ucf_name = ucf_name
    runner.in_name = input_name
    runner.out_name = output_name
    runner.iter_count = 0
    runner.best_score = 0
    runner.best_graphs = []
    runner.units = "Unknown_Units"
    runner.conversions = {}
    runner.filepath = str(cello_output_root / verilog_name / f"{verilog_name}_{ucf_name[:-4]}")
    return runner


def load_netlist(runner) -> None:
    ensure_cello_imports()
    from core_algorithm.utils.netlist_class import Netlist

    net_path = Path(runner.out_path) / runner.verilog_name / f"{runner.verilog_name}_{runner.ucf_name[:-4]}_yosys.json"
    with net_path.open("r", encoding="utf-8") as handle:
        net_json = json.load(handle)
    runner.rnl = Netlist(net_json)
    if not runner.rnl.is_valid_netlist():
        raise RuntimeError(f"Invalid netlist generated at {net_path}")


def load_ucf(runner) -> None:
    ensure_cello_imports()
    from core_algorithm.utils.ucf_class import UCF

    runner.ucf = UCF(runner.constraints_path, runner.ucf_name, runner.in_name, runner.out_name)
    if not runner.ucf.valid:
        raise RuntimeError("Failed to load CLASSIC UCF/input/output bundle")

    units = runner.ucf.query_top_level_collection(runner.ucf.UCFout, "measurement_std")
    if units:
        runner.units = units[0]["signal_carrier_units"]
    else:
        units = runner.ucf.query_top_level_collection(runner.ucf.UCFmain, "measurement_std")
        if units:
            runner.units = units[0]["signal_carrier_units"]

    conversions = runner.ucf.query_top_level_collection(runner.ucf.UCFout, "models")
    for gate in conversions:
        unit_conversion = [param["value"] for param in gate["parameters"] if param["name"] == "unit_conversion"]
        if unit_conversion:
            runner.conversions[gate["name"][:-6]] = unit_conversion[0]


def design_factor_column_key(column_name: str) -> int:
    return int(column_name.rsplit("_", 1)[1])


def find_model_for_gate(ucf_main: list[dict[str, Any]], gate_name: str) -> str:
    for gate in ucf_main:
        if gate["collection"] == "gates" and gate["name"] == gate_name:
            return gate["model"]
    raise KeyError(f"Could not find gate metadata for {gate_name}")


def find_model_object(ucf_main: list[dict[str, Any]], model_name: str) -> dict[str, Any]:
    for collection in ucf_main:
        if collection["collection"] == "models" and collection["name"] == model_name:
            return collection
    raise KeyError(f"Could not find model {model_name}")


def build_design_factor_lookup(ucf_main: list[dict[str, Any]]) -> dict[str, dict[int, str]]:
    lookup: dict[str, dict[int, str]] = {}
    for collection in ucf_main:
        if collection.get("collection") != "parts" or collection.get("type") != "design_factor":
            continue
        name = collection["name"]
        column_prefix, _, suffix = name.rpartition("_")
        lookup.setdefault(column_prefix, {})[int(suffix)] = name
    return lookup


def decode_design_assignment(model: dict[str, Any], design_factor_lookup: dict[str, dict[int, str]]) -> tuple[list[str], list[str]]:
    assignment = model.get("design_assignment")
    if not assignment:
        raise KeyError("Selected CLASSIC gate model has no design_assignment payload")

    selected_parts: list[str] = []
    selected_roles: list[str] = []
    for column_name in sorted(design_factor_lookup, key=design_factor_column_key):
        values_by_index = design_factor_lookup[column_name]
        raw_value = assignment.get(column_name)
        if raw_value is None:
            raise KeyError(f"Missing {column_name} in selected design_assignment")
        part_id = values_by_index.get(int(raw_value))
        if part_id is None:
            raise KeyError(
                f"Missing design factor part for {column_name} value {raw_value}; "
                f"known values: {sorted(values_by_index)}"
            )
        selected_parts.append(part_id)
        selected_roles.append(column_name)
    return selected_parts, selected_roles


def build_part_library_rows(design_factor_lookup: dict[str, dict[int, str]]) -> list[list[str]]:
    rows = [["id", "role", "sequence"]]
    for role, part_ids in sorted(design_factor_lookup.items(), key=lambda item: design_factor_column_key(item[0])):
        for _, part_id in sorted(part_ids.items()):
            rows.append([part_id, role, ""])
    return rows


def build_candidate_result(runner, graph, circuit_score: float) -> CandidateResult | None:
    if len(graph.gates) != 1:
        return None

    selected_gate = graph.gates[0].gate_in_use
    model_name = find_model_for_gate(runner.ucf.UCFmain, selected_gate)
    selected_parts, selected_roles = decode_design_assignment(
        find_model_object(runner.ucf.UCFmain, model_name),
        runner.design_factor_lookup,
    )

    input_assignments = [
        {"net": net_name, "sensor": assigned_input.name}
        for (net_name, _), assigned_input in zip(runner.rnl.inputs, graph.inputs)
    ]
    gate_assignments = [
        {
            "node": gate.name,
            "gate_type": gate.gate_type,
            "selected_gate": gate.gate_in_use,
            "score": gate.best_score,
        }
        for gate in graph.gates
    ]
    output_assignments = [
        {"net": net_name, "device": assigned_output.name}
        for (net_name, _), assigned_output in zip(runner.rnl.outputs, graph.outputs)
    ]

    return CandidateResult(
        score=float(circuit_score),
        selected_gate=selected_gate,
        selected_gate_model=model_name,
        selected_parts=tuple(selected_parts),
        selected_roles=tuple(selected_roles),
        input_assignments=tuple(input_assignments),
        gate_assignments=tuple(gate_assignments),
        output_assignments=tuple(output_assignments),
    )


def enable_candidate_capture(runner) -> None:
    if getattr(runner, "_candidate_capture_enabled", False):
        return

    runner.design_factor_lookup = build_design_factor_lookup(runner.ucf.UCFmain)
    runner.scored_candidates = []
    original_score_circuit = runner.score_circuit

    def wrapped_score_circuit(self, graph):
        circuit_score, tb, tb_labels = original_score_circuit(graph)
        candidate = build_candidate_result(self, graph, circuit_score)
        if candidate is not None:
            self.scored_candidates.append(candidate)
        return circuit_score, tb, tb_labels

    runner.score_circuit = types.MethodType(wrapped_score_circuit, runner)
    runner._candidate_capture_enabled = True


def select_top_candidates(runner, best_result, top_n: int) -> tuple[CandidateResult, ...]:
    if top_n < 1:
        raise ValueError("top_n must be at least 1")

    best_score, best_graph, _, _ = best_result
    fallback_candidate = build_candidate_result(runner, best_graph, best_score)

    deduped: dict[tuple[str, tuple[str, ...]], CandidateResult] = {}
    for candidate in sorted(runner.scored_candidates, key=lambda item: item.score, reverse=True):
        deduped.setdefault((candidate.selected_gate_model, candidate.selected_parts), candidate)

    if not deduped and fallback_candidate is not None:
        deduped[(fallback_candidate.selected_gate_model, fallback_candidate.selected_parts)] = fallback_candidate

    ranked = tuple(list(deduped.values())[:top_n])
    if not ranked:
        raise RuntimeError("Adapter could not collect any scored Cello candidates")
    return ranked


def prepare_cello_runner(
    input_root: Path,
    output_root: Path,
    verilog_name: str,
    ucf_name: str,
    input_name: str,
    output_name: str,
    iterations: int,
    search_mode: str,
    verbose: bool,
):
    ensure_cello_imports()
    patch_cello_eval_helpers()
    from core_algorithm.celloAlgo import CELLO3
    from core_algorithm.utils import log
    from core_algorithm.utils.cello_helpers import print_centered
    from core_algorithm.utils.logic_synthesis import call_YOSYS

    (PROJECT_ROOT / "logs").mkdir(exist_ok=True)
    runner = build_runner(
        verilog_name=verilog_name,
        ucf_name=ucf_name,
        input_name=input_name,
        output_name=output_name,
        input_root=input_root,
        cello_output_root=output_root,
        iterations=iterations,
        search_mode=search_mode,
        verbose=verbose,
    )

    log.config_logger(runner.verilog_name, runner.ucf_name, runner.log_overwrite)
    log.reset_logs()
    print_centered(["CELLO V2.1", f"{runner.verilog_name} + {runner.ucf_name}"])

    if not call_YOSYS(runner.verilogs_path, runner.out_path, runner.verilog_name, runner.ucf_name[:-4], 1):
        raise RuntimeError("Yosys failed to generate the netlist")

    load_netlist(runner)
    load_ucf(runner)
    enable_candidate_capture(runner)

    valid, max_iterations = CELLO3.check_conditions(runner, verbose=verbose)
    if not valid:
        raise RuntimeError("CLASSIC inputs are not compatible with the generated netlist")

    return runner, max_iterations


def run_cello_scoring_only(
    input_root: Path,
    output_root: Path,
    verilog_name: str,
    ucf_name: str,
    input_name: str,
    output_name: str,
    iterations: int,
    search_mode: str,
    verbose: bool,
):
    ensure_cello_imports()
    from core_algorithm.celloAlgo import CELLO3

    runner, max_iterations = prepare_cello_runner(
        input_root=input_root,
        output_root=output_root,
        verilog_name=verilog_name,
        ucf_name=ucf_name,
        input_name=input_name,
        output_name=output_name,
        iterations=iterations,
        search_mode=search_mode,
        verbose=verbose,
    )
    enable_candidate_capture(runner)
    best_result = CELLO3.techmap(runner, max_iterations)
    if not best_result:
        raise RuntimeError("Cello did not produce a best assignment")
    return runner, best_result


def build_adapter_result(runner, best_result, knox_output_dir: Path, requested_top_n: int) -> AdapterResult:
    run_id = f"{runner.verilog_name}_{runner.ucf_name[:-4]}"
    top_candidates = select_top_candidates(runner, best_result, requested_top_n)
    return AdapterResult(
        run_id=run_id,
        requested_top_n=requested_top_n,
        candidates=top_candidates,
        part_library_rows=tuple(tuple(row) for row in build_part_library_rows(runner.design_factor_lookup)),
        cello_output_dir=Path(runner.out_path) / runner.verilog_name,
        knox_output_dir=knox_output_dir,
        search_mode="exhaustive" if runner.exhaustive else "annealing",
    )


def write_csv(path: Path, rows: list[list[Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerows(rows)


def write_knox_exports(result: AdapterResult) -> None:
    result.knox_output_dir.mkdir(parents=True, exist_ok=True)

    designs_csv = result.knox_output_dir / "designs.csv"
    part_library_csv = result.knox_output_dir / "part_library.csv"
    weights_csv = result.knox_output_dir / "weight.csv"
    summary_json = result.knox_output_dir / "adapter_result.json"

    write_csv(designs_csv, [["design"], *[list(candidate.selected_parts) for candidate in result.candidates]])
    write_csv(part_library_csv, result.part_library_rows)
    write_csv(weights_csv, [["weight"], *[[candidate.score] for candidate in result.candidates]])

    summary_payload = {
        "run_id": result.run_id,
        "requested_top_n": result.requested_top_n,
        "returned_top_n": len(result.candidates),
        "best_score": result.candidates[0].score,
        "best_selected_gate": result.candidates[0].selected_gate,
        "search_mode": result.search_mode,
        "candidates": [
            {
                "rank": rank,
                "score": candidate.score,
                "selected_gate": candidate.selected_gate,
                "selected_gate_model": candidate.selected_gate_model,
                "selected_parts": candidate.selected_parts,
                "selected_roles": candidate.selected_roles,
                "input_assignments": candidate.input_assignments,
                "gate_assignments": candidate.gate_assignments,
                "output_assignments": candidate.output_assignments,
            }
            for rank, candidate in enumerate(result.candidates, start=1)
        ],
        "cello_output_dir": str(result.cello_output_dir),
        "knox_output_dir": str(result.knox_output_dir),
    }
    with summary_json.open("w", encoding="utf-8") as handle:
        json.dump(summary_payload, handle, indent=2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the CLASSIC single-input Cello-to-Knox adapter")
    parser.add_argument(
        "--classic-root",
        type=Path,
        default=CLASSIC_ROOT,
        help="Root folder holding adapter outputs",
    )
    parser.add_argument(
        "--dataio-root",
        type=Path,
        default=DEFAULT_DATAIO_ROOT,
        help="Source-of-truth folder containing the CLASSIC Verilog and constraint files",
    )
    parser.add_argument(
        "--verilog-name",
        default=DEFAULT_VERILOG,
        help="Verilog file base name without .v",
    )
    parser.add_argument(
        "--ucf-name",
        default=DEFAULT_UCF,
        help="Main UCF base name without .json",
    )
    parser.add_argument(
        "--input-name",
        default=DEFAULT_INPUT,
        help="Input sensor file base name without .json",
    )
    parser.add_argument(
        "--output-name",
        default=DEFAULT_OUTPUT,
        help="Output device file base name without .json",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=100,
        help="Maximum Cello iterations for the simulated annealing pass",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=1,
        help="Number of top-scoring unique Cello designs to export for Knox",
    )
    parser.add_argument(
        "--search",
        choices=["exhaustive", "annealing"],
        default=DEFAULT_SEARCH,
        help="Cello gate-assignment search mode",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable Cello's verbose compatibility logging",
    )
    return parser.parse_args()


def run_pipeline(
    classic_root: Path,
    dataio_root: Path,
    verilog_name: str,
    ucf_name: str,
    input_name: str,
    output_name: str,
    iterations: int,
    top_n: int,
    search_mode: str,
    verbose: bool,
) -> AdapterResult:
    classic_root = classic_root.resolve()
    dataio_root = dataio_root.resolve()
    cello_output_root = classic_root / "output" / "cello"
    knox_output_dir = classic_root / "output" / "knox" / f"{verilog_name}_{ucf_name[:-4]}"

    runner, best_result = run_cello_scoring_only(
        input_root=dataio_root,
        output_root=cello_output_root,
        verilog_name=verilog_name,
        ucf_name=ucf_name,
        input_name=input_name,
        output_name=output_name,
        iterations=iterations,
        search_mode=search_mode,
        verbose=verbose,
    )
    result = build_adapter_result(runner, best_result, knox_output_dir, top_n)
    write_knox_exports(result)
    return result


def main() -> int:
    args = parse_args()
    result = run_pipeline(
        classic_root=args.classic_root,
        dataio_root=args.dataio_root,
        verilog_name=args.verilog_name,
        ucf_name=args.ucf_name,
        input_name=args.input_name,
        output_name=args.output_name,
        iterations=args.iterations,
        top_n=args.top_n,
        search_mode=args.search,
        verbose=args.verbose,
    )

    best_candidate = result.candidates[0]
    print(f"Run ID: {result.run_id}")
    print(f"Exported top candidates: {len(result.candidates)}")
    print(f"Best score: {best_candidate.score}")
    print(f"Selected gate: {best_candidate.selected_gate}")
    print(f"Selected design parts: {', '.join(best_candidate.selected_parts)}")
    print(f"Cello output: {result.cello_output_dir}")
    print(f"Knox CSVs: {result.knox_output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
