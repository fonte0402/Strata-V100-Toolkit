"""Compare an extracted v138 source tree with its original tar source without copying it."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tarfile

IGNORED_PARTS = {"build", "build-lead", ".venv", "__pycache__"}


def ignored(rel: str) -> bool:
    parts = Path(rel).parts
    return any(part in IGNORED_PARTS for part in parts) or rel.startswith("bench/results/")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tar", required=True, type=Path)
    ap.add_argument("--source-root", required=True, type=Path)
    args = ap.parse_args()
    root = args.source_root.resolve(strict=True)
    with tarfile.open(args.tar, "r:gz") as bundle:
        members = {}
        prefix = None
        for member in bundle.getmembers():
            if not member.isfile():
                continue
            if prefix is None:
                prefix = member.name.split("/", 1)[0] + "/"
            rel = member.name.removeprefix(prefix)
            if not rel or ignored(rel):
                continue
            members[rel] = bundle.extractfile(member).read()
    source_files = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if ignored(rel) or ".git" in path.parts:
            continue
        source_files[rel] = path.read_bytes()
    changed = []
    for rel, original in members.items():
        current = source_files.get(rel)
        if current is None:
            changed.append({"path": rel, "kind": "missing", "tar_sha256": sha(original)})
        elif current.replace(b"\r\n", b"\n") != original.replace(b"\r\n", b"\n"):
            changed.append({"path": rel, "kind": "modified", "tar_sha256": sha(original),
                            "source_sha256": sha(current)})
    extra = sorted(set(source_files) - set(members))
    print(json.dumps({"tar_sha256": sha(args.tar.read_bytes()), "compared_files": len(members),
                      "changes": changed, "extra_source_files": extra}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
