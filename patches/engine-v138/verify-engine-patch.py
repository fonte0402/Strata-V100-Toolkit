from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

VERSION = "Strata-0.1.38"
BASE_FILES = ("CMakeLists.txt", "include/strata/core/expert_cache.hpp",
              "src/core/expert_cache.cpp", "src/program/generate.cpp")


def normalized(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def main() -> int:
    here = Path(__file__).resolve().parent
    if len(sys.argv) != 3:
        raise SystemExit("usage: verify-engine-patch.py SOURCE_TAR EXTRACTED_SOURCE_ROOT")
    tar_path, source_root = Path(sys.argv[1]).resolve(strict=True), Path(sys.argv[2]).resolve(strict=True)
    metadata = json.loads((here / "metadata.json").read_text(encoding="utf-8"))
    patch = here / "engine-v138.patch"
    if hashlib.sha256(tar_path.read_bytes()).hexdigest() != metadata["source_archive_sha256"]:
        raise SystemExit("source tar SHA-256 mismatch")
    if hashlib.sha256(patch.read_bytes()).hexdigest() != metadata["patch_sha256"]:
        raise SystemExit("patch SHA-256 mismatch")
    expected = {entry["path"]: entry["result_sha256"] for entry in metadata["files"]}
    with tempfile.TemporaryDirectory(prefix="v138 patch verify ") as temp:
        root = Path(temp)
        with tarfile.open(tar_path, "r:gz") as archive:
            for rel in BASE_FILES:
                member = archive.getmember(f"{VERSION}/{rel}")
                target = root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
        check = subprocess.run(["git", "apply", "--check", str(patch)], cwd=root,
                               capture_output=True, text=True)
        if check.returncode:
            raise SystemExit("git apply --check failed: " + check.stderr.strip())
        apply = subprocess.run(["git", "apply", str(patch)], cwd=root,
                               capture_output=True, text=True)
        if apply.returncode:
            raise SystemExit("git apply failed: " + apply.stderr.strip())
        for rel, wanted in expected.items():
            staged, actual = root / rel, source_root / rel
            if not staged.is_file() or not actual.is_file():
                raise SystemExit(f"post-apply file missing: {rel}")
            staged_sha = hashlib.sha256(normalized(staged.read_bytes())).hexdigest()
            source_sha = hashlib.sha256(normalized(actual.read_bytes())).hexdigest()
            if staged_sha != wanted or source_sha != wanted:
                raise SystemExit(f"post-apply source mismatch: {rel}")
    print(f"PASS: v138 patch applies to archive {metadata['source_archive_sha256']} and matches extracted source files")
    print("NOTE: this source check does not build or attest the existing engine binary")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
