from __future__ import annotations

import argparse
import difflib
import hashlib
import json
from pathlib import Path
import subprocess
import sys

BASE = "9259cad4cfa3543cd3b8decab5962672b968c649"
TRACKED = ("serve/server.py", "serve/web/app.css", "serve/web/app.js", "serve/web/index.html")
ADDED = ("serve/token_usage.py", "serve/test_generation_accounting.py",
         "serve/test_shutdown.py", "serve/test_token_usage.py")


def run(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=repo)


def blob_oid(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def file_patch(path: str, old: bytes, new: bytes) -> bytes:
    old = old.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    new = new.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    old_text = old.decode("utf-8").splitlines(keepends=True)
    new_text = new.decode("utf-8").splitlines(keepends=True)
    header = [f"diff --git a/{path} b/{path}\n"]
    if old:
        header.append(f"index {blob_oid(old)[:7]}..{blob_oid(new)[:7]} 100644\n")
        header.extend((f"--- a/{path}\n", f"+++ b/{path}\n"))
    else:
        header.extend(("new file mode 100644\n", f"index 0000000..{blob_oid(new)[:7]}\n",
                       "--- /dev/null\n", f"+++ b/{path}\n"))
    body = list(difflib.unified_diff(old_text, new_text, fromfile="", tofile="", n=3, lineterm="\n"))
    hunks = body[2:]
    return "".join(header + hunks).encode("utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent)
    args = parser.parse_args(argv)
    repo = args.repo.resolve(strict=True)
    head = run(repo, "rev-parse", "HEAD").decode().strip()
    if head != BASE:
        raise SystemExit(f"refusing API patch capture from {head}; expected exact base {BASE}")
    paths = []
    chunks = []
    for rel in TRACKED:
        old = run(repo, "show", f"{BASE}:{rel}")
        new = (repo / rel).read_bytes()
        if old != new:
            chunks.append(file_patch(rel, old, new))
            normalized = new.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            paths.append({"path": rel, "kind": "modified", "base_blob": blob_oid(old),
                          "result_sha256": hashlib.sha256(normalized).hexdigest()})
    for rel in ADDED:
        path = repo / rel
        if not path.is_file():
            raise SystemExit(f"required API source addition missing: {rel}")
        new = path.read_bytes()
        chunks.append(file_patch(rel, b"", new))
        normalized = new.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        paths.append({"path": rel, "kind": "added", "result_sha256": hashlib.sha256(normalized).hexdigest()})
    out_dir = args.output_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    patch = b"\n".join(chunks)
    (out_dir / "api-v31.patch").write_bytes(patch)
    metadata = {"base_commit": BASE, "base_label": "Strata API v0.1.31-era source",
                "base_is_v138_engine": False, "patch_sha256": hashlib.sha256(patch).hexdigest(),
                "files": paths, "generated_from": "explicit API path allowlist only"}
    (out_dir / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
