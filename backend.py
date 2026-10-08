import os
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = BASE_DIR / "scripts"
DATA_INPUT_DIR = BASE_DIR / "data-input"
DATA_OUTPUT_DIR = BASE_DIR / "data-output"

for folder in [SCRIPTS_DIR, DATA_INPUT_DIR, DATA_OUTPUT_DIR]:
    folder.mkdir(exist_ok=True)

app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path="")


def discover_scripts():
    files = []
    for root in [SCRIPTS_DIR, BASE_DIR]:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if any(part in {".git", "__pycache__", ".venv"} for part in path.parts):
                continue
            if path.name in {"backend.py"}:
                continue
            files.append(path.relative_to(BASE_DIR).as_posix())
    return sorted(set(files))


def infer_type(value):
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, list):
        return "list"
    return "string"


def extract_params(script_path: Path):
    try:
        source = script_path.read_text(encoding="utf-8")
    except Exception:
        return {"params": []}

    try:
        tree = ast.parse(source)
    except SyntaxError:
        tree = None

    if tree is not None:
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "PARAMS":
                        value = ast.literal_eval(node.value)
                        if isinstance(value, dict):
                            params = []
                            for name, meta in value.items():
                                if isinstance(meta, dict):
                                    params.append({
                                        "name": str(name),
                                        "type": meta.get("type", infer_type(meta.get("default", meta.get("value", "")))),
                                        "default": meta.get("default", meta.get("value", "")),
                                        "min": meta.get("min"),
                                        "max": meta.get("max"),
                                        "options": meta.get("options", []),
                                    })
                                else:
                                    params.append({
                                        "name": str(name),
                                        "type": infer_type(meta),
                                        "default": meta,
                                    })
                            return {"params": params}
                        if isinstance(value, list):
                            params = []
                            for item in value:
                                if isinstance(item, dict):
                                    params.append({
                                        "name": item.get("name", "param"),
                                        "type": item.get("type", "string"),
                                        "default": item.get("default", ""),
                                        "min": item.get("min"),
                                        "max": item.get("max"),
                                        "options": item.get("options", []),
                                    })
                            return {"params": params}

    argparse_matches = re.findall(
        r"add_argument\(\s*['\"](--?\w+)['\"]\s*(?:,\s*.*?default\s*=\s*([^,\)]+))?",
        source,
    )
    if argparse_matches:
        params = []
        for name, default_value in argparse_matches:
            params.append({
                "name": name.lstrip('-'),
                "type": "string",
                "default": default_value.strip(" \"'") if default_value else "",
            })
        return {"params": params}

    return {"params": []}


def list_files_in(folder_name: str):
    base = DATA_INPUT_DIR if folder_name == "data-input" else DATA_OUTPUT_DIR
    items = []
    for path in sorted(base.rglob("*")):
        if path.name == ".gitkeep":
            continue
        if path.is_file():
            items.append({
                "name": path.name,
                "path": path.relative_to(BASE_DIR).as_posix(),
                "type": path.suffix.lower().replace('.', ''),
            })
    return items


@app.route("/")
def root():
    return send_from_directory(str(BASE_DIR), "index.html")


@app.route("/api/scripts")
def api_scripts():
    return jsonify(discover_scripts())


@app.route("/api/params")
def api_params():
    script_name = request.args.get("script", "")
    script_path = BASE_DIR / script_name
    if not script_path.exists():
        return jsonify({"params": []})
    return jsonify(extract_params(script_path))


@app.route("/api/files")
def api_files():
    folder_name = request.args.get("folder", "data-input")
    return jsonify({"files": list_files_in(folder_name)})


@app.route("/api/upload-dataset", methods=["POST"]) 
def upload_dataset():
    file = request.files.get("file")
    if file is None:
        return jsonify({"ok": False, "error": "No se recibió ningún archivo."}), 400

    target = DATA_INPUT_DIR / file.filename
    file.save(target)
    return jsonify({"ok": True, "file": file.filename})


@app.route("/api/run", methods=["POST"]) 
def api_run():
    payload = request.get_json(silent=True) or {}
    script_name = payload.get("script", "")
    params = payload.get("params", {}) or {}
    dataset = payload.get("dataset", "") or ""

    if not script_name:
        return jsonify({"error": "No se especificó script."}), 400

    script_path = (BASE_DIR / script_name).resolve()
    if not script_path.exists():
        return jsonify({"error": "Script no encontrado."}), 404

    command = [sys.executable, str(script_path)]
    for key, value in params.items():
        if isinstance(value, bool):
            if value:
                command.append(f"--{key}")
        else:
            command.extend([f"--{key}", str(value)])

    dataset_path = (BASE_DIR / dataset).resolve() if dataset else ""
    if dataset_path and dataset_path.exists():
        command.extend(["--dataset", str(dataset_path)])

    env = os.environ.copy()
    env["DATASET_PATH"] = str(dataset_path) if dataset_path else ""

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            cwd=str(BASE_DIR),
            timeout=180,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return jsonify({"error": "El script tardó demasiado en ejecutarse.", "stderr": str(exc)})

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    try:
        parsed_json = json.loads(stdout) if stdout else None
    except json.JSONDecodeError:
        parsed_json = None

    return jsonify({
        "ok": result.returncode == 0,
        "stdout": stdout,
        "stderr": stderr,
        "json": parsed_json,
        "files": list_files_in("data-output"),
    })


@app.route("/files/<path:file_path>")
def serve_file(file_path):
    full_path = (BASE_DIR / file_path).resolve()
    if not str(full_path).startswith(str(BASE_DIR)):
        return jsonify({"error": "Ruta no permitida"}), 403
    if not full_path.exists() or not full_path.is_file():
        return jsonify({"error": "Archivo no encontrado"}), 404
    return send_from_directory(str(BASE_DIR), file_path, as_attachment=False)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
