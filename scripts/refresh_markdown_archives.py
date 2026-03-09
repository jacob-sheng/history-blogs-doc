#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path


ARCHIVE_ROOT = "archive"
ARCHIVE_CN_ROOT = "archive-cn"
TIMESTAMP_RE = re.compile(r"^\d{8}-\d{6}_.+\.md$")


def run_git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def list_changed_markdown_files(repo_root: Path, before_sha: str, current_sha: str) -> list[str]:
    zero_sha = "0000000000000000000000000000000000000000"
    if before_sha == zero_sha:
        output = run_git(repo_root, "ls-tree", "-r", "--name-only", "HEAD")
        candidates = output.splitlines()
    else:
        output = run_git(
            repo_root,
            "diff",
            "--name-only",
            "--diff-filter=AM",
            before_sha,
            current_sha,
        )
        candidates = output.splitlines()

    seen = set()
    files: list[str] = []
    for raw_path in candidates:
        path = raw_path.strip()
        if not path or path in seen:
            continue
        seen.add(path)
        if not path.lower().endswith(".md"):
            continue
        if path.startswith(f"{ARCHIVE_ROOT}/") or path.startswith(f"{ARCHIVE_CN_ROOT}/"):
            continue
        if not (repo_root / path).is_file():
            continue
        files.append(path)
    return files


def shanghai_timestamp() -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz=tz).strftime("%Y%m%d-%H%M%S")


def write_github_output(values: dict[str, str]) -> None:
    output_file = os.environ.get("GITHUB_OUTPUT")
    if not output_file:
        return
    with open(output_file, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--before-sha", required=True)
    parser.add_argument("--current-sha", required=True)
    parser.add_argument("--manifest-path", required=True)
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    manifest_path = Path(args.manifest_path).resolve()

    changed_files = list_changed_markdown_files(repo_root, args.before_sha, args.current_sha)
    if not changed_files:
        manifest = {
            "changed": False,
            "timestamp": None,
            "entries": [],
        }
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        write_github_output({"changed": "false", "manifest_path": str(manifest_path), "entry_count": "0"})
        return 0

    timestamp = shanghai_timestamp()
    entries = []
    changed = False

    for source_path in changed_files:
        source_file = repo_root / source_path
        source_dir = Path(source_path).parent
        archive_dir = repo_root / ARCHIVE_ROOT / source_dir if str(source_dir) != "." else repo_root / ARCHIVE_ROOT
        archive_cn_dir = repo_root / ARCHIVE_CN_ROOT / source_dir if str(source_dir) != "." else repo_root / ARCHIVE_CN_ROOT
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_cn_dir.mkdir(parents=True, exist_ok=True)

        base_name = Path(source_path).name
        for cleanup_dir in (archive_dir, archive_cn_dir):
            for existing in cleanup_dir.glob(f"*_{base_name}"):
                if not TIMESTAMP_RE.match(existing.name):
                    continue
                existing.unlink()
                changed = True

        archive_path = archive_dir / f"{timestamp}_{base_name}"
        shutil.copy2(source_file, archive_path)
        changed = True
        entries.append(
            {
                "source_path": source_path.replace("\\", "/"),
                "archive_path": archive_path.relative_to(repo_root).as_posix(),
            }
        )

    manifest = {
        "changed": changed,
        "timestamp": timestamp,
        "entries": entries,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    write_github_output(
        {
            "changed": "true" if changed else "false",
            "manifest_path": str(manifest_path),
            "entry_count": str(len(entries)),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
