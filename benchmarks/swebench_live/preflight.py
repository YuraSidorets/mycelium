#!/usr/bin/env python3
"""Probe the pinned Codex app-server without starting a model turn."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

try:
    from . import pilot
except ImportError:  # Direct script execution puts this directory on sys.path.
    import pilot


SCHEMA = "mycelium.swebench-live-runtime-preflight/v2"
STATUS = "STRUCTURAL_READY_NO_MODEL_CALL"
PACKAGE = f"@openai/codex@{pilot.CODEX_VERSION}"
MODEL_FIELDS = ("provider", "requested", "revision", "reasoning_effort")
REQUIRED_NOTIFICATION_METHODS = (
    "thread/tokenUsage/updated",
    "model/rerouted",
)
RPC_METHODS = (
    "initialize",
    "initialized",
    "model/list",
    "config/read",
    "modelProvider/capabilities/read",
    "experimentalFeature/list",
    "account/read",
)


class PreflightError(ValueError):
    pass


def _object_without_duplicates(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise PreflightError(f"duplicate JSON key: {key}")
        value[key] = child
    return value


def _loads(value, label):
    try:
        return json.loads(value, object_pairs_hook=_object_without_duplicates)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise PreflightError(f"{label} returned malformed JSON") from exc


def _sha256(value):
    return hashlib.sha256(value).hexdigest()


def _file_sha256(path):
    try:
        return _sha256(Path(path).read_bytes())
    except OSError as exc:
        raise PreflightError(f"cannot hash {path}: {exc}") from exc


def _stream_evidence(value):
    data = value if isinstance(value, bytes) else value.encode("utf-8", errors="replace")
    return {"bytes": len(data), "sha256": _sha256(data)}


def _process_evidence(completed):
    return {
        "returncode": completed.returncode,
        "stdout": _stream_evidence(completed.stdout),
        "stderr": _stream_evidence(completed.stderr),
    }


def _document_sha256(document):
    unsigned = dict(document)
    unsigned.pop("receipt_sha256", None)
    data = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256(data)


def _model_contract(model):
    if not isinstance(model, dict) or set(model) != set(MODEL_FIELDS) or any(
        not isinstance(model[field], str) or not model[field].strip() for field in MODEL_FIELDS
    ):
        raise PreflightError(f"model contract fields must be exactly {list(MODEL_FIELDS)}")
    return {field: model[field] for field in MODEL_FIELDS}


def _run(command, *, timeout):
    try:
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreflightError(f"cannot execute {' '.join(command)}: {exc}") from exc
    if completed.returncode != 0:
        raise PreflightError(f"command exited nonzero ({completed.returncode}): {' '.join(command)}")
    return completed


def _default_commands():
    npm = shutil.which("npm")
    npx = shutil.which("npx")
    if not npm or not npx:
        raise PreflightError("npm and npx are required for the pinned Codex runtime")
    return [npm], [npx, "--yes", "--package", PACKAGE, "codex"]


def _local_path(value, timeout):
    if os.name != "nt" and len(value) > 2 and value[1] == ":" and value[2] in "\\/":
        value = _run(["wslpath", "-u", value], timeout=timeout).stdout.strip()
    return Path(value)


def _resolve_platform_root(codex_command, timeout):
    if codex_command[-1:] != ["codex"]:
        raise PreflightError("custom Codex commands must provide platform_root")
    completed = _run(
        [
            *codex_command[:-1],
            "node",
            "-p",
            "process.env.PATH.split(require('path').delimiter)[0]",
        ],
        timeout=timeout,
    )
    binary_directory = _local_path(completed.stdout.strip(), timeout)
    package_scope = binary_directory.parent / "@openai"
    candidates = sorted(
        path for path in package_scope.glob("codex-*")
        if (path / "package.json").is_file() and (path / "vendor").is_dir()
    )
    if len(candidates) != 1:
        raise PreflightError("cannot resolve exactly one installed Codex platform package")
    return candidates[0]


def _platform_receipt(platform_root, npm_command, timeout):
    package_path = Path(platform_root) / "package.json"
    try:
        package = _loads(package_path.read_text(encoding="utf-8"), "Codex platform package")
    except OSError as exc:
        raise PreflightError(f"cannot read Codex platform package: {exc}") from exc
    if not isinstance(package, dict) or package.get("name") != "@openai/codex":
        raise PreflightError("Codex platform package metadata is invalid")
    version = package.get("version")
    expected = pilot.CODEX_PLATFORM_PACKAGES.get(version)
    if not expected:
        raise PreflightError(f"Codex platform package is not frozen: {version}")
    completed = _run(
        [*npm_command, "view", f"@openai/codex@{version}", "version", "dist.integrity", "--json"],
        timeout=timeout,
    )
    observed = _loads(completed.stdout, "npm platform package view")
    if observed != {"version": version, "dist.integrity": expected["integrity"]}:
        raise PreflightError("Codex platform package registry identity does not match the frozen runtime")

    binaries = [
        path for path in (Path(platform_root) / "vendor").rglob("codex*")
        if path.parent.name == "bin" and path.name in {"codex", "codex.exe"}
        and path.is_file() and not path.is_symlink()
    ]
    if len(binaries) != 1:
        raise PreflightError("Codex platform package must contain exactly one native executable")
    binary = binaries[0]
    binary_sha256 = _file_sha256(binary)
    if binary_sha256 != expected["binary_sha256"]:
        raise PreflightError("Codex native executable does not match the frozen binary digest")
    return {
        "version": version,
        "integrity": observed["dist.integrity"],
        "package_json_sha256": _file_sha256(package_path),
        "binary": {
            "path": str(binary.resolve()),
            "bytes": binary.stat().st_size,
            "sha256": binary_sha256,
        },
        "process": _process_evidence(completed),
    }


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _validate_protocol_schema(document):
    definitions = document.get("definitions") if isinstance(document, dict) else None
    if not isinstance(definitions, dict):
        raise PreflightError("Codex app-server schema has no definitions")

    raw_response = definitions.get("RawResponseCompletedNotification")
    raw_properties = raw_response.get("properties") if isinstance(raw_response, dict) else None
    raw_required = raw_response.get("required") if isinstance(raw_response, dict) else None
    usage = raw_properties.get("usage") if isinstance(raw_properties, dict) else None
    usage_options = usage.get("anyOf") if isinstance(usage, dict) else None
    if not isinstance(raw_properties, dict) or not {"responseId", "threadId", "turnId", "usage"} <= set(raw_properties) \
            or not isinstance(raw_required, list) \
            or not {"responseId", "threadId", "turnId"} <= set(raw_required) \
            or usage_options != [
                {"$ref": "#/definitions/TokenUsageBreakdown"},
                {"type": "null"},
            ]:
        raise PreflightError("Codex app-server schema has no upstream usage DTO shape")

    token_usage = definitions.get("TokenUsageBreakdown")
    token_properties = token_usage.get("properties") if isinstance(token_usage, dict) else None
    token_required = token_usage.get("required") if isinstance(token_usage, dict) else None
    required_tokens = {
        "cachedInputTokens", "inputTokens", "outputTokens", "reasoningOutputTokens", "totalTokens",
    }
    if not isinstance(token_usage, dict) or token_usage.get("type") != "object" \
            or not isinstance(token_properties, dict) \
            or not required_tokens <= set(token_properties) \
            or not isinstance(token_required, list) or not required_tokens <= set(token_required) \
            or any(token_properties[field].get("type") != "integer" for field in required_tokens):
        raise PreflightError("Codex app-server schema has no exact token-usage breakdown")

    thread = definitions.get("Thread")
    thread_properties = thread.get("properties") if isinstance(thread, dict) else None
    if not isinstance(thread_properties, dict) or "parentThreadId" not in thread_properties:
        raise PreflightError("Codex app-server schema has no parent thread identity")

    thread_settings = definitions.get("ThreadSettings")
    settings_properties = thread_settings.get("properties") if isinstance(thread_settings, dict) else None
    if not isinstance(settings_properties, dict) or not {"model", "modelProvider", "effort"} <= set(settings_properties):
        raise PreflightError("Codex app-server schema has no effective model settings")

    rerouted = definitions.get("ModelReroutedNotification")
    rerouted_required = rerouted.get("required") if isinstance(rerouted, dict) else None
    if not isinstance(rerouted_required, list) \
            or not {"threadId", "turnId", "fromModel", "toModel", "reason"} <= set(rerouted_required):
        raise PreflightError("Codex app-server schema has no model-reroute evidence")

    collab = next(
        (
            node for node in _walk(document)
            if isinstance(node.get("properties"), dict)
            and node["properties"].get("type", {}).get("enum") == ["collabAgentToolCall"]
        ),
        None,
    )
    if not collab or not {"senderThreadId", "receiverThreadIds"} <= set(collab.get("required", [])):
        raise PreflightError("Codex app-server schema has no collaboration lineage item")
    collab_properties = collab["properties"]
    if collab_properties["senderThreadId"].get("type") != "string":
        raise PreflightError("Codex collaboration sender identity is not a string")
    receivers = collab_properties["receiverThreadIds"]
    if receivers.get("type") != "array" or receivers.get("items", {}).get("type") != "string":
        raise PreflightError("Codex collaboration receiver identities are not a string array")

    enum_values = {
        item for node in _walk(document) for item in node.get("enum", [])
        if isinstance(item, str)
    }
    missing = set(REQUIRED_NOTIFICATION_METHODS) - enum_values
    if missing:
        raise PreflightError(f"Codex app-server schema is missing required notifications: {sorted(missing)}")


def _package_receipt(npm_command, timeout):
    completed = _run(
        [*npm_command, "view", PACKAGE, "version", "dist.integrity", "--json"],
        timeout=timeout,
    )
    observed = _loads(completed.stdout, "npm view")
    expected = {"version": pilot.CODEX_VERSION, "dist.integrity": pilot.CODEX_NPM_INTEGRITY}
    if observed != expected:
        raise PreflightError("npm package identity does not match the frozen Codex runtime")
    return {
        "name": "@openai/codex",
        "version": observed["version"],
        "integrity": observed["dist.integrity"],
        "process": _process_evidence(completed),
    }


def _schema_receipt(codex_command, timeout, artifact_directory):
    launcher = Path(codex_command[0]).as_posix()
    windows_interop = os.name != "nt" and launcher.startswith("/mnt/")
    temporary_parent = Path.cwd() if windows_interop else None
    with tempfile.TemporaryDirectory(prefix="mycelium-codex-schema-", dir=temporary_parent) as directory:
        schema_argument = directory
        if windows_interop:
            schema_argument = _run(["wslpath", "-w", directory], timeout=timeout).stdout.strip()
        completed = _run(
            [*codex_command, "app-server", "generate-json-schema", "--out", schema_argument],
            timeout=timeout,
        )
        bundle = Path(directory) / "codex_app_server_protocol.v2.schemas.json"
        try:
            text = bundle.read_text(encoding="utf-8")
        except OSError as exc:
            raise PreflightError(f"Codex did not generate the v2 app-server schema: {exc}") from exc
        document = _loads(text, "Codex app-server schema")
        _validate_protocol_schema(document)
        artifact_path = None
        if artifact_directory is not None:
            target = Path(artifact_directory) / bundle.name
            try:
                with target.open("xb") as stream:
                    stream.write(bundle.read_bytes())
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as exc:
                raise PreflightError(f"cannot retain Codex app-server schema: {exc}") from exc
            artifact_path = f"{Path(artifact_directory).name}/{target.name}"
        return {
            "artifact_path": artifact_path,
            "bytes": len(text.encode("utf-8")),
            "sha256": _file_sha256(bundle),
            "requirements": {
                "collaboration_lineage": True,
                "effective_model_settings": True,
                "parent_thread_identity": True,
                "upstream_usage_dto_shape": True,
                "notification_methods": list(REQUIRED_NOTIFICATION_METHODS),
            },
            "process": _process_evidence(completed),
        }


def _rpc_receipt(codex_command, model, config_cwd, timeout):
    model = _model_contract(model)
    config_cwd = str(Path(config_cwd).resolve())
    deadline = time.monotonic() + timeout
    stderr = tempfile.TemporaryFile()
    try:
        process = subprocess.Popen(
            [*codex_command, "app-server", "--stdio"],
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            bufsize=1,
        )
    except OSError as exc:
        stderr.close()
        raise PreflightError(f"cannot start Codex app-server: {exc}") from exc

    responses = queue.Queue()

    def read_stdout():
        try:
            for line in process.stdout:
                if line.strip():
                    responses.put(_loads(line, "Codex app-server"))
        except BaseException as exc:  # surfaced on the calling thread
            responses.put(exc)
        finally:
            responses.put(None)

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    sent = []

    def send(document):
        if process.stdin is None:
            raise PreflightError("Codex app-server stdin is unavailable")
        encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        try:
            process.stdin.write(encoded + "\n")
            process.stdin.flush()
        except OSError as exc:
            raise PreflightError(f"cannot write Codex app-server request: {exc}") from exc
        sent.append(_loads(encoded, "Codex app-server request"))

    def receive(request_id):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PreflightError(f"Codex app-server timed out waiting for response {request_id}")
            try:
                response = responses.get(timeout=remaining)
            except queue.Empty as exc:
                raise PreflightError(f"Codex app-server timed out waiting for response {request_id}") from exc
            if response is None:
                raise PreflightError(f"Codex app-server closed before response {request_id}")
            if isinstance(response, BaseException):
                raise PreflightError(f"cannot read Codex app-server output: {response}") from response
            if not isinstance(response, dict):
                raise PreflightError("Codex app-server returned a non-object JSON message")
            if response.get("id") != request_id:
                continue
            if response.get("error"):
                raise PreflightError(f"Codex app-server request {request_id} failed")
            if not isinstance(response.get("result"), dict):
                raise PreflightError(f"Codex app-server response {request_id} has no result object")
            return response

    try:
        send({
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {"name": "mycelium_preflight", "title": "Mycelium Preflight", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        })
        initialized = receive(1)
        send({"method": "initialized", "params": {}})
        send({"method": "model/list", "id": 2, "params": {}})
        models = receive(2)
        send({"method": "config/read", "id": 3, "params": {"cwd": config_cwd, "includeLayers": False}})
        config = receive(3)
        send({"method": "modelProvider/capabilities/read", "id": 4, "params": {}})
        provider = receive(4)
        send({"method": "experimentalFeature/list", "id": 5, "params": {}})
        features = receive(5)
        send({"method": "account/read", "id": 6, "params": {"refreshToken": True}})
        account = receive(6)
    finally:
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        reader.join(timeout=1)
        if process.stdout is not None:
            process.stdout.close()
        stderr.seek(0)
        stderr_bytes = stderr.read()
        stderr.close()
    if process.returncode != 0:
        raise PreflightError(f"Codex app-server exited nonzero: {process.returncode}")

    init_result = initialized["result"]
    user_agent = init_result.get("userAgent")
    if not isinstance(user_agent, str) or pilot.CODEX_VERSION not in user_agent:
        raise PreflightError("Codex app-server user agent does not match the frozen runtime")

    model_rows = models["result"].get("data")
    selected_model = next(
        (
            row for row in model_rows
            if isinstance(row, dict) and row.get("model") == model["requested"]
        ),
        None,
    ) if isinstance(model_rows, list) else None
    effort_rows = selected_model.get("supportedReasoningEfforts") if isinstance(selected_model, dict) else None
    supported_efforts = [
        row.get("reasoningEffort") for row in effort_rows if isinstance(row, dict)
    ] if isinstance(effort_rows, list) else None
    if not isinstance(selected_model, dict) or not isinstance(selected_model.get("id"), str) \
            or not isinstance(supported_efforts, list) or model["reasoning_effort"] not in supported_efforts \
            or any(not isinstance(value, str) for value in supported_efforts):
        raise PreflightError("requested model or reasoning effort is unavailable")

    config_result = config["result"].get("config")
    if not isinstance(config_result, dict):
        raise PreflightError("Codex app-server returned no effective config")
    configured_provider = config_result.get("model_provider")
    effective_defaults = {
        "provider": configured_provider or "openai",
        "model": config_result.get("model"),
        "reasoning_effort": config_result.get("model_reasoning_effort"),
    }
    if effective_defaults != {
        "provider": model["provider"],
        "model": model["requested"],
        "reasoning_effort": model["reasoning_effort"],
    }:
        raise PreflightError("Codex effective model defaults do not match the frozen model contract")

    provider_capabilities = provider["result"]
    capability_fields = {"imageGeneration", "namespaceTools", "webSearch"}
    if set(provider_capabilities) != capability_fields \
            or any(not isinstance(value, bool) for value in provider_capabilities.values()):
        raise PreflightError("Codex app-server returned invalid provider capabilities")

    feature_rows = features["result"].get("data")
    if not isinstance(feature_rows, list):
        raise PreflightError("Codex app-server returned no feature catalog")
    multi_agent = next(
        (row for row in feature_rows if isinstance(row, dict) and row.get("name") == "multi_agent"),
        None,
    )
    if not multi_agent or multi_agent.get("enabled") is not True or multi_agent.get("stage") != "stable":
        raise PreflightError("Codex multi_agent must be stable and enabled")

    account_result = account["result"]
    account_row = account_result.get("account")
    if not isinstance(account_row, dict) or account_row.get("type") != "chatgpt" \
            or account_result.get("requiresOpenaiAuth") is not True:
        raise PreflightError("Codex requires a ChatGPT-managed account for this adapter")

    sent_methods = [document["method"] for document in sent]
    if sent_methods != list(RPC_METHODS):
        raise PreflightError("Codex preflight sent an unexpected RPC sequence")
    transcript = {
        "sent": sent,
        "responses": [
            {
                "id": 1,
                "result": {
                    "userAgent": user_agent,
                    "platformFamily": init_result.get("platformFamily"),
                    "platformOs": init_result.get("platformOs"),
                },
            },
            {
                "id": 2,
                "result": {
                    "model": {
                        "id": selected_model["id"],
                        "model": selected_model["model"],
                        "supportedReasoningEfforts": supported_efforts,
                    },
                },
            },
            {"id": 3, "result": {"effectiveDefaults": effective_defaults}},
            {"id": 4, "result": {"capabilities": provider_capabilities}},
            {"id": 5, "result": {"multi_agent": multi_agent}},
            {
                "id": 6,
                "result": {
                    "account": {"type": account_row["type"]},
                    "requiresOpenaiAuth": account_result["requiresOpenaiAuth"],
                },
            },
        ],
    }
    transcript_sha256 = _sha256(
        json.dumps(transcript, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )

    return {
        "process": {
            "returncode": process.returncode,
            "stderr": _stream_evidence(stderr_bytes),
        },
        "responses": {
            "initialize_sha256": _document_sha256(initialized),
            "model_list_sha256": _document_sha256(models),
            "config_read_sha256": _document_sha256(config),
            "provider_capabilities_sha256": _document_sha256(provider),
            "feature_list_sha256": _document_sha256(features),
            "account_read_sha256": _document_sha256(account),
        },
        "user_agent": user_agent,
        "platform_family": init_result.get("platformFamily"),
        "platform_os": init_result.get("platformOs"),
        "requested_model_available": True,
        "reasoning_effort_supported": True,
        "effective_defaults": effective_defaults,
        "provider_capabilities": provider_capabilities,
        "revision_observation": "NOT_VERIFIED",
        "exact_upstream_usage_observation": "NOT_VERIFIED",
        "multi_agent": {"enabled": True, "stage": "stable"},
        "account": {
            "type": account_row["type"],
            "requires_openai_auth": account_result.get("requiresOpenaiAuth"),
        },
        "transcript": transcript,
        "transcript_sha256": transcript_sha256,
        "model_calls": sent_methods.count("turn/start"),
        "turn_start_requests": sent_methods.count("turn/start"),
    }


def probe(
    model, *, config_cwd, timeout=120, npm_command=None, codex_command=None,
    platform_root=None, artifact_directory=None,
):
    model = _model_contract(model)
    config_cwd = Path(config_cwd).resolve()
    if not config_cwd.is_dir():
        raise PreflightError(f"config cwd is not a directory: {config_cwd}")
    platform_locator = None
    if npm_command is None or platform_root is None:
        default_npm, platform_locator = _default_commands()
        npm_command = npm_command or default_npm
    npm_command = [str(value) for value in npm_command]
    if not npm_command:
        raise PreflightError("npm command must not be empty")
    if artifact_directory is not None:
        artifact_directory = Path(artifact_directory)
        try:
            artifact_directory.mkdir(exist_ok=False)
        except OSError as exc:
            raise PreflightError(f"cannot create preflight artifact directory: {exc}") from exc

    package = _package_receipt(npm_command, timeout)
    platform_root = Path(platform_root) if platform_root is not None else _resolve_platform_root(platform_locator, timeout)
    platform = _platform_receipt(platform_root, npm_command, timeout)
    binary_path = Path(platform["binary"]["path"]).resolve()
    codex_command = [str(binary_path)] if codex_command is None else [str(value) for value in codex_command]
    if not codex_command or Path(codex_command[0]).resolve() != binary_path:
        raise PreflightError("Codex command does not launch the verified native executable")
    # Record the launcher as the verified resolved path so validate_receipt's
    # exact comparison holds when the caller passed an unresolved spelling
    # (8.3 short names in %TEMP% on hosted Windows runners, relative paths).
    codex_command[0] = str(binary_path)
    version = _run([*codex_command, "--version"], timeout=timeout)
    if version.stdout.strip() != f"codex-cli {pilot.CODEX_VERSION}":
        raise PreflightError("Codex executable version does not match the frozen runtime")
    protocol_schema = _schema_receipt(codex_command, timeout, artifact_directory)
    app_server = _rpc_receipt(codex_command, model, config_cwd, timeout)
    receipt = {
        "schema": SCHEMA,
        "status": STATUS,
        "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "package": package,
        "platform_package": platform,
        "runtime": {
            "command": codex_command,
            "launcher_sha256": platform["binary"]["sha256"],
            "version": version.stdout.strip(),
            "version_process": _process_evidence(version),
        },
        "model": model,
        "protocol_schema": protocol_schema,
        "app_server": app_server,
        "model_calls": app_server["model_calls"],
        "turn_start_requests": app_server["turn_start_requests"],
        "model_authorization": "NOT_VERIFIED",
        "model_canary_required": True,
    }
    receipt["receipt_sha256"] = _document_sha256(receipt)
    return receipt


def validate_receipt(receipt, model, artifact_root, config_cwd):
    model = _model_contract(model)
    config_cwd = str(Path(config_cwd).resolve())
    expected_fields = {
        "schema", "status", "completed_at", "package", "platform_package", "runtime",
        "model", "protocol_schema", "app_server", "model_calls", "turn_start_requests",
        "model_authorization", "model_canary_required", "receipt_sha256",
    }
    if not isinstance(receipt, dict) or set(receipt) != expected_fields:
        raise PreflightError(f"preflight receipt fields must be exactly {sorted(expected_fields)}")
    if receipt["schema"] != SCHEMA or receipt["status"] != STATUS:
        raise PreflightError("preflight receipt schema or status is invalid")
    if receipt["receipt_sha256"] != _document_sha256(receipt):
        raise PreflightError("preflight receipt digest does not match its content")
    try:
        completed_at = datetime.fromisoformat(receipt["completed_at"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise PreflightError("preflight completion timestamp is invalid") from exc
    if completed_at.tzinfo is None:
        raise PreflightError("preflight completion timestamp has no timezone")
    if receipt["model"] != model:
        raise PreflightError("preflight model contract does not match the frozen model tuple")

    package = receipt["package"]
    if not isinstance(package, dict) or package.get("version") != pilot.CODEX_VERSION \
            or package.get("integrity") != pilot.CODEX_NPM_INTEGRITY:
        raise PreflightError("preflight base package identity is invalid")
    platform = receipt["platform_package"]
    expected_platform = pilot.CODEX_PLATFORM_PACKAGES.get(platform.get("version")) if isinstance(platform, dict) else None
    binary = platform.get("binary") if isinstance(platform, dict) else None
    if not expected_platform or platform.get("integrity") != expected_platform["integrity"] \
            or not isinstance(binary, dict) or binary.get("sha256") != expected_platform["binary_sha256"]:
        raise PreflightError("preflight native runtime identity is invalid")

    runtime = receipt["runtime"]
    command = runtime.get("command") if isinstance(runtime, dict) else None
    binary_path = Path(binary.get("path", "")) if isinstance(binary, dict) else None
    if not isinstance(runtime, dict) or runtime.get("version") != f"codex-cli {pilot.CODEX_VERSION}" \
            or runtime.get("launcher_sha256") != binary.get("sha256") \
            or not isinstance(command, list) or not command or command[0] != binary.get("path") \
            or binary_path is None or not binary_path.is_absolute() or binary_path.is_symlink() \
            or not binary_path.is_file() or _file_sha256(binary_path) != binary.get("sha256"):
        raise PreflightError("preflight runtime launcher identity is invalid")

    protocol = receipt["protocol_schema"]
    expected_requirements = {
        "collaboration_lineage": True,
        "effective_model_settings": True,
        "parent_thread_identity": True,
        "upstream_usage_dto_shape": True,
        "notification_methods": list(REQUIRED_NOTIFICATION_METHODS),
    }
    if not isinstance(protocol, dict) or protocol.get("requirements") != expected_requirements \
            or not pilot.HEX_SHA256.fullmatch(protocol.get("sha256", "")):
        raise PreflightError("preflight protocol-schema evidence is invalid")
    artifact_path = protocol.get("artifact_path")
    relative = PurePosixPath(artifact_path) if isinstance(artifact_path, str) else None
    if relative is None or relative.is_absolute() or ".." in relative.parts \
            or relative.as_posix() != artifact_path or len(relative.parts) != 2:
        raise PreflightError("preflight schema artifact path is not canonical")
    artifact = Path(artifact_root).joinpath(*relative.parts)
    try:
        if artifact.is_symlink() or not artifact.is_file() or _file_sha256(artifact) != protocol["sha256"]:
            raise PreflightError("preflight schema artifact is missing or changed")
        schema_text = artifact.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PreflightError(f"cannot read retained Codex app-server schema: {exc}") from exc
    schema_document = _loads(schema_text, "retained Codex app-server schema")
    _validate_protocol_schema(schema_document)

    app_server = receipt["app_server"]
    transcript = app_server.get("transcript") if isinstance(app_server, dict) else None
    if not isinstance(transcript, dict) or set(transcript) != {"sent", "responses"}:
        raise PreflightError("preflight app-server transcript is invalid")
    transcript_sha256 = _sha256(
        json.dumps(transcript, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    if transcript_sha256 != app_server.get("transcript_sha256"):
        raise PreflightError("preflight app-server transcript digest is invalid")
    sent = transcript["sent"]
    if not isinstance(sent, list) or [row.get("method") for row in sent if isinstance(row, dict)] != list(RPC_METHODS) \
            or len(sent) != len(RPC_METHODS):
        raise PreflightError("preflight app-server transcript sent an unexpected RPC sequence")
    if [row.get("id") for row in sent] != [1, None, 2, 3, 4, 5, 6] \
            or sent[3].get("params") != {"cwd": config_cwd, "includeLayers": False} \
            or sent[6].get("params") != {"refreshToken": True}:
        raise PreflightError("preflight app-server transcript request binding is invalid")
    if any(row.get("method") == "turn/start" for row in sent):
        raise PreflightError("preflight app-server transcript contains a model turn")

    response_hashes = app_server.get("responses")
    expected_hashes = {
        "initialize_sha256", "model_list_sha256", "config_read_sha256",
        "provider_capabilities_sha256", "feature_list_sha256", "account_read_sha256",
    }
    if not isinstance(response_hashes, dict) or set(response_hashes) != expected_hashes \
            or any(not isinstance(value, str) or not pilot.HEX_SHA256.fullmatch(value)
                   for value in response_hashes.values()):
        raise PreflightError("preflight app-server response digests are invalid")
    responses = transcript["responses"]
    if not isinstance(responses, list) or len(responses) != 6 \
            or [row.get("id") for row in responses if isinstance(row, dict)] != [1, 2, 3, 4, 5, 6] \
            or any(set(row) != {"id", "result"} or not isinstance(row["result"], dict) for row in responses):
        raise PreflightError("preflight app-server response transcript is invalid")
    init_result, model_result, config_result, provider_result, feature_result, account_result = [
        row["result"] for row in responses
    ]
    if set(init_result) != {"userAgent", "platformFamily", "platformOs"} \
            or not isinstance(init_result["userAgent"], str) \
            or pilot.CODEX_VERSION not in init_result["userAgent"] \
            or app_server.get("user_agent") != init_result["userAgent"] \
            or app_server.get("platform_family") != init_result["platformFamily"] \
            or app_server.get("platform_os") != init_result["platformOs"]:
        raise PreflightError("preflight initialize response evidence is invalid")
    model_row = model_result.get("model") if set(model_result) == {"model"} else None
    effort_rows = model_row.get("supportedReasoningEfforts") if isinstance(model_row, dict) else None
    if not isinstance(model_row, dict) or set(model_row) != {"id", "model", "supportedReasoningEfforts"} \
            or not isinstance(model_row["id"], str) or model_row["model"] != model["requested"] \
            or not isinstance(effort_rows, list) or any(not isinstance(value, str) for value in effort_rows) \
            or model["reasoning_effort"] not in effort_rows \
            or app_server.get("requested_model_available") is not True \
            or app_server.get("reasoning_effort_supported") is not True:
        raise PreflightError("preflight requested-model evidence is invalid")
    effective_defaults = config_result.get("effectiveDefaults") \
        if set(config_result) == {"effectiveDefaults"} else None
    expected_defaults = {
        "provider": model["provider"],
        "model": model["requested"],
        "reasoning_effort": model["reasoning_effort"],
    }
    if effective_defaults != expected_defaults or app_server.get("effective_defaults") != expected_defaults:
        raise PreflightError("preflight effective model defaults are invalid")
    capabilities = provider_result.get("capabilities") if set(provider_result) == {"capabilities"} else None
    capability_fields = {"imageGeneration", "namespaceTools", "webSearch"}
    if not isinstance(capabilities, dict) or set(capabilities) != capability_fields \
            or any(not isinstance(value, bool) for value in capabilities.values()) \
            or app_server.get("provider_capabilities") != capabilities:
        raise PreflightError("preflight provider-capability evidence is invalid")
    if app_server.get("revision_observation") != "NOT_VERIFIED":
        raise PreflightError("preflight must not claim a model-revision observation")
    if app_server.get("exact_upstream_usage_observation") != "NOT_VERIFIED":
        raise PreflightError("preflight must not claim an exact-usage observation")
    multi_agent = feature_result.get("multi_agent") if set(feature_result) == {"multi_agent"} else None
    if not isinstance(multi_agent, dict) or multi_agent.get("name") != "multi_agent" \
            or multi_agent.get("enabled") is not True or multi_agent.get("stage") != "stable" \
            or app_server.get("multi_agent") != {"enabled": True, "stage": "stable"}:
        raise PreflightError("preflight multi-agent evidence is invalid")
    account_row = account_result.get("account") if set(account_result) == {"account", "requiresOpenaiAuth"} else None
    requires_auth = account_result.get("requiresOpenaiAuth")
    account = app_server.get("account")
    if account_row != {"type": "chatgpt"} or requires_auth is not True \
            or account != {"type": "chatgpt", "requires_openai_auth": True}:
        raise PreflightError("preflight account evidence is invalid")
    if app_server.get("model_calls") != 0 or app_server.get("turn_start_requests") != 0 \
            or receipt["model_calls"] != 0 or receipt["turn_start_requests"] != 0:
        raise PreflightError("preflight must contain zero model calls and zero turn starts")
    if receipt["model_authorization"] != "NOT_VERIFIED" or receipt["model_canary_required"] is not True:
        raise PreflightError("preflight must not claim model authorization or a completed model canary")
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--reasoning-effort", required=True)
    parser.add_argument("--cwd", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--artifacts", type=Path)
    args = parser.parse_args(argv)
    try:
        receipt = probe(
            {
                "provider": args.provider,
                "requested": args.model,
                "revision": args.revision,
                "reasoning_effort": args.reasoning_effort,
            },
            config_cwd=args.cwd,
            timeout=args.timeout,
            artifact_directory=args.artifacts,
        )
    except PreflightError as exc:
        print(f"preflight failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
