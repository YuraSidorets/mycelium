# Mycelium process-lineage specification

## Boundary

Process lineage records how one Mycelium run moved through APEX, SEPTUM,
HYPHAE, STEM, and CAP. It is separate from reusable schema-v2 evidence nodes:

- `depends_on` remains an evidence or prerequisite relationship.
- `supersedes` remains a lifecycle relationship.
- `Consumes`, timestamps, goal similarity, and role lanes never create process
  edges.

Only explicit, sealed run manifests are authoritative process history. Legacy
nodes remain readable as untracked evidence and are never rewritten or assigned
inferred lineage.

## Storage and identity

Run manifests use schema 1 and live at `.mycelium/runs/<run-id>.json`. A Run ID
distinguishes repeated executions of the same exact goal. Each manifest contains:

- `schema`, `run_id`, `goal`, and `state` (`open`, `sealed`, or `invalid`);
- `created_at`, `updated_at`, and nullable `sealed_at` timestamps;
- one locked, ordered `topology` entry for every role;
- `roles` with explicit `pending`, `performed`, or `skipped` execution state;
- `nodes` containing durable reservations and commit bindings;
- `diagnostics` explaining validation failures.

The implementation serializes reserve, finalize, abort, verify, and seal under
one per-run cross-platform lock. It writes a temporary manifest in the run
directory and replaces the live manifest atomically. The run directory, lock,
and manifest path must resolve beneath the workspace and contain no symlink,
junction, or other reparse-point component.

On POSIX, writers open the opaque lock read/write with an exclusive advisory
lock. A protected verifier opens the same descriptor read-only with a shared
advisory lock. Run manifests, bound node snapshots, and flow records are
readable but not writable across identities; the lock and artifact directories
remain traversable. This permits independent verification without granting the
task identity access to protected grader files.

Manifest, topology, role, node, reservation-audit, and commit objects use exact
schema keys and types. Verification binds the embedded Run ID to the requested
manifest filename and rejects missing, unknown, duplicated, or inconsistent
state instead of tolerating it as authoritative history.

## Topology

The default topology is:

```json
[
  {"role":"apex","state":"required"},
  {"role":"septum","state":"required"},
  {"role":"hyphae","state":"required"},
  {"role":"stem","state":"required"},
  {"role":"cap","state":"required"}
]
```

APEX, STEM, and CAP are always required. There must be at least one committed
APEX, exactly one committed STEM, and at least one committed CAP.

During an open run, a required role is `pending` until it has a committed node.
A skipped topology role is always `skipped`. A sealed manifest contains only
`performed` or `skipped` role states; it can never retain `pending`.

SEPTUM can be skipped only with code `single-clean-stream`; HYPHAE can be
skipped only with code `already-compact`. Either skip requires this exact typed
evidence object:

```json
{
  "input_packets": 1,
  "all_directly_sourced": true,
  "conflicts": false,
  "duplicates": false,
  "stale": false,
  "uncertainty": false,
  "risk": "low"
}
```

Unknown or extra fields, wrong types or values, missing evidence, prose-only
reasons, and skipped mandatory roles are invalid. Nodes cannot be reserved for
a skipped role. A process edge may bridge intermediate roles only when every
bridged role has a valid skip entry.

## Reservation and commit protocol

The public command surface is `python bin/mycelium_lineage.py [--root PATH]` or
the equivalent PowerShell/POSIX wrapper:

```text
preflight
begin --goal G [--run-id ID] [--topology-json JSON | --topology-file FILE]
reserve --run-id R --node-id N --role ROLE [--process-input NODE ...]
finalize --run-id R --reservation-token T --node-path PATH \
  --flow-path PATH --flow-line NUMBER
abort --run-id R --reservation-token T --reason TEXT
verify --run-id R
seal --run-id R
```

Commands return one canonical JSON object on stdout. Contract errors return a
canonical JSON error on stderr and exit nonzero.

`reserve` durably adds a pending node before the existing node/flow writer runs.
`finalize` binds it to the current artifacts using:

```text
node: path, sha256
flow: path, line, sha256
timestamp
canonical commit digest
```

A process crash after reservation therefore leaves visible pending state instead
of an apparently complete run. Seal rejects pending reservations. Exact retries
reuse the same reservation and commit; conflicting retries fail. Abort is
allowed only for a pending reservation and retains the reason for audit.

## Seal validation

Seal fails closed when it finds any of the following:

- a pending reservation or mutation after seal;
- missing required roles, a skipped mandatory role, or more than one STEM;
- unknown, same-role, backward, cross-goal, cyclic, or disconnected process
  input;
- a bridge over a required role;
- a pre-STEM branch that cannot reach the sole STEM;
- a CAP that is not downstream of the STEM;
- a missing, out-of-workspace, stale, or tampered node/flow commit binding.

An open run can be repaired and retried. Failed validation does not claim a
successful process. Only a sealed, currently verified manifest can support a
CAP `Verification: PASS`.

## Compatibility and dependencies

Calls without a Run ID keep the existing schema-v2 node and flow behavior and
remain Python-free on Windows. A tracked Windows call performs a working
interpreter preflight before any reservation or node write, trying `python`,
`python3`, then argv-form `py -3`; POSIX tries `python3`, then `python`.
`PYTHONDONTWRITEBYTECODE=1` is set for lineage execution.

Tracked finalize accepts only a complete schema-v2 node and its matching full
flow projection. Legacy schema-v1 nodes remain available only through the
untracked evidence path.

Atlas loads manifests directly and exposes process, evidence, and lifecycle as
separate layers. Open, invalid, malformed, stale, and legacy-untracked state is
diagnostic, never silently upgraded to authoritative lineage.
