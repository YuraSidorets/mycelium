# Contributing

Thanks for looking at Mycelium. This is a personally maintained, experimental
project; contributions are welcome but reviewed slowly.

## Ground rules

- You must have the right to contribute what you send. Do not paste code,
  text, or data whose license you cannot satisfy, and say where anything
  non-original came from.
- Contributions are accepted under the project license, Apache-2.0 (see
  `LICENSE`). By contributing you agree your contribution is licensed the same
  way.
- Sign off each commit (`git commit -s`) to indicate agreement with the
  [Developer Certificate of Origin](https://developercertificate.org/). This
  records an origin assertion under your public identity; it is not a
  copyright transfer.

## Before you open a pull request

Run everything that can run on your platform from the repository root:

```
python -m unittest discover -s tests -p "test_*.py"
node --test tests/*.mjs
pwsh tests/smoke.ps1        # Windows or any host with PowerShell 7
bash tests/smoke.sh          # Linux, macOS, or Git Bash
bash tests/workflow-evals.sh
```

The same behavior is often implemented twice, once in `bin/mycelium.py` and
once in native PowerShell under `bin/`. Change both, and keep their output
byte-identical; several tests assert that.

Do not commit anything under `.mycelium/` from your own runs; that directory
is the live state of whatever project Mycelium is used in and is ignored.

## What a good change looks like

- One concern per pull request.
- A test that fails before the change and passes after it.
- No new runtime dependencies. The Python side is standard library only, on
  purpose.
- If you change `bin/mycelium_lineage.py` or `evals/verify-cap.py`, regenerate
  the matching base64 payload in `evals/Dockerfile-v4`; `tests/smoke.ps1`
  checks that it matches the live file.

## Reporting bugs

Open an issue with the command you ran, the output, your OS and Python,
PowerShell, or Node versions, and a minimal synthetic fixture. Do not attach
real `.mycelium/` records.
