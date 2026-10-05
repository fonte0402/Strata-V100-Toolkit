from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import sys

BASE = "9259cad4cfa3543cd3b8decab5962672b968c649"
TRACKED = ("serve/server.py", "serve/web/app.css", "serve/web/app.js", "serve/web/index.html")
ADDED = ("serve/token_usage.py", "serve/test_generation_accounting.py",
         "serve/test_shutdown.py", "serve/test_token_usage.py")


def run(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=repo)


def main() -> int:
    here = Path(__file__).resolve().parent
    repo = Path(sys.argv[1]).resolve(strict=True) if len(sys.argv) > 1 else None
    metadata = json.loads((here / "metadata.json").read_text(encoding="utf-8"))
    patch = here / "api-v31.patch"
    if hashlib.sha256(patch.read_bytes()).hexdigest() != metadata["patch_sha256"]:
        raise SystemExit("patch SHA-256 does not match metadata")
    if metadata["base_commit"] != BASE:
        raise SystemExit("metadata names a different base")
    if repo is None:
        raise SystemExit("pass the source Git checkout so exact base blobs can be verified")
    head = run(repo, "rev-parse", "HEAD").decode().strip()
    if head != BASE:
        raise SystemExit(f"source checkout HEAD is {head}, expected {BASE}")
    result_hashes = {item["path"]: item["result_sha256"] for item in metadata["files"]}
    with tempfile.TemporaryDirectory(prefix="strata api patch verify ") as temp:
        root = Path(temp)
        for rel in TRACKED:
            data = run(repo, "show", f"{BASE}:{rel}")
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        checked = subprocess.run(["git", "apply", "--check", str(patch)], cwd=root,
                                 capture_output=True, text=True)
        if checked.returncode:
            raise SystemExit("git apply --check failed: " + checked.stderr.strip())
        applied = subprocess.run(["git", "apply", str(patch)], cwd=root,
                                 capture_output=True, text=True)
        if applied.returncode:
            raise SystemExit("git apply failed: " + applied.stderr.strip())
        for rel in (*TRACKED, *ADDED):
            target = root / rel
            if rel not in result_hashes:
                if target.exists():
                    raise SystemExit(f"unexpected path after apply: {rel}")
                continue
            actual_data = target.read_bytes()
            # Core.autocrlf may materialize CRLF in this Windows verification tree.
            normalized = actual_data.replace(b"\r\n", b"\n")
            actual = hashlib.sha256(normalized).hexdigest()
            if actual != result_hashes[rel]:
                expected_data = (repo / rel).read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                at = next((i for i, (left, right) in enumerate(zip(expected_data, normalized))
                           if left != right), min(len(expected_data), len(actual_data)))
                raise SystemExit(f"post-apply bytes differ from capture: {rel}; expected={len(expected_data)} bytes actual={len(actual_data)} bytes first-difference={at}; expected_slice={expected_data[max(0,at-12):at+24]!r}; actual_slice={normalized[max(0,at-12):at+24]!r}")
    print(f"PASS: patch applies to exact base {BASE}; every exported file matches its recorded SHA-256")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
