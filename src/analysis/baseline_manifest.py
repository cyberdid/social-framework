"""Hash a bounded set of baseline files without copying source contents."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_manifest(repo_root: Path, baseline_commit: str, paths: list[Path]) -> dict:
    root = repo_root.resolve()
    records = []
    for path in paths:
        resolved = path.resolve()
        try:
            relpath = resolved.relative_to(root).as_posix()
            scope = "repository"
            display_path = relpath
        except ValueError:
            scope = "external_local_input"
            display_path = str(resolved)
        if resolved.is_file():
            records.append({"path": display_path, "scope": scope, "status": "present",
                            "size_bytes": resolved.stat().st_size, "sha256": sha256_file(resolved)})
        else:
            records.append({"path": display_path, "scope": scope, "status": "not_present",
                            "size_bytes": None, "sha256": None})
    return {
        "schema_version": "1.0",
        "baseline_commit": baseline_commit,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "files": records,
        "interpretation": "SHA-256 and file size identify the bytes available at baseline; they do not verify semantic correctness or completeness.",
        "raw_data_policy": "Only paths, availability, sizes, and digests are recorded; file contents are not copied.",
    }


def write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def main(argv=None) -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", help="Baseline commit; defaults to current HEAD")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("paths", nargs="+", type=Path, help="Files to fingerprint")
    args = parser.parse_args(argv)
    commit = args.commit or subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                                             capture_output=True, text=True,
                                             check=True).stdout.strip()
    if args.output.resolve() in {path.resolve() for path in args.paths}:
        raise SystemExit("output must not overwrite a baseline input")
    result = build_manifest(root, commit, args.paths)
    write_atomic(args.output, result)
    print(f"recorded {len(result['files'])} baseline file receipts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
