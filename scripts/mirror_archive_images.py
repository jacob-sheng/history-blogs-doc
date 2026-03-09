#!/usr/bin/env python3
import argparse
import hashlib
import io
import json
import mimetypes
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover - handled by workflow dependency step
    Image = None
    ImageOps = None


RAW_URL_RE = re.compile(r"https://raw\.githubusercontent\.com/[^\s\"'<>)]+" )
DEFAULT_PICHUB_ENDPOINT = "https://api.pichub.app/api/v1/upload"
DEFAULT_SUPERBED_ENDPOINT = "https://api.superbed.cn/upload"
DEFAULT_USER_AGENT = "history-blogs-doc-cn-mirror/1.0"
DEFAULT_MAX_BYTES = 512000
BITMAP_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
WEBP_QUALITIES = (90, 84, 78, 72, 66, 60, 54, 48, 42, 36)
WEBP_SCALES = (1.0, 0.9, 0.8, 0.7, 0.6, 0.5)


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


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


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
    request_headers = {
        "Content-Type": content_type,
        "Accept": "application/json, text/plain, */*",
        "User-Agent": DEFAULT_USER_AGENT,
    }
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url=url, data=body, headers=request_headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            snippet = raw[:400].replace("\n", " ")
            raise UploadError(f"HTTP {exc.code}: {snippet}") from exc
        message = payload.get("message")
        if not message and isinstance(payload.get("error"), dict):
            message = payload["error"].get("message") or payload["error"].get("code")
        raise UploadError(f"HTTP {exc.code}: {message or payload}") from exc


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
            "thumbnail_url",
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


def human_bytes(size: int) -> str:
    units = ("B", "KB", "MB", "GB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)}{unit}"
            return f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{int(size)}B"


def parse_int_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name, "").strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return value if value > 0 else default


def normalize_bitmap(image):
    image = ImageOps.exif_transpose(image)
    has_transparency = image.mode in ("RGBA", "LA") or "transparency" in image.info
    if has_transparency:
        return image.convert("RGBA")
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def save_webp_bytes(image, quality: int) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="WEBP", quality=quality, method=6)
    return buffer.getvalue()


def prepare_upload_asset(source_file: Path, max_bytes: int) -> dict:
    original_bytes = source_file.read_bytes()
    original_size = len(original_bytes)
    suffix = source_file.suffix.lower()
    prepared = {
        "content": original_bytes,
        "filename": source_file.name,
        "original_size": original_size,
        "uploaded_size": original_size,
        "transformed": False,
    }

    if suffix not in BITMAP_SUFFIXES or original_size <= max_bytes:
        return prepared

    if Image is None or ImageOps is None:
        prepared["transform_error"] = "Pillow is unavailable"
        return prepared

    try:
        with Image.open(source_file) as raw_image:
            base_image = normalize_bitmap(raw_image)
            base_width, base_height = base_image.size
            best_candidate = None
            seen_sizes = set()

            for scale in WEBP_SCALES:
                width = max(1, int(round(base_width * scale)))
                height = max(1, int(round(base_height * scale)))
                if (width, height) in seen_sizes:
                    continue
                seen_sizes.add((width, height))

                if (width, height) == base_image.size:
                    scaled_image = base_image
                else:
                    scaled_image = base_image.resize((width, height), Image.Resampling.LANCZOS)

                for quality in WEBP_QUALITIES:
                    candidate_bytes = save_webp_bytes(scaled_image, quality)
                    candidate_size = len(candidate_bytes)
                    candidate = {
                        "content": candidate_bytes,
                        "filename": f"{source_file.stem}.webp",
                        "original_size": original_size,
                        "uploaded_size": candidate_size,
                        "transformed": True,
                    }
                    if candidate_size <= max_bytes:
                        return candidate
                    if best_candidate is None or candidate_size < best_candidate["uploaded_size"]:
                        best_candidate = candidate

            if best_candidate and best_candidate["uploaded_size"] < original_size:
                return best_candidate
    except Exception as exc:  # pragma: no cover - depends on file/Pillow runtime
        prepared["transform_error"] = str(exc)
        return prepared

    return prepared


class UploadError(Exception):
    pass


class PicHubUploader:
    name = "pichub"

    def __init__(self):
        self.endpoint = os.environ.get("PICHUB_UPLOAD_URL", DEFAULT_PICHUB_ENDPOINT).strip()
        self.accounts = self._load_accounts()
        self.next_account_index = 0
        self.last_attempt_accounts: list[str] = []
        self.last_success_account: str | None = None

    def _load_accounts(self) -> list[dict]:
        raw_pool = os.environ.get("PICHUB_TOKENS", "").strip()
        tokens = []
        if raw_pool:
            tokens = [item.strip() for item in re.split(r"[\r\n,]+", raw_pool) if item.strip()]
        else:
            single = os.environ.get("PICHUB_TOKEN", "").strip()
            if single:
                tokens = [single]

        accounts = []
        seen_tokens = set()
        for index, token in enumerate(tokens, start=1):
            if token in seen_tokens:
                continue
            seen_tokens.add(token)
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
            accounts.append(
                {
                    "token": token,
                    "label": f"pichub-{index:02d}-{token_hash}",
                }
            )
        return accounts

    def available(self) -> bool:
        return bool(self.accounts)

    def upload(self, file_content: bytes, stable_name: str) -> tuple[str, str]:
        attempts = (
            [("image", stable_name, file_content)],
            [("files[]", stable_name, file_content)],
        )
        self.last_attempt_accounts = []
        self.last_success_account = None
        last_error = None

        if not self.accounts:
            raise UploadError("PicHub token pool is empty")

        start_index = self.next_account_index
        self.next_account_index = (self.next_account_index + 1) % len(self.accounts)

        for offset in range(len(self.accounts)):
            account = self.accounts[(start_index + offset) % len(self.accounts)]
            self.last_attempt_accounts.append(account["label"])

            for file_fields in attempts:
                try:
                    payload = post_json_request(
                        self.endpoint,
                        {},
                        file_fields,
                        headers={"Authorization": f"Bearer {account['token']}"},
                    )
                except UploadError as exc:
                    last_error = UploadError(f"{account['label']}: {exc}")
                    continue

                if not payload.get("success"):
                    error_payload = payload.get("error")
                    message = payload.get("message")
                    if not message and isinstance(error_payload, dict):
                        message = error_payload.get("message") or error_payload.get("code")
                    last_error = UploadError(f"{account['label']}: {message or 'PicHub upload failed'}")
                    continue

                url = extract_first_url(payload.get("data"))
                if not url:
                    url = extract_first_url(payload.get("images"))
                if url:
                    self.last_success_account = account["label"]
                    return url, account["label"]
                last_error = UploadError(f"{account['label']}: PicHub response did not include a usable URL")

        raise last_error or UploadError("PicHub upload failed")


class SuperbedUploader:
    name = "superbed"

    def __init__(self):
        self.token = os.environ.get("SUPERBED_TOKEN", "").strip()
        self.endpoint = os.environ.get("SUPERBED_UPLOAD_URL", DEFAULT_SUPERBED_ENDPOINT).strip()
        self.categories = os.environ.get("SUPERBED_CATEGORIES", "").strip()
        self.account_label = "superbed-default"

    def available(self) -> bool:
        return bool(self.token)

    def upload(self, file_content: bytes, stable_name: str) -> tuple[str, str]:
        fields = {
            "token": self.token,
            "filename": stable_name,
        }
        if self.categories:
            fields["categories"] = self.categories
        payload = post_json_request(
            self.endpoint,
            fields,
            [("file", stable_name, file_content)],
        )
        if payload.get("err") not in (0, "0", None) and not payload.get("url"):
            raise UploadError(payload.get("msg") or "Superbed upload failed")
        url = extract_first_url(payload)
        if not url:
            raise UploadError("Superbed response did not include a usable URL")
        return url, self.account_label


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


def record_asset_stats(stats: dict, upload_hash: str, prepared_asset: dict) -> None:
    seen_hashes = stats.setdefault("seen_asset_hashes", set())
    if upload_hash in seen_hashes:
        return
    seen_hashes.add(upload_hash)
    stats["original_bytes_total"] += prepared_asset["original_size"]
    stats["uploaded_bytes_total"] += prepared_asset["uploaded_size"]
    if prepared_asset["transformed"]:
        stats["compressed_count"] += 1
    transform_error = prepared_asset.get("transform_error")
    if transform_error:
        stats["errors"].append(f"{prepared_asset['filename']}: compression fallback: {transform_error}")


def mirror_file(
    repo_root: Path,
    archive_path: str,
    destination_path: Path,
    photobed_root: str,
    cache: dict,
    uploaders,
    stats: dict,
    max_bytes: int,
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

        prepared_asset = prepare_upload_asset(source_file, max_bytes)
        upload_hash = sha256_bytes(prepared_asset["content"])
        cache_items = cache.setdefault("items", {})
        cached = cache_items.get(upload_hash)
        record_asset_stats(stats, upload_hash, prepared_asset)

        if cached and cached.get("status") == "uploaded" and cached.get("mirror_url"):
            replacements[github_url] = cached["mirror_url"]
            stats["cache_hits"] += 1
            continue

        upload_result = None
        stable_name = f"{upload_hash[:16]}-{prepared_asset['filename']}"
        for uploader in uploaders:
            if not uploader.available():
                stats["provider_skips"][uploader.name] = stats["provider_skips"].get(uploader.name, 0) + 1
                continue
            try:
                mirror_url, provider_account = uploader.upload(prepared_asset["content"], stable_name)
                upload_result = {
                    "source_path": repo_image_path,
                    "github_url": github_url,
                    "mirror_url": mirror_url,
                    "provider": uploader.name,
                    "provider_account": provider_account,
                    "hash": upload_hash,
                    "original_size": prepared_asset["original_size"],
                    "uploaded_size": prepared_asset["uploaded_size"],
                    "transformed": prepared_asset["transformed"],
                    "uploaded_filename": stable_name,
                    "status": "uploaded",
                }
                stats["provider_success"][uploader.name] = stats["provider_success"].get(uploader.name, 0) + 1
                stats["uploaded"] += 1
                if provider_account:
                    success_counts = stats["provider_account_success"].setdefault(uploader.name, {})
                    success_counts[provider_account] = success_counts.get(provider_account, 0) + 1
                replacements[github_url] = mirror_url
                break
            except (UploadError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
                stats["provider_failures"][uploader.name] = stats["provider_failures"].get(uploader.name, 0) + 1
                if uploader.name == "pichub":
                    failure_counts = stats["provider_account_failures"].setdefault("pichub", {})
                    for account_label in getattr(uploader, "last_attempt_accounts", []):
                        failure_counts[account_label] = failure_counts.get(account_label, 0) + 1
                stats["errors"].append(f"{uploader.name}: {repo_image_path}: {exc}")

        if upload_result is None:
            upload_result = {
                "source_path": repo_image_path,
                "github_url": github_url,
                "mirror_url": github_url,
                "provider": "github",
                "provider_account": "github-raw",
                "hash": upload_hash,
                "original_size": prepared_asset["original_size"],
                "uploaded_size": prepared_asset["uploaded_size"],
                "transformed": prepared_asset["transformed"],
                "uploaded_filename": stable_name,
                "status": "fallback",
            }
            stats["fallbacks"] += 1
            replacements[github_url] = github_url

        cache_items[upload_hash] = upload_result

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
    max_bytes = parse_int_env("CN_IMAGE_MAX_BYTES", DEFAULT_MAX_BYTES)
    provider_names = [
        name.strip().lower()
        for name in os.environ.get("CN_MIRROR_PROVIDERS", "pichub,superbed").split(",")
        if name.strip()
    ]
    uploaders = build_uploaders(provider_names)
    cache = load_json(cache_path, {"version": 2, "items": {}})

    stats = {
        "uploaded": 0,
        "fallbacks": 0,
        "cache_hits": 0,
        "compressed_count": 0,
        "original_bytes_total": 0,
        "uploaded_bytes_total": 0,
        "provider_success": {},
        "provider_failures": {},
        "provider_skips": {},
        "provider_account_success": {},
        "provider_account_failures": {},
        "generated_files": [],
        "missing_files": [],
        "errors": [],
        "seen_asset_hashes": set(),
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
            max_bytes=max_bytes,
        )

    save_json(cache_path, cache)

    summary_lines = [
        "## CN mirror archive",
        f"- Generated files: {len(stats['generated_files'])}",
        f"- Uploaded images: {stats['uploaded']}",
        f"- Cache hits: {stats['cache_hits']}",
        f"- GitHub fallback images: {stats['fallbacks']}",
        f"- Compressed images: {stats['compressed_count']}",
        f"- Original bytes total: {human_bytes(stats['original_bytes_total'])}",
        f"- Uploaded bytes total: {human_bytes(stats['uploaded_bytes_total'])}",
        f"- Max upload bytes target: {human_bytes(max_bytes)}",
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
    for provider, counts in sorted(stats["provider_account_success"].items()):
        for account_label, count in sorted(counts.items()):
            summary_lines.append(f"- Account success `{provider}:{account_label}`: {count}")
    for provider, counts in sorted(stats["provider_account_failures"].items()):
        for account_label, count in sorted(counts.items()):
            summary_lines.append(f"- Account failures `{provider}:{account_label}`: {count}")
    if stats["missing_files"]:
        preview = ", ".join(stats["missing_files"][:5])
        suffix = " ..." if len(stats["missing_files"]) > 5 else ""
        summary_lines.append(f"- Missing local source files: {preview}{suffix}")
    if stats["errors"]:
        summary_lines.append("- Sample upload errors:")
        for message in stats["errors"][:5]:
            summary_lines.append(f"  - {message}")
    append_summary(summary_lines)
    for line in summary_lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
