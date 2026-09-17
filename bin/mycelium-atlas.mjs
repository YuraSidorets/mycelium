#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const binDirectory = dirname(fileURLToPath(import.meta.url));
const backend = resolve(binDirectory, "mycelium_graph.py");
const requiredFiles = [
  backend,
  resolve(binDirectory, "mycelium.py"),
  resolve(binDirectory, "mycelium_lineage.py"),
  resolve(binDirectory, "..", "web", "mycelium-atlas", "index.html"),
  resolve(binDirectory, "..", "web", "mycelium-atlas", "atlas.css"),
  resolve(binDirectory, "..", "web", "mycelium-atlas", "atlas.js"),
  resolve(binDirectory, "..", "web", "mycelium-atlas", "atlas-draft-store.js"),
  resolve(binDirectory, "..", "web", "mycelium-atlas", "atlas-layout.js"),
  resolve(binDirectory, "..", "web", "mycelium-atlas", "atlas-model.js"),
];

for (const requiredFile of requiredFiles) {
  if (!existsSync(requiredFile)) {
    console.error(`Mycelium Atlas installation is incomplete: missing ${requiredFile}`);
    process.exit(1);
  }
}

const pythonCandidates = process.platform === "win32"
  ? [
      { command: "python", prefix: [] },
      { command: "python3", prefix: [] },
      { command: "py", prefix: ["-3"] },
    ]
  : [
      { command: "python3", prefix: [] },
      { command: "python", prefix: [] },
    ];

const python = pythonCandidates.find(({ command, prefix }) => {
  const probe = spawnSync(
    command,
    [
      ...prefix,
      "-B",
      "-c",
      "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)",
    ],
    { shell: false, stdio: "ignore", windowsHide: true },
  );
  return probe.status === 0;
});

if (!python) {
  console.error("Mycelium Atlas requires Python 3.9 or newer.");
  process.exit(1);
}

const result = spawnSync(
  python.command,
  [...python.prefix, "-B", backend, ...process.argv.slice(2)],
  {
    cwd: process.cwd(),
    shell: false,
    stdio: "inherit",
  },
);

if (result.error) {
  console.error(`Unable to start Mycelium Atlas: ${result.error.message}`);
  process.exit(1);
}

if (typeof result.status === "number") {
  process.exit(result.status);
}

process.exit(result.signal === "SIGINT" ? 130 : 1);
