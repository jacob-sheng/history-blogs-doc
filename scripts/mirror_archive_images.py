#!/usr/bin/env python3
import argparse
import hashlib
import json
import mimetypes
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


RAW_URL_RE = re.compile(r"https://raw\.githubusercontent\.com/[^\s\"'<>)]+" )
DEFAULT_PICHUB_ENDPOINT = "https://api.pichub.app/api/v1/upload"
DEFAULT_SUPERBED_ENDPOINT = "https://api.superbed.cn/upload"


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_repo_image_path(url: str, photobed_root: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc != "raw.githubusercontent.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4:
        return None
    repo_path = "/".join(parts[3:])
    if not repo_path.startswith(f"{photobed_root}/"):
        return None
    return repo_path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def encode_multipart(fields: dict[str, str], files: list[tuple[str, str, bytes]]) -> tuple[bytes, str]:
    boundary = f"----CodexBoundary{hashlib.sha256(os.urandom(16)).hexdigest()[:24]}"
    body = bytearray()
    for name, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")
    for field_name, filename, content in files:
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode("utf-8")
        )
        body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
        body.extend(content)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def post_json_request(
    url: str,
    fields: dict[str, str],
    files: list[tuple[str, str, bytes]],
    headers: dict[str, str] | None = None,
    timeout: int = 60,
):
    body, content_type = encode_multipart(fields, files)
    request_headers = {"Content-Type": content_type, "Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url=url, data=body, headers=request_headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw)


def extract_first_url(payload):
    if isinstance(payload, str):
        if payload.startswith("http://") or payload.startswith("https://"):
            return payload
        return None
    if isinstance(payload, list):
        for item in payload:
            found = extract_first_url(item)
            if found:
                return found
        return None
    if isinstance(payload, dict):
        preferred_keys = (
            "url",
            "src",
            "link",
            "public_url",
            "image_url",
            "display_url",
            "markdown_url",
        )
        for key in preferred_keys:
            value = payload.get(key)
            found = extract_first_url(value)
            if found:
                return found
        for value in payload.values():
            found = extract_first_url(value)
            if found:
                return found
    return None


class UploadError(Exception):
    pass


class PicHubUploader:
    name = "pichub"

    def __init__(self):
        self.token = os.environ.get("PICHUB_TOKEN", "").strip()
        self.endpoint = os.environ.get("PICHUB_UPLOAD_URL", DEFAULT_PICHUB_ENDPOINT).strip()

    def available(self) -> bool:
        return bool(self.token)

    def upload(self, file_path: Path, stable_name: str) -> str:
        with file_path.open("rb") as handle:
            payload = post_json_request(
                self.endpoint,
                {
                    "quality": "85",
                    "is_public": "1",
                },
                [("files[]", stable_name, handle.read())],
                headers={"Authorization": f"Bearer {self.token}"},
            )
        if not payload.get("success"):
            raise UploadError(payload.get("message") or "PicHub upload failed")
        url = extract_first_url(payload.get("data"))
        if not url:
            raise UploadError("PicHub response did not include a usable URL")
        return url


class SuperbedUploader:
    name = "superbed"

    def __init__(self):
        self.token = os.environ.get("SUPERBED_TOKEN", "").strip()
        self.endpoint = os.environ.get("SUPERBED_UPLOAD_URL", DEFAULT_SUPERBED_ENDPOINT).strip()
        self.categories = os.environ.get("SUPERBED_CATEGORIES", "").strip()

    def available(self) -> bool:
        return bool(self.token)

    def upload(self, file_path: Path, stable_name: str) -> str:
        fields = {
            "token": self.token,
            "filename": stable_name,
        }
        if self.categories:
            fields["categories"] = self.categories
        with file_path.open("rb") as handle:
            payload = post_json_request(
                self.endpoint,
                fields,
                [("file", stable_name, handle.read())],
            )
        if payload.get("err") not in (0, "0", None) and not payload.get("url"):
            raise UploadError(payload.get("msg") or "Superbed upload failed")
        url = extract_first_url(payload)
        if not url:
            raise UploadError("Superbed response did not include a usable URL")
        return url


def build_uploaders(provider_names: list[str]):
    registry = {
        "pichub": PicHubUploader,
        "superbed": SuperbedUploader,
    }
    uploaders = []
    for name in provider_names:
        cls = registry.get(name)
        if cls:
            uploaders.append(cls())
    return uploaders


def replace_urls(content: str, mapping: dict[str, str]) -> str:
    updated = content
    for source_url, mirror_url in mapping.items():
        updated = updated.replace(source_url, mirror_url)
    return updated


def append_summary(lines: list[str]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def mirror_file(
    repo_root: Path,
    archive_path: str,
    destination_path: Path,
    photobed_root: str,
    cache: dict,
    uploaders,
    stats: dict,
) -> None:
    content = (repo_root / archive_path).read_text(encoding="utf-8")
    urls = {match.group(0) for match in RAW_URL_RE.finditer(content)}
    replacements: dict[str, str] = {}

    for github_url in sorted(urls):
        repo_image_path = parse_repo_image_path(github_url, photobed_root)
        if not repo_image_path:
            continue
        source_file = repo_root / repo_image_path
        if not source_file.is_file():
            stats["missing_files"].append(repo_image_path)
            continue

        file_hash = sha256_file(source_file)
        cache_items = cache.setdefault("items", {})
        cached = cache_items.get(file_hash)
        if cached and cached.get("status") == "uploaded" and cached.get("mirror_url"):
            replacements[github_url] = cached["mirror_url"]
            stats["cache_hits"] += 1
            continue

        stable_name = f"{file_hash[:16]}-{source_file.name}"
        upload_result = None
        for uploader in uploaders:
            if not uploader.available():
                stats["provider_skips"][uploader.name] = stats["provider_skips"].get(uploader.name, 0) + 1
                continue
            try:
                mirror_url = uploader.upload(source_file, stable_name)
                upload_result = {
                    "source_path": repo_image_path,
                    "github_url": github_url,
                    "mirror_url": mirror_url,
                    "provider": uploader.name,
                    "hash": file_hash,
                    "status": "uploaded",
                }
                stats["provider_success"][uploader.name] = stats["provider_success"].get(uploader.name, 0) + 1
                stats["uploaded"] += 1
                replacements[github_url] = mirror_url
                break
            except (UploadError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
                stats["provider_failures"][uploader.name] = stats["provider_failures"].get(uploader.name, 0) + 1
                stats["errors"].append(f"{uploader.name}: {repo_image_path}: {exc}")

        if upload_result is None:
            upload_result = {
                "source_path": repo_image_path,
                "github_url": github_url,
                "mirror_url": github_url,
                "provider": "github",
                "hash": file_hash,
                "status": "fallback",
            }
            stats["fallbacks"] += 1
            replacements[github_url] = github_url

        cache_items[file_hash] = upload_result

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination_path.write_text(replace_urls(content, replacements), encoding="utf-8")
    stats["generated_files"].append(destination_path.relative_to(repo_root).as_posix())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--manifest-path", required=True)
    parser.add_argument("--cache-path", default="archive/_image-mirror-map.json")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    manifest_path = Path(args.manifest_path).resolve()
    cache_path = (repo_root / args.cache_path).resolve()

    manifest = load_json(manifest_path, {"changed": False, "entries": []})
    if not manifest.get("changed") or not manifest.get("entries"):
        append_summary(["## CN mirror archive", "- No changed markdown snapshots to mirror."])
        return 0

    photobed_root = os.environ.get("PHOTOBED_ROOT", "photobed").strip().strip("/")
    provider_names = [
        name.strip().lower()
        for name in os.environ.get("CN_MIRROR_PROVIDERS", "pichub,superbed").split(",")
        if name.strip()
    ]
    uploaders = build_uploaders(provider_names)
    cache = load_json(cache_path, {"version": 1, "items": {}})

    stats = {
        "uploaded": 0,
        "fallbacks": 0,
        "cache_hits": 0,
        "provider_success": {},
        "provider_failures": {},
        "provider_skips": {},
        "generated_files": [],
        "missing_files": [],
        "errors": [],
    }

    for entry in manifest["entries"]:
        archive_path = entry["archive_path"]
        if not archive_path.startswith("archive/"):
            continue
        destination_rel = "archive-cn/" + archive_path[len("archive/") :]
        mirror_file(
            repo_root=repo_root,
            archive_path=archive_path,
            destination_path=repo_root / destination_rel,
            photobed_root=photobed_root,
            cache=cache,
            uploaders=uploaders,
            stats=stats,
        )

    save_json(cache_path, cache)

    summary_lines = [
        "## CN mirror archive",
        f"- Generated files: {len(stats['generated_files'])}",
        f"- Uploaded images: {stats['uploaded']}",
        f"- Cache hits: {stats['cache_hits']}",
        f"- GitHub fallback images: {stats['fallbacks']}",
    ]
    if stats["provider_success"]:
        for provider, count in sorted(stats["provider_success"].items()):
            summary_lines.append(f"- Provider success `{provider}`: {count}")
    if stats["provider_failures"]:
        for provider, count in sorted(stats["provider_failures"].items()):
            summary_lines.append(f"- Provider failures `{provider}`: {count}")
    if stats["provider_skips"]:
        for provider, count in sorted(stats["provider_skips"].items()):
            summary_lines.append(f"- Provider skipped `{provider}`: {count}")
    if stats["missing_files"]:
        preview = ", ".join(stats["missing_files"][:5])
        suffix = " ..." if len(stats["missing_files"]) > 5 else ""
        summary_lines.append(f"- Missing local source files: {preview}{suffix}")
    if stats["errors"]:
        summary_lines.append("- Sample upload errors:")
        for message in stats["errors"][:5]:
            summary_lines.append(f"  - {message}")
    append_summary(summary_lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
