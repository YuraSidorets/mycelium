import http.client
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPOSITORY / "package.json"
NODE = shutil.which("node")
NPM = shutil.which("npm")
EXPECTED_PACKAGE_FILES = {
    "NOTICE",
    "bin/mycelium-atlas.mjs",
    "bin/mycelium_graph.py",
    "bin/mycelium_lineage.py",
    "bin/mycelium.py",
    "web/mycelium-atlas/index.html",
    "web/mycelium-atlas/atlas.css",
    "web/mycelium-atlas/atlas.js",
    "web/mycelium-atlas/atlas-layout.js",
    "web/mycelium-atlas/atlas-model.js",
    "web/mycelium-atlas/atlas-draft-store.js",
}
ALLOWED_NPM_METADATA = {
    "package.json",
    "README",
    "README.md",
    "README.txt",
    "LICENSE",
    "LICENSE.md",
    "LICENSE.txt",
    "NOTICE",
    "NOTICE.md",
    "NOTICE.txt",
}


def snapshot_tree(root):
    result = {}
    if not root.exists():
        return result
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        result[relative] = (path.read_bytes(), path.stat().st_mtime_ns)
    return result


def write_minimal_graph(root, fact="npx package fact"):
    (root / "evidence.md").write_text(f"{fact}\n", encoding="utf-8")
    nodes = root / ".mycelium" / "nodes"
    nodes.mkdir(parents=True)
    (nodes / "npx-package.md").write_text(
        "\n".join(
            [
                "---",
                "nodeId: npx-package",
                "goal: verify the npx package",
                "role: hyphae",
                "status: complete",
                "topics: npx,package",
                "confidence: high",
                "consumes: none",
                "blocks: none",
                "version: 1",
                "source_refs: evidence.md:1",
                "invalidated: false",
                "---",
                "",
                "# npx-package",
                "",
                "## Facts",
                fact,
                "",
                "## Questions",
                "none",
                "",
                "## Next",
                "verify package execution",
                "",
            ]
        ),
        encoding="utf-8",
    )


class MyceliumNpxTest(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def copy_package(self, destination):
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(MANIFEST_PATH, destination / "package.json")
        for relative in EXPECTED_PACKAGE_FILES:
            source = REPOSITORY / Path(relative)
            target = destination / Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        return destination

    def run_node(self, package_root, cwd, *arguments):
        environment = os.environ.copy()
        environment.pop("PYTHONDONTWRITEBYTECODE", None)
        environment.pop("PYTHONPYCACHEPREFIX", None)
        return subprocess.run(
            [NODE, str(package_root / "bin" / "mycelium-atlas.mjs"), *arguments],
            cwd=cwd,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def load_packaged_backend(self, package_root):
        module_path = package_root / "bin" / "mycelium_graph.py"
        spec = importlib.util.spec_from_file_location(
            f"mycelium_graph_npx_{id(package_root)}", module_path
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        previous = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = previous
        return module

    def test_manifest_is_private_dependency_free_and_has_exact_bin_and_files(self):
        self.assertEqual("mycelium-atlas", self.manifest["name"])
        self.assertIs(True, self.manifest.get("private"))
        self.assertEqual(
            {"mycelium-atlas": "bin/mycelium-atlas.mjs"},
            self.manifest.get("bin"),
        )
        self.assertEqual(EXPECTED_PACKAGE_FILES, set(self.manifest.get("files", [])))
        for field in (
            "dependencies",
            "optionalDependencies",
            "peerDependencies",
        ):
            self.assertEqual({}, self.manifest.get(field, {}), field)
        scripts = self.manifest.get("scripts", {})
        for lifecycle in ("preinstall", "install", "postinstall"):
            self.assertNotIn(lifecycle, scripts)

    @unittest.skipUnless(NPM, "npm is not available")
    def test_npm_pack_dry_run_contains_only_the_static_allowlist(self):
        with tempfile.TemporaryDirectory(prefix="mycelium npx cache ") as cache:
            environment = {
                **os.environ,
                "npm_config_cache": cache,
                "npm_config_offline": "true",
                "npm_config_ignore_scripts": "true",
            }
            completed = subprocess.run(
                [NPM, "pack", "--dry-run", "--json", "--ignore-scripts", "--offline"],
                cwd=REPOSITORY,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(1, len(report))
        packed = {item["path"].replace("\\", "/") for item in report[0]["files"]}
        self.assertTrue(EXPECTED_PACKAGE_FILES <= packed, packed)
        unexpected = packed - EXPECTED_PACKAGE_FILES - ALLOWED_NPM_METADATA
        self.assertEqual(set(), unexpected)
        self.assertFalse(any(path.startswith(".mycelium/") for path in packed))
        self.assertFalse(any("__pycache__" in path or path.endswith(".pyc") for path in packed))

    @unittest.skipUnless(NODE, "node is not available")
    def test_node_launcher_uses_unrelated_working_directory_as_default_root(self):
        with tempfile.TemporaryDirectory(prefix="mycelium npx package ") as package_dir:
            with tempfile.TemporaryDirectory(prefix="mycelium outside package ") as target_dir:
                package = self.copy_package(Path(package_dir))
                target = Path(target_dir)
                write_minimal_graph(target)
                package_before = snapshot_tree(package)
                artifacts_before = snapshot_tree(target / ".mycelium")

                completed = self.run_node(package, target, "--json")

                self.assertEqual(0, completed.returncode, completed.stderr)
                payload = json.loads(completed.stdout)
                self.assertEqual(target.name, payload["repository"])
                self.assertEqual(1, payload["summary"]["nodes"])
                self.assertEqual(package_before, snapshot_tree(package))
                self.assertEqual(artifacts_before, snapshot_tree(target / ".mycelium"))

    @unittest.skipUnless(NODE, "node is not available")
    def test_explicit_root_with_spaces_is_forwarded_without_writes(self):
        with tempfile.TemporaryDirectory(prefix="mycelium npx package ") as package_dir:
            with tempfile.TemporaryDirectory(prefix="mycelium unrelated cwd ") as cwd_dir:
                with tempfile.TemporaryDirectory(prefix="mycelium target with spaces ") as target_dir:
                    package = self.copy_package(Path(package_dir))
                    target = Path(target_dir)
                    write_minimal_graph(target, "explicit root fact")
                    package_before = snapshot_tree(package)
                    artifacts_before = snapshot_tree(target / ".mycelium")

                    completed = self.run_node(
                        package,
                        Path(cwd_dir),
                        "--json",
                        "--root",
                        str(target),
                    )

                    self.assertEqual(0, completed.returncode, completed.stderr)
                    payload = json.loads(completed.stdout)
                    self.assertEqual(target.name, payload["repository"])
                    self.assertEqual(1, payload["summary"]["nodes"])
                    self.assertEqual(package_before, snapshot_tree(package))
                    self.assertEqual(artifacts_before, snapshot_tree(target / ".mycelium"))

    @unittest.skipUnless(NODE, "node is not available")
    def test_unknown_argument_preserves_argparse_exit_code_two(self):
        with tempfile.TemporaryDirectory(prefix="mycelium npx package ") as package_dir:
            with tempfile.TemporaryDirectory(prefix="mycelium unrelated cwd ") as cwd_dir:
                package = self.copy_package(Path(package_dir))
                completed = self.run_node(
                    package,
                    Path(cwd_dir),
                    "--definitely-not-an-atlas-option",
                )

                self.assertEqual(2, completed.returncode, completed.stderr)
                self.assertIn("unrecognized arguments", completed.stderr)

    def test_packaged_assets_are_served_when_target_has_no_web_directory(self):
        with tempfile.TemporaryDirectory(prefix="mycelium npx package ") as package_dir:
            with tempfile.TemporaryDirectory(prefix="mycelium target without web ") as target_dir:
                package = self.copy_package(Path(package_dir))
                target = Path(target_dir)
                write_minimal_graph(target)
                self.assertFalse((target / "web").exists())
                package_before = snapshot_tree(package)
                artifacts_before = snapshot_tree(target / ".mycelium")
                backend = self.load_packaged_backend(package)
                server = backend.create_server(target, port=0)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                port = server.server_address[1]
                try:
                    for request_path, relative in (
                        ("/", "web/mycelium-atlas/index.html"),
                        ("/atlas.css", "web/mycelium-atlas/atlas.css"),
                        ("/atlas.js", "web/mycelium-atlas/atlas.js"),
                        ("/atlas-layout.js", "web/mycelium-atlas/atlas-layout.js"),
                        ("/atlas-model.js", "web/mycelium-atlas/atlas-model.js"),
                        ("/atlas-draft-store.js", "web/mycelium-atlas/atlas-draft-store.js"),
                    ):
                        with self.subTest(path=request_path):
                            connection = http.client.HTTPConnection(
                                "127.0.0.1", port, timeout=5
                            )
                            try:
                                connection.request("GET", request_path)
                                response = connection.getresponse()
                                body = response.read()
                            finally:
                                connection.close()
                            self.assertEqual(200, response.status)
                            self.assertEqual((package / relative).read_bytes(), body)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)

                self.assertFalse(thread.is_alive())
                self.assertEqual(package_before, snapshot_tree(package))
                self.assertEqual(artifacts_before, snapshot_tree(target / ".mycelium"))


if __name__ == "__main__":
    unittest.main()
