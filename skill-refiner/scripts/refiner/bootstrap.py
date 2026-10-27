"""Install a verified, pinned upstream release into a project-local directory."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import platform
import shutil
import tarfile
import urllib.request
import zipfile

from .common import RefinerError, write_json

VERSION = "0.13.0"
BUNDLE_ROOT = Path(__file__).resolve().parents[1] / "vendor" / "skill-up"
_source_root = Path(__file__).resolve().parents[3]
PROJECT_ROOT = _source_root if (_source_root / "pyproject.toml").is_file() else Path.cwd()


def install(destination=None):
    dest = Path(destination) if destination else PROJECT_ROOT / ".tools"
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "windows"}.get(platform.system())
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "AMD64": "amd64"}.get(platform.machine())
    if not system or not arch:
        raise RefinerError("Unsupported platform for the pinned skill-up binary")
    binary = "skill-up.exe" if system == "windows" else "skill-up"
    if system == "windows" and arch == "amd64":
        bundle_dir = BUNDLE_ROOT / f"{system}-{arch}"
        bundled = bundle_dir / binary
        manifest_path = bundle_dir / "manifest.json"
        if not bundled.is_file() or not manifest_path.is_file():
            raise RefinerError("Bundled Windows x64 skill-up is missing; this Skill package is incomplete")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (manifest.get("version") != VERSION or manifest.get("platform") != system
                or manifest.get("architecture") != arch or manifest.get("binary") != binary
                or manifest.get("license") != "Apache-2.0"):
            raise RefinerError("Bundled skill-up manifest does not match the supported release")
        digest = hashlib.sha256()
        with bundled.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
        if actual != manifest.get("binary_sha256"):
            raise RefinerError("Bundled skill-up binary checksum mismatch")
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / binary
        if bundled.resolve() != target.resolve():
            shutil.copyfile(bundled, target)
        target.chmod(0o755)
        write_json(dest / "skill-up.lock.json", {"version": VERSION,
                   "archive": manifest["archive"], "sha256": manifest["archive_sha256"],
                   "url": manifest["source_url"], "binary_sha256": actual, "bundled": True})
        return target
    extension = "zip" if system == "windows" else "tar.gz"
    name = f"skill-up_{VERSION}_{system}_{arch}.{extension}"
    base = f"https://github.com/alibaba/skill-up/releases/download/v{VERSION}/"

    def fetch(url):
        with urllib.request.urlopen(url, timeout=90) as response:
            return response.read()

    checksums = fetch(base + f"skill-up_{VERSION}_checksums.txt").decode()
    expected = next((line.split()[0] for line in checksums.splitlines() if line.split()[-1] == name), None)
    if not expected:
        raise RefinerError(f"Upstream checksum missing for {name}")
    content = fetch(base + name)
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected:
        raise RefinerError("Downloaded skill-up archive checksum mismatch")
    binary = "skill-up.exe" if system == "windows" else "skill-up"
    if extension == "zip":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            payload = archive.read(binary)
    else:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            member = next(m for m in archive.getmembers() if Path(m.name).name == binary and m.isfile())
            payload = archive.extractfile(member).read()
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / binary
    target.write_bytes(payload)
    target.chmod(0o755)
    write_json(dest / "skill-up.lock.json", {"version": VERSION, "archive": name,
               "sha256": actual, "url": base + name})
    return target

