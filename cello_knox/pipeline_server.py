import csv
import io
import json
import os
import shutil
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime
from email.parser import BytesParser
from email.policy import default as email_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from classic_single_input_adapter import normalize_search_mode, run_pipeline


SERVER_ROOT = Path(__file__).resolve().parent
RUNS_ROOT = SERVER_ROOT / "pipeline_runs"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8051
DEFAULT_KNOX_BASE_URL = os.environ.get("KNOX_BASE_URL", "http://127.0.0.1:8080")
PREVIEW_MAX_LINES = 20
PREVIEW_MAX_CHARS = 6000
KNOX_TIMEOUT_SECONDS = 60
RULE_MATRIX_RESERVED_KEYS = {"designIDs", "scores", "labels"}

DOWNLOADABLE_ARTIFACTS = {
    "designs": ("designs.csv", "text/csv; charset=utf-8"),
    "partLibrary": ("part_library.csv", "text/csv; charset=utf-8"),
    "weight": ("weight.csv", "text/csv; charset=utf-8"),
    "summary": ("adapter_result.json", "application/json; charset=utf-8"),
}

FIELD_ALIASES = {
    "verilog": ("verilog_file", "verilogFile", "verilog"),
    "ucf": ("ucf_file", "ucfFile", "ucf"),
    "input": ("input_file", "inputFile", "input"),
    "output": ("output_file", "outputFile", "output"),
}


@dataclass(frozen=True)
class UploadedFile:
    filename: str
    content: bytes


def make_request_id() -> str:
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return f"{timestamp}_{uuid.uuid4().hex[:8]}"


def strip_suffix(filename: str) -> str:
    if filename.endswith(".UCF.json"):
        return filename[: -len(".json")]
    if filename.endswith(".input.json"):
        return filename[: -len(".json")]
    if filename.endswith(".output.json"):
        return filename[: -len(".json")]
    if filename.endswith(".json"):
        return filename[: -len(".json")]
    if filename.endswith(".v"):
        return filename[: -len(".v")]
    return filename


def sanitize_identifier(value: Any, fallback: str) -> str:
    sanitized = str(value or "").strip()
    sanitized = "".join(char if char.isalnum() or char in {"_", "-", ".", "(", ")"} else "_" for char in sanitized)
    sanitized = sanitized.strip("_")
    return sanitized or fallback


def build_server_base_url(host_header: str | None) -> str:
    return f"http://{host_header or f'{DEFAULT_HOST}:{DEFAULT_PORT}'}"


def json_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return bool(value)


def first_present(mapping: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    for alias in aliases:
        if alias in mapping and mapping[alias] not in (None, ""):
            return mapping[alias]
    return None


def load_summary(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def build_file_preview(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        content = handle.read()

    lines = content.splitlines()
    preview_lines = lines[:PREVIEW_MAX_LINES]
    preview_text = "\n".join(preview_lines)
    truncated = len(lines) > PREVIEW_MAX_LINES

    if len(preview_text) > PREVIEW_MAX_CHARS:
        preview_text = preview_text[:PREVIEW_MAX_CHARS].rstrip()
        truncated = True

    return {
        "text": preview_text,
        "lineCount": len(lines),
        "truncated": truncated,
    }


def resolve_run_artifact(request_id: str, artifact: str) -> tuple[Path, str]:
    artifact_info = DOWNLOADABLE_ARTIFACTS.get(artifact)
    if not artifact_info:
        raise ValueError(f"Unsupported artifact: {artifact}")

    filename, content_type = artifact_info
    run_root = (RUNS_ROOT / request_id).resolve()
    if not run_root.exists() or RUNS_ROOT.resolve() not in run_root.parents:
        raise FileNotFoundError(f"Unknown run: {request_id}")

    knox_root = run_root / "output" / "knox"
    artifact_paths = list(knox_root.glob(f"*/{filename}"))
    if not artifact_paths:
        raise FileNotFoundError(f"Artifact not found for run {request_id}: {artifact}")

    return artifact_paths[0], content_type


def resolve_generated_knox_bundle(request_id: str) -> dict[str, Path]:
    designs_path, _ = resolve_run_artifact(request_id, "designs")
    part_library_path, _ = resolve_run_artifact(request_id, "partLibrary")
    weight_path, _ = resolve_run_artifact(request_id, "weight")
    summary_path, _ = resolve_run_artifact(request_id, "summary")
    return {
        "designs": designs_path,
        "partLibrary": part_library_path,
        "weight": weight_path,
        "summary": summary_path,
    }


def stage_path_inputs(payload: dict[str, Any], run_root: Path) -> tuple[Path, str, str, str, str]:
    dataio_root = run_root / "dataio"
    dataio_root.mkdir(parents=True, exist_ok=True)

    required = {
        "verilog": payload.get("verilogPath"),
        "ucf": payload.get("ucfPath"),
        "input": payload.get("inputPath"),
        "output": payload.get("outputPath"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError(f"Missing required JSON path fields: {', '.join(sorted(missing))}")

    staged: dict[str, Path] = {}
    for key, raw_path in required.items():
        src = Path(str(raw_path)).expanduser().resolve()
        if not src.exists():
            raise FileNotFoundError(f"{key} source file does not exist: {src}")
        dst = dataio_root / src.name
        shutil.copy2(src, dst)
        staged[key] = dst

    return (
        dataio_root,
        strip_suffix(staged["verilog"].name),
        strip_suffix(staged["ucf"].name),
        strip_suffix(staged["input"].name),
        strip_suffix(staged["output"].name),
    )


def parse_multipart_form(content_type: str, body: bytes) -> dict[str, Any]:
    message = BytesParser(policy=email_policy).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
    )
    fields: dict[str, Any] = {}
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            fields[name] = UploadedFile(filename=Path(filename).name, content=payload)
        else:
            fields[name] = payload.decode(part.get_content_charset() or "utf-8").strip()
    return fields


def stage_form_inputs(form: dict[str, Any], run_root: Path) -> tuple[Path, str, str, str, str]:
    dataio_root = run_root / "dataio"
    dataio_root.mkdir(parents=True, exist_ok=True)

    staged_names: dict[str, str] = {}
    for logical_name, aliases in FIELD_ALIASES.items():
        field = first_present(form, aliases)
        if field is None or not isinstance(field, UploadedFile):
            raise ValueError(f"Missing required uploaded file for {logical_name}")

        filename = field.filename
        target = dataio_root / filename
        with target.open("wb") as handle:
            handle.write(field.content)
        staged_names[logical_name] = strip_suffix(filename)

    return (
        dataio_root,
        staged_names["verilog"],
        staged_names["ucf"],
        staged_names["input"],
        staged_names["output"],
    )


def parse_weight_scores(text: str) -> list[float]:
    if not text.strip():
        return []

    scores: list[float] = []
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header:
        return []

    for row in reader:
        if not row or row[0].strip() == "":
            continue
        try:
            scores.append(float(row[0].strip()))
        except ValueError:
            continue
    return scores


def encode_multipart_formdata(
    fields: list[tuple[str, str]],
    files: list[tuple[str, str, bytes, str]],
) -> tuple[bytes, str]:
    boundary = f"----RuleScapeBoundary{uuid.uuid4().hex}"
    body = bytearray()

    for name, value in fields:
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")

    for field_name, filename, content, content_type in files:
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode(
                "utf-8"
            )
        )
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        body.extend(content)
        body.extend(b"\r\n")

    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def knox_request(
    path: str,
    *,
    method: str = "GET",
    fields: list[tuple[str, str]] | None = None,
    files: list[tuple[str, str, bytes, str]] | None = None,
    timeout: int = KNOX_TIMEOUT_SECONDS,
) -> tuple[int, bytes, str]:
    url = urljoin(DEFAULT_KNOX_BASE_URL.rstrip("/") + "/", path.lstrip("/"))
    data: bytes | None = None
    headers: dict[str, str] = {"Accept": "application/json, text/plain, */*"}

    if files:
        data, content_type = encode_multipart_formdata(fields or [], files)
        headers["Content-Type"] = content_type
    elif fields is not None:
        data = urlencode(fields, doseq=True).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    request = Request(url, data=data, method=method)
    for header_name, header_value in headers.items():
        request.add_header(header_name, header_value)

    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), response.headers.get("Content-Type", "")
    except HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type", "")
    except URLError as exc:
        raise ConnectionError(f"Failed to reach Knox at {DEFAULT_KNOX_BASE_URL}: {exc.reason}") from exc


def knox_expect_success(
    path: str,
    *,
    method: str = "GET",
    fields: list[tuple[str, str]] | None = None,
    files: list[tuple[str, str, bytes, str]] | None = None,
    timeout: int = KNOX_TIMEOUT_SECONDS,
) -> tuple[bytes, str]:
    status, payload, content_type = knox_request(
        path,
        method=method,
        fields=fields,
        files=files,
        timeout=timeout,
    )
    if 200 <= status < 300:
        return payload, content_type

    message = payload.decode("utf-8", errors="replace").strip() or f"Knox request failed with status {status}"
    raise RuntimeError(message)


def knox_expect_json(
    path: str,
    *,
    method: str = "GET",
    fields: list[tuple[str, str]] | None = None,
    files: list[tuple[str, str, bytes, str]] | None = None,
    timeout: int = KNOX_TIMEOUT_SECONDS,
) -> Any:
    payload, _ = knox_expect_success(
        path,
        method=method,
        fields=fields,
        files=files,
        timeout=timeout,
    )
    text = payload.decode("utf-8").strip()
    return json.loads(text) if text else {}


def normalize_knox_evaluation(raw: dict[str, Any]) -> dict[str, Any]:
    design_to_rule = raw.get("designToRule", {}) if isinstance(raw, dict) else {}
    evaluation_results = raw.get("evaluationResults", {}) if isinstance(raw, dict) else {}
    design_ids = [str(value) for value in design_to_rule.get("designIDs", [])]
    scores = [float(value) for value in design_to_rule.get("scores", [])]
    labels = [int(value) for value in design_to_rule.get("labels", [])]
    rule_ids = [key for key in design_to_rule.keys() if key not in RULE_MATRIX_RESERVED_KEYS]

    rules = []
    for rule_id in rule_ids:
        metrics = evaluation_results.get(rule_id, {}) or {}
        rules.append(
            {
                "id": rule_id,
                "impact": metrics.get("impact"),
                "numCorrect": metrics.get("numCorrect"),
                "numIncorrect": metrics.get("numIncorrect"),
                "goodDesignsElim": metrics.get("goodDesignsElim"),
                "poorDesignsElim": metrics.get("poorDesignsElim"),
                "totalDesignsElim": metrics.get("totalDesignsElim"),
                "goodnessPercent": metrics.get("goodnessPercent"),
                "poornessPercent": metrics.get("poornessPercent"),
                "poorEliminationPercent": metrics.get("poorEliminationPercent"),
                "goodPerfection": metrics.get("goodPerfection"),
                "poorPerfection": metrics.get("poorPerfection"),
                "totalPerfection": metrics.get("totalPerfection"),
                "totalImperfection": metrics.get("totalImperfection"),
            }
        )

    rules.sort(
        key=lambda item: (
            -float(item["impact"]) if item.get("impact") is not None else 0.0,
            item["id"],
        )
    )

    designs = []
    for index, design_id in enumerate(design_ids):
        rule_statuses = {}
        passed_count = 0
        eliminated_count = 0
        for rule_id in rule_ids:
            values = design_to_rule.get(rule_id, [])
            raw_value = values[index] if index < len(values) else None
            status = None if raw_value is None else int(raw_value)
            rule_statuses[rule_id] = status
            if status == 0:
                passed_count += 1
            elif status == 1:
                eliminated_count += 1

        designs.append(
            {
                "designId": design_id,
                "score": scores[index] if index < len(scores) else None,
                "label": labels[index] if index < len(labels) else None,
                "ruleStatuses": rule_statuses,
                "passedCount": passed_count,
                "eliminatedCount": eliminated_count,
            }
        )

    return {
        "designCount": len(designs),
        "ruleCount": len(rules),
        "designs": designs,
        "rules": rules,
        "raw": raw,
    }


def write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def build_cello_response_payload(
    request_id: str,
    dataio_root: Path,
    result: Any,
    host_header: str | None,
) -> dict[str, Any]:
    summary_path = result.knox_output_dir / "adapter_result.json"
    designs_path = result.knox_output_dir / "designs.csv"
    part_library_path = result.knox_output_dir / "part_library.csv"
    weight_path = result.knox_output_dir / "weight.csv"
    server_base_url = build_server_base_url(host_header)

    return {
        "requestId": request_id,
        "status": "completed",
        "runId": result.run_id,
        "requestedTopN": result.requested_top_n,
        "returnedTopN": len(result.candidates),
        "bestScore": result.candidates[0].score,
        "searchMode": result.search_mode,
        "dataioRoot": str(dataio_root),
        "celloOutputDir": str(result.cello_output_dir),
        "knoxOutputDir": str(result.knox_output_dir),
        "files": {
            "designs": str(designs_path),
            "partLibrary": str(part_library_path),
            "weight": str(weight_path),
            "summary": str(summary_path),
        },
        "downloads": {
            artifact: (
                f"{server_base_url}/api/pipeline/download"
                f"?requestId={request_id}&artifact={artifact}"
            )
            for artifact in DOWNLOADABLE_ARTIFACTS
        },
        "filePreviews": {
            "designs": build_file_preview(designs_path),
            "partLibrary": build_file_preview(part_library_path),
            "weight": build_file_preview(weight_path),
            "summary": build_file_preview(summary_path),
        },
        "summary": load_summary(summary_path),
    }


def execute_knox_run(payload: dict[str, Any], request_id: str, run_root: Path) -> dict[str, Any]:
    action = str(payload.get("action", "import")).strip().lower() or "import"
    if action not in {"import", "evaluate"}:
        raise ValueError("Knox action must be 'import' or 'evaluate'.")

    bundle_source = str(payload.get("bundleSource", "generated")).strip().lower()
    output_space_prefix = sanitize_identifier(
        payload.get("outputSpacePrefix"),
        f"rulescape_knox_{request_id}",
    )
    design_group_id = sanitize_identifier(
        payload.get("designGroupId"),
        f"{output_space_prefix}_designs",
    )
    default_weight = str(payload.get("defaultWeight", "0.0")).strip() or "0.0"
    rule_space_id = sanitize_identifier(
        payload.get("ruleSpaceId"),
        f"{output_space_prefix}_rules",
    )
    rules_group_id = sanitize_identifier(
        payload.get("rulesGroupId"),
        f"{output_space_prefix}_rules_group",
    )
    evaluation_name = sanitize_identifier(
        payload.get("evaluationName"),
        f"{output_space_prefix}_evaluation",
    )
    labeling_method = str(payload.get("labelingMethod", "median")).strip().lower() or "median"
    if labeling_method not in {"median", "sign"}:
        raise ValueError("Knox labelingMethod must be 'median' or 'sign'.")

    bundle_files: list[tuple[str, str, bytes, str]] = []
    weight_text = ""
    source_summary: dict[str, Any] = {"bundleSource": bundle_source}

    if bundle_source == "generated":
        cello_request_id = str(payload.get("celloRequestId", "")).strip()
        if not cello_request_id:
            raise ValueError("celloRequestId is required when bundleSource is 'generated'.")

        generated_bundle = resolve_generated_knox_bundle(cello_request_id)
        if action == "import":
            bundle_files.extend(
                [
                    ("inputCSVFiles[]", "designs.csv", generated_bundle["designs"].read_bytes(), "text/csv"),
                    (
                        "inputCSVFiles[]",
                        "part_library.csv",
                        generated_bundle["partLibrary"].read_bytes(),
                        "text/csv",
                    ),
                    ("inputCSVFiles[]", "weight.csv", generated_bundle["weight"].read_bytes(), "text/csv"),
                ]
            )
        weight_text = generated_bundle["weight"].read_text(encoding="utf-8")
        source_summary.update(
            {
                "celloRequestId": cello_request_id,
                "generatedFiles": {
                    "designs": str(generated_bundle["designs"]),
                    "partLibrary": str(generated_bundle["partLibrary"]),
                    "weight": str(generated_bundle["weight"]),
                    "summary": str(generated_bundle["summary"]),
                },
            }
        )
    elif bundle_source == "uploaded":
        uploaded_bundle = payload.get("uploadedBundle", {}) or {}
        designs_text = str(uploaded_bundle.get("designs", ""))
        part_library_text = str(uploaded_bundle.get("partLibrary", ""))
        weight_text = str(uploaded_bundle.get("weight", ""))

        if action == "import" and (not designs_text.strip() or not part_library_text.strip()):
            raise ValueError("uploadedBundle.designs and uploadedBundle.partLibrary are required.")

        if action == "import":
            bundle_files.extend(
                [
                    ("inputCSVFiles[]", "designs.csv", designs_text.encode("utf-8"), "text/csv"),
                    ("inputCSVFiles[]", "part_library.csv", part_library_text.encode("utf-8"), "text/csv"),
                ]
            )
            if weight_text.strip():
                bundle_files.append(("inputCSVFiles[]", "weight.csv", weight_text.encode("utf-8"), "text/csv"))

        source_summary["uploadedBundle"] = {
            "hasWeight": bool(weight_text.strip()),
        }
    else:
        raise ValueError("bundleSource must be 'generated' or 'uploaded'.")

    if action == "import":
        knox_expect_success(
            "/import/csv",
            method="POST",
            fields=[
                ("outputSpacePrefix", output_space_prefix),
                ("groupID", design_group_id),
                ("weight", default_weight),
            ],
            files=bundle_files,
        )

    design_space_ids = knox_expect_json(f"/designSpace/listGroupSpaces?groupID={quote(design_group_id)}")
    design_space_ids = [str(space_id) for space_id in (design_space_ids or [])]
    if not design_space_ids:
        raise ValueError(f"No Knox design spaces found for design group '{design_group_id}'. Import the bundle first.")

    goldbar = str(payload.get("goldbar", ""))
    categories = str(payload.get("categories", ""))
    design_scores = parse_weight_scores(weight_text)
    evaluation_payload: dict[str, Any] = {
        "executed": False,
        "reason": "Import only requested. Add Goldbar and categories, then run Evaluate Rules.",
        "evaluationName": evaluation_name,
        "ruleSpaceId": rule_space_id,
        "rulesGroupId": rules_group_id,
        "labelingMethod": labeling_method,
        "designScoresProvided": len(design_scores),
    }

    if action == "evaluate":
        if not goldbar.strip() or not categories.strip():
            raise ValueError("Goldbar and categories are required to evaluate rules.")

        knox_expect_success(
            "/goldbar/import",
            method="POST",
            fields=[
                ("goldbar", goldbar),
                ("categories", categories),
                ("outputSpaceID", rule_space_id),
                ("groupID", rules_group_id),
                ("verbose", "true" if json_bool(payload.get("verbose"), default=False) else "false"),
            ],
        )

        rule_space_ids = knox_expect_json(f"/designSpace/listGroupSpaces?groupID={quote(rules_group_id)}")
        rule_space_ids = [str(space_id) for space_id in (rule_space_ids or [])]

        evaluation_fields: list[tuple[str, str]] = [
            ("evaluationName", evaluation_name),
            ("designGroupID", design_group_id),
            ("rulesGroupID", rules_group_id),
            ("labelingMethod", labeling_method),
        ]
        for score in design_scores:
            evaluation_fields.append(("designScores", str(score)))

        raw_evaluation = knox_expect_json(
            "/rule/evaluate",
            method="POST",
            fields=evaluation_fields,
            timeout=max(KNOX_TIMEOUT_SECONDS, 120),
        )
        normalized_evaluation = normalize_knox_evaluation(raw_evaluation)
        top_rule = normalized_evaluation["rules"][0] if normalized_evaluation["rules"] else None

        evaluation_payload = {
            "executed": True,
            "evaluationName": evaluation_name,
            "ruleSpaceId": rule_space_id,
            "rulesGroupId": rules_group_id,
            "ruleSpaceIds": rule_space_ids,
            "labelingMethod": labeling_method,
            "designScoresProvided": len(design_scores),
            "topRule": top_rule,
            **normalized_evaluation,
        }

    response_payload = {
        "requestId": request_id,
        "status": "completed",
        "service": "knox",
        "action": action,
        "knoxBaseUrl": DEFAULT_KNOX_BASE_URL,
        "import": {
            **source_summary,
            "outputSpacePrefix": output_space_prefix,
            "designGroupId": design_group_id,
            "designSpaceIds": design_space_ids,
            "designCount": len(design_space_ids),
            "defaultWeight": default_weight,
        },
        "evaluation": evaluation_payload,
    }

    write_json_file(run_root / "output" / "knox_runtime" / "knox_result.json", response_payload)
    return response_payload


class PipelineRequestHandler(BaseHTTPRequestHandler):
    server_version = "CelloKnoxPipeline/0.2"

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(encoded)

    def send_file(self, path: Path, content_type: str) -> None:
        payload = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/api/pipeline/health":
            self.send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "service": "cello-knox-pipeline",
                    "runsRoot": str(RUNS_ROOT),
                },
            )
            return

        if parsed.path == "/api/pipeline/knox/health":
            try:
                knox_expect_json("/designSpace/list")
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "service": "knox",
                        "baseUrl": DEFAULT_KNOX_BASE_URL,
                    },
                )
            except Exception as exc:
                self.send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "status": "error",
                        "service": "knox",
                        "baseUrl": DEFAULT_KNOX_BASE_URL,
                        "error": str(exc),
                    },
                )
            return

        if parsed.path == "/api/pipeline/download":
            try:
                query = parse_qs(parsed.query)
                request_id = (query.get("requestId") or [""])[0].strip()
                artifact = (query.get("artifact") or [""])[0].strip()
                if not request_id or not artifact:
                    raise ValueError("requestId and artifact are required")

                path, content_type = resolve_run_artifact(request_id, artifact)
                self.send_file(path, content_type)
                return
            except ValueError as exc:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except FileNotFoundError as exc:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return

        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not Found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/api/pipeline/cello-knox/run":
            self.handle_cello_run()
            return

        if parsed.path == "/api/pipeline/knox/run":
            self.handle_knox_run()
            return

        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not Found"})

    def handle_cello_run(self) -> None:
        request_id = make_request_id()
        run_root = RUNS_ROOT / request_id
        run_root.mkdir(parents=True, exist_ok=True)

        try:
            content_type = self.headers.get("Content-Type", "")
            if content_type.startswith("application/json"):
                content_length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(content_length)
                payload = json.loads(raw_body.decode("utf-8") or "{}")
                (
                    dataio_root,
                    verilog_name,
                    ucf_name,
                    input_name,
                    output_name,
                ) = stage_path_inputs(payload, run_root)
                top_n = int(payload.get("topN", payload.get("top_n", 1)))
                iterations = int(payload.get("iterations", 25))
                search_mode = normalize_search_mode(payload.get("search", payload.get("searchMode")))
                verbose = json_bool(payload.get("verbose"), default=False)
            elif content_type.startswith("multipart/form-data"):
                content_length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(content_length)
                form = parse_multipart_form(content_type, raw_body)
                (
                    dataio_root,
                    verilog_name,
                    ucf_name,
                    input_name,
                    output_name,
                ) = stage_form_inputs(form, run_root)
                top_n = int(first_present(form, ("topN", "top_n")) or 1)
                iterations = int(first_present(form, ("iterations",)) or 25)
                search_mode = normalize_search_mode(first_present(form, ("search", "searchMode")))
                verbose = json_bool(first_present(form, ("verbose",)), default=False)
            else:
                raise ValueError("Unsupported Content-Type. Use application/json or multipart/form-data.")

            result = run_pipeline(
                classic_root=run_root,
                dataio_root=dataio_root,
                verilog_name=verilog_name,
                ucf_name=ucf_name,
                input_name=input_name,
                output_name=output_name,
                iterations=iterations,
                top_n=top_n,
                search_mode=search_mode,
                verbose=verbose,
            )

            self.send_json(
                HTTPStatus.OK,
                build_cello_response_payload(request_id, dataio_root, result, self.headers.get("Host")),
            )
        except ValueError as exc:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"status": "error", "requestId": request_id, "error": str(exc)},
            )
        except Exception as exc:
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "status": "error",
                    "requestId": request_id,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )

    def handle_knox_run(self) -> None:
        request_id = make_request_id()
        run_root = RUNS_ROOT / request_id
        run_root.mkdir(parents=True, exist_ok=True)

        try:
            content_type = self.headers.get("Content-Type", "")
            if not content_type.startswith("application/json"):
                raise ValueError("Knox runs currently require application/json payloads.")

            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length)
            payload = json.loads(raw_body.decode("utf-8") or "{}")
            result = execute_knox_run(payload, request_id, run_root)
            self.send_json(HTTPStatus.OK, result)
        except ValueError as exc:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"status": "error", "requestId": request_id, "error": str(exc)},
            )
        except ConnectionError as exc:
            self.send_json(
                HTTPStatus.BAD_GATEWAY,
                {"status": "error", "requestId": request_id, "error": str(exc)},
            )
        except RuntimeError as exc:
            self.send_json(
                HTTPStatus.BAD_GATEWAY,
                {"status": "error", "requestId": request_id, "error": str(exc)},
            )
        except Exception as exc:
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "status": "error",
                    "requestId": request_id,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((host, port), PipelineRequestHandler)
    print(f"Cello-Knox pipeline server listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    host = os.environ.get("CELLO_KNOX_HOST", DEFAULT_HOST)
    port = int(os.environ.get("CELLO_KNOX_PORT", DEFAULT_PORT))
    run_server(host=host, port=port)
