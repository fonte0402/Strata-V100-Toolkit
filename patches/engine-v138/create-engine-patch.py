from __future__ import annotations

import argparse
import difflib
import hashlib
import json
from pathlib import Path
import tarfile

BASE_FILES = ("CMakeLists.txt", "include/strata/core/expert_cache.hpp",
              "src/core/expert_cache.cpp", "src/program/generate.cpp")
ADDED_FILES = ("src/core/expert_cache_sized_test.cpp",)
VERSION = "Strata-0.1.38"


def normalized(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def sha(data: bytes) -> str:
    return hashlib.sha256(normalized(data)).hexdigest()


def oid(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def patch_file(path: str, old: bytes, new: bytes) -> bytes:
    old = normalized(old)
    new = normalized(new)
    headers = [f"diff --git a/{path} b/{path}\n"]
    if old:
        headers.extend((f"index {oid(old)[:7]}..{oid(new)[:7]} 100644\n",
                        f"--- a/{path}\n", f"+++ b/{path}\n"))
    else:
        headers.extend(("new file mode 100644\n", f"index 0000000..{oid(new)[:7]}\n",
                        "--- /dev/null\n", f"+++ b/{path}\n"))
    lines = list(difflib.unified_diff(old.decode().splitlines(keepends=True),
                                      new.decode().splitlines(keepends=True),
                                      fromfile="", tofile="", n=3, lineterm="\n"))
    return "".join(headers + lines[2:]).encode()


def tar_files(path: Path) -> dict[str, bytes]:
    result = {}
    with tarfile.open(path, "r:gz") as archive:
        members = [m for m in archive.getmembers() if m.isfile()]
        prefixes = {m.name.split("/", 1)[0] for m in members}
        if prefixes != {VERSION}:
            raise ValueError(f"unexpected archive root: {sorted(prefixes)}")
        for rel in BASE_FILES:
            member = archive.getmember(f"{VERSION}/{rel}")
            result[rel] = archive.extractfile(member).read()
    return result


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tar", required=True, type=Path)
    ap.add_argument("--source-root", required=True, type=Path)
    ap.add_argument("--output-dir", type=Path, default=Path(__file__).parent)
    args = ap.parse_args(argv)
    tar_path = args.tar.resolve(strict=True)
    root = args.source_root.resolve(strict=True)
    base = tar_files(tar_path)
    chunks = []
    records = []
    for rel, old in base.items():
        new = (root / rel).read_bytes()
        if normalized(old) == normalized(new):
            raise SystemExit(f"expected v138 source modification is missing: {rel}")
        chunks.append(patch_file(rel, old, new))
        records.append({"path": rel, "kind": "modified", "base_sha256": sha(old),
                        "result_sha256": sha(new)})
    for rel in ADDED_FILES:
        new_path = root / rel
        if not new_path.is_file():
            raise SystemExit(f"expected added engine test source missing: {rel}")
        new = new_path.read_bytes()
        chunks.append(patch_file(rel, b"", new))
        records.append({"path": rel, "kind": "added", "result_sha256": sha(new)})
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    patch = b"\n".join(chunks)
    (out / "engine-v138.patch").write_bytes(patch)
    meta = {"source_archive": tar_path.name, "source_archive_sha256": hashlib.sha256(tar_path.read_bytes()).hexdigest(),
            "source_root": VERSION, "source_version": "v0.1.38", "files": records,
            "patch_sha256": hashlib.sha256(patch).hexdigest(),
            "external_candidate_binary_sha256": "a0fdf2b8e1228c80b8c1fcd721e9f67a8240d8ac7cee88edccfbe8d00db6e772",
            "external_candidate_binary_included": False,
            "candidate_binary_mtime": "2026-10-04 14:27 local file time",
            "generate_cpp_mtime": "2026-10-04 14:48 local file time",
            "built_binary_reproduced_by_this_patch": False}
    (out / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
