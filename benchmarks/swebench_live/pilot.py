#!/usr/bin/env python3
"""Freeze and verify a no-score SWE-bench-Live pilot manifest."""

import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import itertools
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


DATASET_NAME = "SWE-bench-Live/MultiLang"
DATASET_REVISION = "608f7ae9ab8ea1f9f0d030fe04562cf6bd1a0c8b"
EVALUATOR_REPOSITORY = "microsoft/SWE-bench-Live"
EVALUATOR_COMMIT = "70ec57e852e3f2d195790fe71f553e272c691833"
PIER_VERSION = "0.3.0"
CODEX_VERSION = "0.146.0"
CODEX_NPM_INTEGRITY = "sha512-yG3sPWNda/2YAIQIDq9MrrjoCTIQ7rxYM5IasrG3VBcuhCLTkgeg/JzqmJq1V98RE4MJ5jCxDXXQlOjrditFRw=="
CODEX_PLATFORM_PACKAGES = {
    "0.146.0-linux-x64": {
        "integrity": "sha512-fswvyGprAPCMiOEue/7MKMk7pCjh9kZIJfJX5i9atmfnmGYbYCcUhZsEH9LEP0+0t5xyPqDbfNXY7NSxIVuXxA==",
        "binary_sha256": "2e863156ed35ecc5253b1e2f907a9143077b9f7cb51942070c61996471ff6e04",
    },
    "0.146.0-win32-x64": {
        "integrity": "sha512-b3lxMYeR0+IhstNo4JjX1P9cPc1xwVcCVkPd1lD1wpWPJ0SBhpIkPczwbu3ZRkJcdyl342+rgyf4DUrbZLdrGA==",
        "binary_sha256": "bc343ba420dc2e2e9f59e6fc5e5bf0aae1cd8c771fc319665241fc9c0271fddb",
    },
}
ARMS = ("vanilla", "prompt", "full")
LANGUAGE_QUOTAS = {"Go": 3, "TypeScript": 3, "JavaScript": 2, "Rust": 2}
RANK_SALT = "mycelium-swebench-live-pilot-v1"
TREATMENT_MANIFEST = "evals/treatment-files.txt"
PUBLIC_FEASIBILITY_IDS = (
    "submariner-3957",
    "kops-18152",
    "gh-dash-813",
    "mikro-orm-7464",
    "wxt-2267",
    "vueuse-5336",
    "react-rails-1418",
    "doctoc-328",
    "wgpu-9367",
    "OpenShell-695",
)
FULL_PROVENANCE = (
    "spawn",
    "complete_node",
    "fresh_retrieval",
    "stem",
    "external_cap",
    "multi_session",
)
TELEMETRY_FIELDS = (
    "run_id",
    "session_id",
    "parent_session_id",
    "instance_id",
    "arm",
    "phase",
    "requested_model",
    "observed_model",
    "observed_provider",
    "observed_reasoning_effort",
    "replicate",
    "arm_order",
    "started_at",
    "ended_at",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cost_usd",
    "wall_time_seconds",
    "steps",
    "tool_calls",
    "patch_sha256",
    "patch_files",
    "spawn_count",
    "peak_parallel_agents",
    "durable_node_ids",
    "fresh_retrieval",
    "stem_ledger_sha256",
    "cap_verification",
    "grader_report_sha256",
    "grader_resolved",
    "trace_sha256",
)
CANDIDATE_FIELDS = {
    "instance_id",
    "repository",
    "language",
    "created_at",
    "declared_tests",
    "gold_passes",
    "gold_attempts",
    "base_commit",
    "image_digest",
    "hidden_row_sha256",
}
TASK_FIELDS = CANDIDATE_FIELDS - {"gold_passes", "gold_attempts"}
HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
GIT_OBJECT = re.compile(r"[0-9a-f]{40,64}\Z")
REPO_DIGEST = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}\Z")
EXCLUDED_REPOSITORY = re.compile(r"agent|llm|codex|benchmark", re.IGNORECASE)
OUTCOME_KEYS = re.compile(r"(^|_)(score|reward|outcome|resolved|prediction|patch|result)s?($|_)")
CREATED_AFTER = datetime(2026, 1, 1, tzinfo=timezone.utc)
STAGER_PATH = Path(__file__).resolve().parents[2] / "evals" / "stage-treatment.py"
CAP_VERIFIER_PATH = Path(__file__).resolve().parents[2] / "evals" / "verify-cap.py"
EXECUTOR_PATHS = (
    Path(__file__),
    Path(__file__).with_name("preflight.py"),
    Path(__file__).with_name("run_pilot.py"),
    CAP_VERIFIER_PATH,
)
SCHEDULE_EXTRAS = (
    (0, 1, 2, 5),
    (1, 2, 3, 5),
    (1, 2, 4, 5),
)


class PilotError(ValueError):
    pass


def _object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PilotError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_object_without_duplicates)
    except (OSError, json.JSONDecodeError) as exc:
        raise PilotError(f"cannot read JSON {path}: {exc}") from exc


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path):
    try:
        return _sha256(Path(path).read_bytes())
    except OSError as exc:
        raise PilotError(f"missing digest input {path}: {exc}") from exc


def executor_digest():
    return _sha256(_canonical({path.name: _file_sha256(path) for path in EXECUTOR_PATHS}))


def document_digest(document):
    unsigned = dict(document)
    unsigned.pop("manifest_sha256", None)
    return _sha256(_canonical(unsigned))


def _reject_outcomes(value, path="$"):
    if isinstance(value, dict):
        for key, child in value.items():
            if key != "gold_passes" and OUTCOME_KEYS.search(key.lower()):
                raise PilotError(f"outcome field is forbidden at {path}.{key}")
            _reject_outcomes(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_outcomes(child, f"{path}[{index}]")


def _require_exact_keys(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise PilotError(f"{label} fields must be exactly {sorted(expected)}")


def _parse_created_at(value, instance_id):
    if not isinstance(value, str):
        raise PilotError(f"candidate {instance_id} created_at must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PilotError(f"candidate {instance_id} has invalid created_at") from exc
    if parsed.tzinfo is None:
        raise PilotError(f"candidate {instance_id} created_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def _load_candidates(path):
    try:
        raw = Path(path).read_bytes()
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_without_duplicates)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PilotError(f"cannot read candidate metadata {path}: {exc}") from exc
    _reject_outcomes(document)
    _require_exact_keys(
        document,
        {
            "dataset_name",
            "dataset_revision",
            "evaluator_repository",
            "evaluator_commit",
            "candidates",
        },
        "candidate metadata",
    )
    if (
        document["dataset_name"] != DATASET_NAME
        or document["dataset_revision"] != DATASET_REVISION
    ):
        raise PilotError("candidate metadata uses an unpinned dataset identity")
    if (
        document["evaluator_repository"] != EVALUATOR_REPOSITORY
        or document["evaluator_commit"] != EVALUATOR_COMMIT
    ):
        raise PilotError("candidate metadata uses an unpinned evaluator identity")
    if not isinstance(document["candidates"], list):
        raise PilotError("candidates must be a list")

    seen_ids = set()
    candidates = []
    for candidate in document["candidates"]:
        _require_exact_keys(candidate, CANDIDATE_FIELDS, "candidate")
        instance_id = candidate["instance_id"]
        if not isinstance(instance_id, str) or not instance_id.strip() or instance_id in seen_ids:
            raise PilotError(f"candidate instance_id is missing or duplicated: {instance_id!r}")
        seen_ids.add(instance_id)
        repository = candidate["repository"]
        if not isinstance(repository, str) or "/" not in repository:
            raise PilotError(f"candidate {instance_id} repository must be owner/name")
        if not isinstance(candidate["declared_tests"], int) or isinstance(candidate["declared_tests"], bool):
            raise PilotError(f"candidate {instance_id} declared_tests must be an integer")
        if not isinstance(candidate["gold_passes"], int) or isinstance(candidate["gold_passes"], bool):
            raise PilotError(f"candidate {instance_id} gold_passes must be an integer")
        if not isinstance(candidate["gold_attempts"], int) or isinstance(candidate["gold_attempts"], bool):
            raise PilotError(f"candidate {instance_id} gold_attempts must be an integer")
        if not isinstance(candidate["base_commit"], str) or not GIT_OBJECT.fullmatch(candidate["base_commit"]):
            raise PilotError(f"candidate {instance_id} base commit digest is missing or mutable")
        if not isinstance(candidate["image_digest"], str) or not REPO_DIGEST.fullmatch(candidate["image_digest"]):
            raise PilotError(f"candidate {instance_id} image digest must be an immutable RepoDigest")
        if not isinstance(candidate["hidden_row_sha256"], str) or not HEX_SHA256.fullmatch(candidate["hidden_row_sha256"]):
            raise PilotError(f"candidate {instance_id} hidden row digest is missing or mutable")
        created_at = _parse_created_at(candidate["created_at"], instance_id)
        if (
            candidate["language"] in LANGUAGE_QUOTAS
            and created_at >= CREATED_AFTER
            and 0 < candidate["declared_tests"] <= 2000
            and candidate["gold_passes"] == candidate["gold_attempts"] == 3
            and not EXCLUDED_REPOSITORY.search(repository)
            and instance_id not in PUBLIC_FEASIBILITY_IDS
        ):
            candidates.append(candidate)
    return document, candidates, _sha256(raw)


def _rank(instance_id):
    return _sha256(f"{RANK_SALT}\0{instance_id}".encode("utf-8"))


def _select_tasks(candidates):
    selected = []
    repositories = set()
    for language, quota in LANGUAGE_QUOTAS.items():
        eligible = sorted(
            (candidate for candidate in candidates if candidate["language"] == language),
            key=lambda candidate: (_rank(candidate["instance_id"]), candidate["instance_id"]),
        )
        for candidate in eligible:
            repository = candidate["repository"].casefold()
            if repository in repositories:
                continue
            selected.append(candidate)
            repositories.add(repository)
            if sum(item["language"] == language for item in selected) == quota:
                break
        if sum(item["language"] == language for item in selected) != quota:
            raise PilotError(f"candidate quota cannot be met for {language}: need {quota} unique repositories")

    selected.sort(key=lambda candidate: (_rank(candidate["instance_id"]), candidate["instance_id"]))
    return [{key: candidate[key] for key in sorted(TASK_FIELDS)} for candidate in selected]


def _select_canary(candidates, tasks):
    task_ids = {task["instance_id"] for task in tasks}
    repositories = {task["repository"].casefold() for task in tasks}
    candidates_by_id = {candidate["instance_id"]: candidate for candidate in candidates}
    for instance_id in PUBLIC_FEASIBILITY_IDS:
        candidate = candidates_by_id.get(instance_id)
        if candidate is not None \
                and instance_id not in task_ids \
                and candidate["repository"].casefold() not in repositories:
            return {
                "arm": "full",
                "task": {key: candidate[key] for key in sorted(TASK_FIELDS)},
            }
    raise PilotError("candidate metadata has no public feasibility canary disjoint from diagnostic tasks")


def _git(root, *args):
    try:
        return subprocess.run(
            ["git", "-C", str(Path(root).resolve()), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PilotError(f"cannot inspect treatment git repository: {exc}") from exc


def _installed_treatment_digest(root, installed_root, commit):
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(STAGER_PATH),
                "--source", str(Path(root).resolve()),
                "--destination", str(Path(installed_root).resolve()),
                "--manifest", str(Path(root).resolve() / TREATMENT_MANIFEST),
                "--commit", commit,
                "--verify-only",
                "--exact",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        package_sha256 = json.loads(result.stdout)["package_sha256"]
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError, KeyError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise PilotError(f"installed treatment does not match the committed package: {detail.strip()}") from exc
    if not HEX_SHA256.fullmatch(package_sha256 or ""):
        raise PilotError("installed treatment package digest is invalid")
    return package_sha256


def _treatment_identity(root, installed_root):
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise PilotError("treatment repository is dirty")
    commit = _git(root, "rev-parse", "HEAD").decode("ascii").strip()
    tree = _git(root, "rev-parse", "HEAD^{tree}").decode("ascii").strip()
    if not GIT_OBJECT.fullmatch(commit) or not GIT_OBJECT.fullmatch(tree):
        raise PilotError("treatment commit, tree, or package digest input is missing")
    manifest = _git(root, "show", f"{commit}:{TREATMENT_MANIFEST}").decode("utf-8").splitlines()
    if not manifest or len(manifest) != len(set(manifest)):
        raise PilotError("committed treatment manifest is empty or duplicated")
    rows = []
    for relative in manifest:
        path = PurePosixPath(relative)
        if path.as_posix() != relative or path.is_absolute() or ".." in path.parts:
            raise PilotError(f"committed treatment path is not canonical: {relative}")
        rows.append(f"{relative}\0{_sha256(_git(root, 'show', f'{commit}:{relative}'))}\n")
    package_sha256 = _sha256("".join(rows).encode())
    installed_package_sha256 = _installed_treatment_digest(root, installed_root, commit)
    if installed_package_sha256 != package_sha256:
        raise PilotError("installed treatment digest does not match the committed package")
    return {
        "commit": commit,
        "tree": tree,
        "package_sha256": package_sha256,
        "installed_package_sha256": installed_package_sha256,
    }


def _prompt_digests(prompt_paths):
    if set(prompt_paths) != set(ARMS):
        raise PilotError(f"prompt paths must be supplied for exactly {ARMS}")
    return {arm: _file_sha256(prompt_paths[arm]) for arm in ARMS}


def _schedule(tasks, replicate):
    if not isinstance(replicate, int) or isinstance(replicate, bool) or replicate < 0:
        raise PilotError("replicate must be a non-negative integer")
    permutations = list(itertools.permutations(ARMS))
    orders = permutations + [permutations[index] for index in SCHEDULE_EXTRAS[replicate % 3]]
    return [
        {
            "instance_id": task["instance_id"],
            "arm_order": list(orders[index]),
        }
        for index, task in enumerate(tasks)
    ]


def _validate_cost_caps(value):
    _require_exact_keys(
        value,
        {
            "max_usd_per_run", "max_total_usd", "max_tokens_per_run", "max_wall_time_seconds",
            "max_tool_calls_per_run",
        },
        "cost caps",
    )
    amount = value["max_usd_per_run"]
    try:
        valid_amount = isinstance(amount, str) and Decimal(amount) > 0 and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", amount)
    except InvalidOperation:
        valid_amount = False
    if not valid_amount:
        raise PilotError("max_usd_per_run must be a positive decimal string")
    total = value["max_total_usd"]
    try:
        valid_total = isinstance(total, str) and Decimal(total) > 0 and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", total)
    except InvalidOperation:
        valid_total = False
    if not valid_total:
        raise PilotError("max_total_usd must be a positive decimal string")
    tokens = value["max_tokens_per_run"]
    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens <= 0:
        raise PilotError("max_tokens_per_run must be a positive integer")
    wall_time = value["max_wall_time_seconds"]
    if not isinstance(wall_time, int) or isinstance(wall_time, bool) or wall_time <= 0:
        raise PilotError("max_wall_time_seconds must be a positive integer")
    tool_calls = value["max_tool_calls_per_run"]
    if not isinstance(tool_calls, int) or isinstance(tool_calls, bool) or tool_calls <= 0:
        raise PilotError("max_tool_calls_per_run must be a positive integer")


def _build_manifest(candidates_path, treatment_root, installed_treatment, prompt_paths, runner_path,
                    model_provider, requested_model, model_revision, reasoning_effort, replicate,
                    max_usd_per_run, max_total_usd, max_tokens_per_run, max_wall_time_seconds,
                    max_tool_calls_per_run):
    source, eligible, candidate_digest = _load_candidates(candidates_path)
    tasks = _select_tasks(eligible)
    canary = _select_canary(source["candidates"], tasks)
    schedule = _schedule(tasks, replicate)
    prompt_digests = _prompt_digests(prompt_paths)
    model_fields = {
        "provider": model_provider,
        "requested": requested_model,
        "revision": model_revision,
        "reasoning_effort": reasoning_effort,
    }
    if any(not isinstance(value, str) or not value.strip() for value in model_fields.values()):
        raise PilotError("model provider, requested model, revision, and reasoning effort are required")
    cost_caps = {
        "max_usd_per_run": str(max_usd_per_run),
        "max_total_usd": str(max_total_usd),
        "max_tokens_per_run": max_tokens_per_run,
        "max_wall_time_seconds": max_wall_time_seconds,
        "max_tool_calls_per_run": max_tool_calls_per_run,
    }
    _validate_cost_caps(cost_caps)

    arms = []
    for arm in ARMS:
        arms.append({
            "name": arm,
            "prompt_sha256": prompt_digests[arm],
            "treatment_enabled": arm != "vanilla",
            "multi_agent_enabled": arm == "full",
            "required_provenance": list(FULL_PROVENANCE if arm == "full" else ()),
        })
    manifest = {
        "schema": "mycelium.swebench-live-pilot/v4",
        "status": "NO-SCORE",
        "dataset": {"name": DATASET_NAME, "revision": DATASET_REVISION},
        "evaluator": {"repository": EVALUATOR_REPOSITORY, "commit": EVALUATOR_COMMIT},
        "runtime": {
            "pier_version": PIER_VERSION,
            "codex_version": CODEX_VERSION,
            "codex_npm_integrity": CODEX_NPM_INTEGRITY,
            "codex_platform_packages": CODEX_PLATFORM_PACKAGES,
        },
        "model": {**model_fields, "observed": None},
        "treatment": _treatment_identity(treatment_root, installed_treatment),
        "runner_sha256": _file_sha256(runner_path),
        "executor_sha256": executor_digest(),
        "arms": arms,
        "eligibility": {
            "created_at_on_or_after": "2026-01-01T00:00:00Z",
            "declared_tests_max": 2000,
            "official_gold_validation": "3/3",
            "repositories_unique": True,
            "excluded_repository_terms": ["agent", "llm", "codex", "benchmark"],
            "public_feasibility_ids": list(PUBLIC_FEASIBILITY_IDS),
        },
        "selection": {
            "candidate_count": len(source["candidates"]),
            "candidate_metadata_sha256": candidate_digest,
            "language_quotas": dict(LANGUAGE_QUOTAS),
            "rank_salt": RANK_SALT,
            "tasks": tasks,
        },
        "canary": canary,
        "replicate": replicate,
        "schedule": schedule,
        "schedule_sha256": _sha256(_canonical(schedule)),
        "telemetry_required": list(TELEMETRY_FIELDS),
        "cost_caps": cost_caps,
    }
    manifest["manifest_sha256"] = document_digest(manifest)
    return manifest


def validate_structure(manifest):
    if not isinstance(manifest, dict):
        raise PilotError("manifest must be a JSON object")
    _reject_outcomes(manifest)
    expected_top = {
        "schema", "status", "dataset", "evaluator", "runtime", "model", "treatment", "runner_sha256",
        "executor_sha256",
        "arms", "eligibility", "selection", "canary", "replicate", "schedule", "schedule_sha256",
        "telemetry_required", "cost_caps", "manifest_sha256",
    }
    _require_exact_keys(manifest, expected_top, "manifest")
    if manifest["manifest_sha256"] != document_digest(manifest):
        raise PilotError("manifest digest does not match its content")
    if manifest["schema"] != "mycelium.swebench-live-pilot/v4" or manifest["status"] != "NO-SCORE":
        raise PilotError("manifest must use the v4 NO-SCORE schema")
    if manifest["dataset"] != {"name": DATASET_NAME, "revision": DATASET_REVISION}:
        raise PilotError("manifest dataset identity is not pinned")
    if manifest["evaluator"] != {
        "repository": EVALUATOR_REPOSITORY,
        "commit": EVALUATOR_COMMIT,
    }:
        raise PilotError("manifest evaluator identity is not pinned")
    expected_runtime = {
        "pier_version": PIER_VERSION,
        "codex_version": CODEX_VERSION,
        "codex_npm_integrity": CODEX_NPM_INTEGRITY,
        "codex_platform_packages": CODEX_PLATFORM_PACKAGES,
    }
    if manifest["runtime"] != expected_runtime:
        raise PilotError("pilot runtime versions are not pinned")
    _require_exact_keys(
        manifest["model"],
        {"provider", "requested", "revision", "reasoning_effort", "observed"},
        "model",
    )
    if any(
        not isinstance(manifest["model"][field], str) or not manifest["model"][field].strip()
        for field in ("provider", "requested", "revision", "reasoning_effort")
    ):
        raise PilotError("model provider, requested model, revision, and reasoning effort must be pinned")
    if manifest["model"]["observed"] is not None:
        raise PilotError("NO-SCORE manifest observed model must be null")

    _require_exact_keys(
        manifest["treatment"],
        {"commit", "tree", "package_sha256", "installed_package_sha256"},
        "treatment",
    )
    if not GIT_OBJECT.fullmatch(manifest["treatment"]["commit"] or ""):
        raise PilotError("treatment commit is missing or mutable")
    if not GIT_OBJECT.fullmatch(manifest["treatment"]["tree"] or ""):
        raise PilotError("treatment tree is missing or mutable")
    if not HEX_SHA256.fullmatch(manifest["treatment"]["package_sha256"] or ""):
        raise PilotError("treatment package digest is missing or mutable")
    if manifest["treatment"]["installed_package_sha256"] != manifest["treatment"]["package_sha256"]:
        raise PilotError("installed treatment digest does not match the committed package")
    if not HEX_SHA256.fullmatch(manifest["runner_sha256"] or ""):
        raise PilotError("runner digest is missing or mutable")
    if not HEX_SHA256.fullmatch(manifest["executor_sha256"] or ""):
        raise PilotError("executor digest is missing or mutable")

    if not isinstance(manifest["arms"], list) or len(manifest["arms"]) != len(ARMS):
        raise PilotError("manifest must contain exactly vanilla, prompt, and full arms")
    for expected_name, arm in zip(ARMS, manifest["arms"]):
        _require_exact_keys(
            arm,
            {"name", "prompt_sha256", "treatment_enabled", "multi_agent_enabled", "required_provenance"},
            "arm",
        )
        if arm["name"] != expected_name or not HEX_SHA256.fullmatch(arm["prompt_sha256"] or ""):
            raise PilotError("arm names and prompt digests must be immutable")
        if arm["treatment_enabled"] != (expected_name != "vanilla"):
            raise PilotError("only prompt and full arms may receive the treatment")
        if arm["multi_agent_enabled"] != (expected_name == "full"):
            raise PilotError("only the full arm may enable multi-agent execution")
        expected_provenance = list(FULL_PROVENANCE if expected_name == "full" else ())
        if arm["required_provenance"] != expected_provenance:
            if expected_name == "full":
                raise PilotError("full arm provenance must require spawn, node, STEM, external CAP, and multi-session evidence")
            raise PilotError(f"{expected_name} arm must not require Mycelium provenance")

    expected_eligibility = {
        "created_at_on_or_after": "2026-01-01T00:00:00Z",
        "declared_tests_max": 2000,
        "official_gold_validation": "3/3",
        "repositories_unique": True,
        "excluded_repository_terms": ["agent", "llm", "codex", "benchmark"],
        "public_feasibility_ids": list(PUBLIC_FEASIBILITY_IDS),
    }
    if manifest["eligibility"] != expected_eligibility:
        raise PilotError("eligibility contract is mutable")
    _require_exact_keys(
        manifest["selection"],
        {"candidate_count", "candidate_metadata_sha256", "language_quotas", "rank_salt", "tasks"},
        "selection",
    )
    selection = manifest["selection"]
    if not isinstance(selection["candidate_count"], int) or selection["candidate_count"] < 10:
        raise PilotError("candidate count cannot satisfy the frozen quota")
    if not HEX_SHA256.fullmatch(selection["candidate_metadata_sha256"] or ""):
        raise PilotError("candidate metadata digest is missing or mutable")
    if selection["language_quotas"] != LANGUAGE_QUOTAS or selection["rank_salt"] != RANK_SALT:
        raise PilotError("candidate quotas or rank salt are mutable")
    tasks = selection["tasks"]
    if not isinstance(tasks, list) or len(tasks) != 10:
        raise PilotError("candidate quota must freeze exactly 10 tasks")
    for task in tasks:
        _require_exact_keys(task, TASK_FIELDS, "selected task")
        if not GIT_OBJECT.fullmatch(task["base_commit"] or ""):
            raise PilotError("selected task base commit digest is missing or mutable")
        if not REPO_DIGEST.fullmatch(task["image_digest"] or ""):
            raise PilotError("selected task image digest is missing or mutable")
        if not HEX_SHA256.fullmatch(task["hidden_row_sha256"] or ""):
            raise PilotError("selected task hidden row digest is missing or mutable")
    if Counter(task["language"] for task in tasks) != Counter(LANGUAGE_QUOTAS):
        raise PilotError("selected task language quota is invalid")
    if len({task["repository"].casefold() for task in tasks}) != 10:
        raise PilotError("selected task repositories must be unique")
    if len({task["instance_id"] for task in tasks}) != 10:
        raise PilotError("selected task IDs must be unique")
    if tasks != sorted(tasks, key=lambda task: (_rank(task["instance_id"]), task["instance_id"])):
        raise PilotError("selected tasks are not in deterministic outcome-blind order")

    canary = manifest["canary"]
    _require_exact_keys(canary, {"arm", "task"}, "canary")
    if canary["arm"] != "full":
        raise PilotError("canary must use the full arm")
    canary_task = canary["task"]
    _require_exact_keys(canary_task, TASK_FIELDS, "canary task")
    if not GIT_OBJECT.fullmatch(canary_task["base_commit"] or ""):
        raise PilotError("canary task base commit digest is missing or mutable")
    if not REPO_DIGEST.fullmatch(canary_task["image_digest"] or ""):
        raise PilotError("canary task image digest is missing or mutable")
    if not HEX_SHA256.fullmatch(canary_task["hidden_row_sha256"] or ""):
        raise PilotError("canary task hidden row digest is missing or mutable")
    if canary_task["instance_id"] in {task["instance_id"] for task in tasks}:
        raise PilotError("canary task ID must be disjoint from diagnostic task IDs")
    if not isinstance(canary_task["repository"], str) \
            or canary_task["repository"].casefold() in {task["repository"].casefold() for task in tasks}:
        raise PilotError("canary task repository must be disjoint from diagnostic task repositories")
    if canary_task["instance_id"] not in PUBLIC_FEASIBILITY_IDS:
        raise PilotError("canary task must use a public feasibility ID")

    expected_schedule = _schedule(tasks, manifest["replicate"])
    if manifest["schedule"] != expected_schedule:
        raise PilotError("six-permutation arm schedule is invalid")
    if manifest["schedule_sha256"] != _sha256(_canonical(manifest["schedule"])):
        raise PilotError("schedule digest does not match its content")
    if manifest["telemetry_required"] != list(TELEMETRY_FIELDS):
        raise PilotError("multi-session telemetry contract is incomplete")
    _validate_cost_caps(manifest["cost_caps"])
    return manifest


def freeze(*, candidates_path, output_path, treatment_root, installed_treatment, prompt_paths, runner_path,
           model_provider, requested_model, model_revision, reasoning_effort, replicate,
           max_usd_per_run, max_total_usd, max_tokens_per_run, max_wall_time_seconds,
           max_tool_calls_per_run):
    manifest = _build_manifest(
        candidates_path, treatment_root, installed_treatment, prompt_paths, runner_path,
        model_provider, requested_model, model_revision, reasoning_effort, replicate,
        max_usd_per_run, max_total_usd, max_tokens_per_run, max_wall_time_seconds,
        max_tool_calls_per_run,
    )
    validate_structure(manifest)
    with Path(output_path).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    return manifest


def validate_manifest(manifest_path, candidates_path, treatment_root, installed_treatment, prompt_paths, runner_path):
    manifest = validate_structure(_load_json(manifest_path))
    source, eligible, candidate_digest = _load_candidates(candidates_path)
    if manifest["selection"]["candidate_metadata_sha256"] != candidate_digest:
        raise PilotError("candidate metadata digest does not match the supplied file")
    if manifest["selection"]["candidate_count"] != len(source["candidates"]):
        raise PilotError("candidate count does not match the supplied metadata")
    if manifest["selection"]["tasks"] != _select_tasks(eligible):
        raise PilotError("selected tasks do not match the supplied candidate metadata")
    if manifest["canary"] != _select_canary(source["candidates"], manifest["selection"]["tasks"]):
        raise PilotError("canary does not match the supplied candidate metadata")
    if manifest["treatment"] != _treatment_identity(treatment_root, installed_treatment):
        raise PilotError("treatment digest does not match the clean repository")
    prompt_digests = _prompt_digests(prompt_paths)
    for arm in manifest["arms"]:
        if arm["prompt_sha256"] != prompt_digests[arm["name"]]:
            raise PilotError(f"{arm['name']} prompt digest does not match the supplied file")
    if manifest["runner_sha256"] != _file_sha256(runner_path):
        raise PilotError("runner digest does not match the supplied file")
    if manifest["executor_sha256"] != executor_digest():
        raise PilotError("executor digest does not match the frozen pilot executor files")
    return manifest


def _add_inputs(parser, include_freeze_fields):
    parser.add_argument("--candidates", required=True, type=Path)
    parser.add_argument("--treatment-root", required=True, type=Path)
    parser.add_argument("--installed-treatment", required=True, type=Path)
    parser.add_argument("--vanilla-prompt", required=True, type=Path)
    parser.add_argument("--prompt-prompt", required=True, type=Path)
    parser.add_argument("--full-prompt", required=True, type=Path)
    parser.add_argument("--runner", required=True, type=Path)
    if include_freeze_fields:
        parser.add_argument("--model-provider", required=True)
        parser.add_argument("--requested-model", required=True)
        parser.add_argument("--model-revision", required=True)
        parser.add_argument("--reasoning-effort", required=True)
        parser.add_argument("--replicate", type=int, default=0)
        parser.add_argument("--max-usd-per-run", required=True)
        parser.add_argument("--max-total-usd", required=True)
        parser.add_argument("--max-tokens-per-run", required=True, type=int)
        parser.add_argument("--max-wall-time-seconds", required=True, type=int)
        parser.add_argument("--max-tool-calls-per-run", required=True, type=int)


def _prompt_paths(args):
    return {"vanilla": args.vanilla_prompt, "prompt": args.prompt_prompt, "full": args.full_prompt}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze_parser = commands.add_parser("freeze", help="write a new immutable NO-SCORE manifest")
    freeze_parser.add_argument("manifest", type=Path)
    _add_inputs(freeze_parser, True)
    validate_parser = commands.add_parser("validate", help="verify a frozen manifest and every local digest input")
    validate_parser.add_argument("manifest", type=Path)
    _add_inputs(validate_parser, False)
    args = parser.parse_args(argv)
    try:
        if args.command == "freeze":
            manifest = freeze(
                candidates_path=args.candidates,
                output_path=args.manifest,
                treatment_root=args.treatment_root,
                installed_treatment=args.installed_treatment,
                prompt_paths=_prompt_paths(args),
                runner_path=args.runner,
                model_provider=args.model_provider,
                requested_model=args.requested_model,
                model_revision=args.model_revision,
                reasoning_effort=args.reasoning_effort,
                replicate=args.replicate,
                max_usd_per_run=args.max_usd_per_run,
                max_total_usd=args.max_total_usd,
                max_tokens_per_run=args.max_tokens_per_run,
                max_wall_time_seconds=args.max_wall_time_seconds,
                max_tool_calls_per_run=args.max_tool_calls_per_run,
            )
        else:
            manifest = validate_manifest(
                args.manifest, args.candidates, args.treatment_root, args.installed_treatment,
                _prompt_paths(args), args.runner
            )
    except (PilotError, FileExistsError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(f"NO-SCORE {manifest['manifest_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
