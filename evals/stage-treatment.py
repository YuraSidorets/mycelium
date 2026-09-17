#!/usr/bin/env python3
"""Stage or verify the exact files that define the benchmark treatment."""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath


def canonical(relative):
    parts = PurePosixPath(relative)
    normalized = parts.as_posix()
    if "\\" in relative or normalized != relative or normalized == "." or parts.is_absolute() or ".." in parts.parts:
        raise ValueError(f"non-canonical treatment path: {relative}")
    return normalized


def contained(root, relative):
    parts = PurePosixPath(relative)
    path = (root / Path(*parts.parts)).resolve()
    path.relative_to(root)
    return path


def digest(path):
    return hashlib.sha256(path).hexdigest()


def git_file(source, commit, relative):
    result = subprocess.run(
        ["git", "-C", str(source), "show", f"{commit}:{relative}"],
        check=False,
        capture_output=True,
    )
    if result.returncode:
        reason = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"missing committed treatment source: {relative}: {reason}")
    return result.stdout


def manifest_files(source, manifest, commit):
    if commit:
        relative_manifest = manifest.resolve().relative_to(source).as_posix()
        text = git_file(source, commit, relative_manifest).decode("utf-8")
    else:
        text = manifest.read_text(encoding="utf-8")
    raw_files = [line for line in text.splitlines() if line]
    files = [canonical(line) for line in raw_files]
    if not files or len(files) != len(set(files)):
        raise ValueError("treatment manifest is empty or contains duplicate paths")
    return files


def stage(source, destination, manifest, verify_only, exact, commit):
    source = source.resolve()
    destination = destination.resolve()
    files = manifest_files(source, manifest, commit)

    if verify_only and not destination.is_dir():
        raise ValueError(f"missing treatment directory: {destination}")
    if not verify_only:
        destination.mkdir(parents=True, exist_ok=True)

    rows = []
    for relative in files:
        destination_file = contained(destination, relative)
        if commit:
            expected = git_file(source, commit, relative)
        else:
            source_file = contained(source, relative)
            if not source_file.is_file():
                raise ValueError(f"missing treatment source: {relative}")
            expected = source_file.read_bytes()
        expected_digest = digest(expected)
        if not verify_only:
            destination_file.parent.mkdir(parents=True, exist_ok=True)
            destination_file.write_bytes(expected)
        if not destination_file.is_file():
            raise ValueError(f"digest mismatch: {relative}")
        staged_digest = digest(destination_file.read_bytes())
        if staged_digest != expected_digest:
            raise ValueError(f"digest mismatch: {relative}")
        rows.append({"path": relative, "sha256": staged_digest})

    if exact:
        allowed_directories = {
            parent.as_posix()
            for relative in files
            for parent in PurePosixPath(relative).parents
            if parent.as_posix() != "."
        }
        unexpected = []
        for path in destination.rglob("*"):
            relative = path.relative_to(destination).as_posix()
            if path.is_symlink():
                raise ValueError(f"symlink in treatment directory: {relative}")
            if path.is_file() and relative not in files:
                unexpected.append(relative)
            elif path.is_dir() and relative not in allowed_directories:
                unexpected.append(relative + "/")
            elif not path.is_file() and not path.is_dir():
                unexpected.append(relative)
        if unexpected:
            raise ValueError(f"unexpected treatment entries: {', '.join(sorted(unexpected))}")

    package_bytes = "".join(f"{row['path']}\0{row['sha256']}\n" for row in rows).encode()
    return {
        "file_count": len(rows),
        "package_sha256": hashlib.sha256(package_bytes).hexdigest(),
        "files": rows,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--commit")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--exact", action="store_true")
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                stage(
                    args.source,
                    args.destination,
                    args.manifest,
                    args.verify_only,
                    args.exact,
                    args.commit,
                ),
                indent=2,
            )
        )
        return 0
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
