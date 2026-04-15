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
from urllib.parse import urlparse

from classic_single_input_adapter import run_pipeline


SERVER_ROOT = Path(__file__).resolve().parent
RUNS_ROOT = SERVER_ROOT / "pipeline_runs"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8051

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


class PipelineRequestHandler(BaseHTTPRequestHandler):
    server_version = "CelloKnoxPipeline/0.1"

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

        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not Found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/pipeline/cello-knox/run":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not Found"})
            return

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
                verbose=verbose,
            )

            summary_path = result.knox_output_dir / "adapter_result.json"
            self.send_json(
                HTTPStatus.OK,
                {
                    "requestId": request_id,
                    "status": "completed",
                    "runId": result.run_id,
                    "requestedTopN": result.requested_top_n,
                    "returnedTopN": len(result.candidates),
                    "bestScore": result.candidates[0].score,
                    "dataioRoot": str(dataio_root),
                    "celloOutputDir": str(result.cello_output_dir),
                    "knoxOutputDir": str(result.knox_output_dir),
                    "files": {
                        "designs": str(result.knox_output_dir / "designs.csv"),
                        "partLibrary": str(result.knox_output_dir / "part_library.csv"),
                        "weight": str(result.knox_output_dir / "weight.csv"),
                        "summary": str(summary_path),
                    },
                    "summary": load_summary(summary_path),
                },
            )
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"status": "error", "requestId": request_id, "error": str(exc)})
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
