"""Install a verified, pinned upstream release into a project-local directory."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import platform
import tarfile
import urllib.request
import zipfile

from .common import RefinerError, write_json

VERSION = "0.13.0"
_source_root = Path(__file__).resolve().parents[3]
PROJECT_ROOT = _source_root if (_source_root / "pyproject.toml").is_file() else Path.cwd()


def install(destination=None):
    dest = Path(destination) if destination else PROJECT_ROOT / ".tools"
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "windows"}.get(platform.system())
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "AMD64": "amd64"}.get(platform.machine())
    if not system or not arch:
        raise RefinerError("Unsupported platform for the pinned skill-up binary")
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

