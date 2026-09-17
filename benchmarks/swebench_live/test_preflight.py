import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import textwrap
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).parent))
import pilot
import preflight


FAKE_PLATFORM_VERSION = f"{pilot.CODEX_VERSION}-test"
FAKE_PLATFORM_INTEGRITY = "sha512-test-platform"
FAKE_MODEL = {
    "provider": "openai",
    "requested": "gpt-test",
    "revision": "gpt-test-revision",
    "reasoning_effort": "ultra",
}
FAKE_SCHEMA = {
    "definitions": {
        "RawResponseCompletedNotification": {
            "properties": {
                "responseId": {"type": "string"},
                "threadId": {"type": "string"},
                "turnId": {"type": "string"},
                "usage": {"anyOf": [
                    {"$ref": "#/definitions/TokenUsageBreakdown"},
                    {"type": "null"},
                ]},
            },
            "required": ["responseId", "threadId", "turnId"],
        },
        "TokenUsageBreakdown": {
            "type": "object",
            "properties": {
                field: {"type": "integer"}
                for field in (
                    "cachedInputTokens", "inputTokens", "outputTokens",
                    "reasoningOutputTokens", "totalTokens",
                )
            },
            "required": [
                "cachedInputTokens", "inputTokens", "outputTokens",
                "reasoningOutputTokens", "totalTokens",
            ],
        },
        "Thread": {"properties": {
            "parentThreadId": {"type": ["string", "null"]},
            "modelProvider": {"type": "string"},
        }},
        "ThreadSettings": {"properties": {
            "model": {"type": "string"},
            "modelProvider": {"type": "string"},
            "effort": {"type": ["string", "null"]},
        }},
        "ModelReroutedNotification": {
            "required": ["fromModel", "reason", "threadId", "toModel", "turnId"],
        },
        "ThreadItem": {
            "oneOf": [{
                "required": ["senderThreadId", "receiverThreadIds", "type"],
                "properties": {
                    "senderThreadId": {"type": "string"},
                    "receiverThreadIds": {"type": "array", "items": {"type": "string"}},
                    "type": {"enum": ["collabAgentToolCall"]},
                },
            }],
        },
        "ServerNotification": {
            "oneOf": [
                {"properties": {"method": {"enum": ["rawResponse/completed"]}}},
                {"properties": {"method": {"enum": ["thread/tokenUsage/updated"]}}},
                {"properties": {"method": {"enum": ["model/rerouted"]}}},
            ],
        },
    },
}


FAKE_NPM = r'''#!/usr/bin/env python3
import json
import os
import sys

target = sys.argv[2]
if target.endswith("-test"):
    version = __PLATFORM_VERSION__
    integrity = "wrong" if os.environ.get("FAKE_CODEX_MODE") == "wrong-platform" else __PLATFORM_INTEGRITY__
else:
    version = __VERSION__
    integrity = "wrong" if os.environ.get("FAKE_CODEX_MODE") == "wrong-package" else __INTEGRITY__
print(json.dumps({"version": version, "dist.integrity": integrity}))
'''


FAKE_CODEX = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

mode = os.environ.get("FAKE_CODEX_MODE", "ok")
args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 0.1.0" if mode == "wrong-version" else "codex-cli " + __VERSION__)
    raise SystemExit(0)

if args[:2] == ["app-server", "generate-json-schema"]:
    output = Path(args[args.index("--out") + 1])
    output.mkdir(parents=True, exist_ok=True)
    schema = __SCHEMA__
    if mode == "missing-schema":
        schema["definitions"]["RawResponseCompletedNotification"]["properties"].pop("usage")
    if mode == "weak-usage-schema":
        schema["definitions"]["TokenUsageBreakdown"]["properties"]["inputTokens"] = {}
    (output / "codex_app_server_protocol.v2.schemas.json").write_text(
        json.dumps(schema), encoding="utf-8"
    )
    print("ok")
    raise SystemExit(0)

if args != ["app-server", "--stdio"]:
    raise SystemExit(9)

log_path = os.environ.get("FAKE_RPC_LOG")
for line in sys.stdin:
    request = json.loads(line)
    if log_path:
        with Path(log_path).open("a", encoding="utf-8") as stream:
            stream.write(request["method"] + "\n")
    method = request["method"]
    if mode == "non-object-rpc":
        print("[]", flush=True)
        continue
    if method == "initialized":
        continue
    if method == "initialize":
        result = {
            "userAgent": "Codex Desktop/" + __VERSION__ + " (test)",
            "codexHome": "/tmp/codex",
            "platformFamily": "test",
            "platformOs": "test",
        }
    elif method == "model/list":
        efforts = ["low"] if mode == "unsupported-effort" else ["low", "ultra"]
        result = {"data": [] if mode == "no-model" else [{
            "id": "gpt-test",
            "model": "gpt-test",
            "supportedReasoningEfforts": [
                {"reasoningEffort": effort, "description": effort} for effort in efforts
            ],
        }]}
    elif method == "config/read":
        result = {"config": {
            "model": "gpt-test",
            "model_provider": "amazon-bedrock" if mode == "wrong-provider" else None,
            "model_reasoning_effort": "ultra",
        }, "origins": {}}
    elif method == "modelProvider/capabilities/read":
        result = {"imageGeneration": True, "namespaceTools": True, "webSearch": True}
    elif method == "experimentalFeature/list":
        result = {"data": [{
            "name": "multi_agent",
            "enabled": mode != "disabled-feature",
            "stage": "stable",
        }]}
    elif method == "account/read":
        result = {
            "account": None if mode == "no-account" else {
                "type": "apiKey" if mode == "wrong-account" else "chatgpt",
                "email": "secret@example.test",
            },
            "requiresOpenaiAuth": mode != "no-openai-auth",
        }
    else:
        print(json.dumps({"id": request.get("id"), "error": {"message": "unexpected"}}), flush=True)
        continue
    print(json.dumps({"id": request["id"], "result": result}), flush=True)
'''


class RuntimePreflightTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.npm = self.root / "fake-npm.py"
        self.codex = self.root / "fake-codex.py"
        self.log = self.root / "rpc.log"
        self.platform = self.root / "codex-test"
        (self.platform / "vendor" / "test" / "bin").mkdir(parents=True)
        self.binary = self.platform / "vendor" / "test" / "bin" / "codex.exe"
        shutil.copy2(sys.executable, self.binary)
        self.binary_sha256 = hashlib.sha256(self.binary.read_bytes()).hexdigest()
        (self.platform / "package.json").write_text(
            json.dumps({"name": "@openai/codex", "version": FAKE_PLATFORM_VERSION}),
            encoding="utf-8",
        )
        self.npm.write_text(
            textwrap.dedent(FAKE_NPM)
            .replace("__VERSION__", repr(pilot.CODEX_VERSION))
            .replace("__INTEGRITY__", repr(pilot.CODEX_NPM_INTEGRITY))
            .replace("__PLATFORM_VERSION__", repr(FAKE_PLATFORM_VERSION))
            .replace("__PLATFORM_INTEGRITY__", repr(FAKE_PLATFORM_INTEGRITY)),
            encoding="utf-8",
        )
        self.codex.write_text(
            textwrap.dedent(FAKE_CODEX)
            .replace("__VERSION__", repr(pilot.CODEX_VERSION))
            .replace("__SCHEMA__", repr(FAKE_SCHEMA)),
            encoding="utf-8",
        )
        self.environment = mock.patch.dict(os.environ, {"FAKE_RPC_LOG": str(self.log)}, clear=False)
        self.environment.start()
        self.platform_identity = mock.patch.dict(
            pilot.CODEX_PLATFORM_PACKAGES,
            {FAKE_PLATFORM_VERSION: {
                "integrity": FAKE_PLATFORM_INTEGRITY,
                "binary_sha256": self.binary_sha256,
            }},
            clear=True,
        )
        self.platform_identity.start()
        self.probe_number = 0

    def tearDown(self):
        self.platform_identity.stop()
        self.environment.stop()
        self.temporary.cleanup()

    def _probe(self, *, retain=False):
        self.probe_number += 1
        artifacts = self.root / f"artifacts-{self.probe_number}" if retain else None
        self.last_artifacts = artifacts
        return preflight.probe(
            FAKE_MODEL,
            config_cwd=self.root,
            timeout=10,
            npm_command=[sys.executable, str(self.npm)],
            codex_command=[str(self.binary), str(self.codex)],
            platform_root=self.platform,
            artifact_directory=artifacts,
        )

    def test_proves_structural_readiness_without_starting_a_turn(self):
        receipt = self._probe(retain=True)

        self.assertEqual(preflight.STATUS, receipt["status"])
        self.assertEqual(0, receipt["model_calls"])
        self.assertEqual(0, receipt["turn_start_requests"])
        self.assertEqual("NOT_VERIFIED", receipt["model_authorization"])
        self.assertTrue(receipt["model_canary_required"])
        self.assertTrue(receipt["app_server"]["requested_model_available"])
        self.assertTrue(receipt["app_server"]["reasoning_effort_supported"])
        self.assertEqual("NOT_VERIFIED", receipt["app_server"]["exact_upstream_usage_observation"])
        self.assertEqual({"enabled": True, "stage": "stable"}, receipt["app_server"]["multi_agent"])
        self.assertEqual(self.binary_sha256, receipt["platform_package"]["binary"]["sha256"])
        self.assertEqual(self.binary_sha256, receipt["runtime"]["launcher_sha256"])
        self.assertEqual(preflight._document_sha256(receipt), receipt["receipt_sha256"])
        self.assertEqual(list(preflight.RPC_METHODS), self.log.read_text(encoding="utf-8").splitlines())
        self.assertNotIn("turn/start", self.log.read_text(encoding="utf-8"))
        self.assertNotIn("secret@example.test", json.dumps(receipt))
        self.assertEqual(receipt, preflight.validate_receipt(receipt, FAKE_MODEL, self.root, self.root))
        self.assertTrue((self.last_artifacts / "codex_app_server_protocol.v2.schemas.json").is_file())

    def test_fails_closed_on_runtime_or_capability_drift(self):
        with self.assertRaises(preflight.PreflightError):
            preflight.probe(
                FAKE_MODEL,
                config_cwd=self.root,
                timeout=10,
                npm_command=[sys.executable, str(self.npm)],
                codex_command=[sys.executable, str(self.codex)],
                platform_root=self.platform,
            )
        for mode in (
            "wrong-package",
            "wrong-platform",
            "wrong-version",
            "missing-schema",
            "weak-usage-schema",
            "no-model",
            "unsupported-effort",
            "wrong-provider",
            "disabled-feature",
            "no-account",
            "wrong-account",
            "no-openai-auth",
            "non-object-rpc",
        ):
            with self.subTest(mode=mode), mock.patch.dict(os.environ, {"FAKE_CODEX_MODE": mode}):
                self.log.unlink(missing_ok=True)
                with self.assertRaises(preflight.PreflightError):
                    self._probe()

    def test_retained_schema_or_transcript_tampering_fails_validation(self):
        receipt = self._probe(retain=True)
        schema = self.last_artifacts / "codex_app_server_protocol.v2.schemas.json"
        schema.write_text("{}", encoding="utf-8")
        with self.assertRaises(preflight.PreflightError):
            preflight.validate_receipt(receipt, FAKE_MODEL, self.root, self.root)

        receipt = self._probe(retain=True)
        receipt["app_server"]["transcript"]["sent"].append({"method": "turn/start"})
        receipt["receipt_sha256"] = preflight._document_sha256(receipt)
        with self.assertRaises(preflight.PreflightError):
            preflight.validate_receipt(receipt, FAKE_MODEL, self.root, self.root)

        receipt = self._probe(retain=True)
        receipt["app_server"]["transcript"]["responses"] = []
        receipt["app_server"]["transcript_sha256"] = preflight._sha256(
            json.dumps(
                receipt["app_server"]["transcript"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        receipt["receipt_sha256"] = preflight._document_sha256(receipt)
        with self.assertRaises(preflight.PreflightError):
            preflight.validate_receipt(receipt, FAKE_MODEL, self.root, self.root)


if __name__ == "__main__":
    unittest.main()
