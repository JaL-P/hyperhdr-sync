"""
Fetch Butterchurn and its presets for the MilkDrop effect.

Butterchurn (https://github.com/jberg/butterchurn, MIT) is MilkDrop rewritten for WebGL;
butterchurn-presets (MIT) holds the presets Music Assistant's visualizer uses too. The
bridge runs them in a headless Chromium page. This script downloads the pinned releases
from npm, checks them against their published SHA-512 and keeps only the files the page
loads, with their licences, in ``standalone/vendor``. The add-on's Dockerfile runs it;
run it once yourself for the standalone bridge.
"""

from __future__ import annotations

import base64
import hashlib
import io
import sys
import tarfile
import urllib.request
from pathlib import Path

VENDOR = Path(__file__).resolve().parent / "vendor"
# package, version, published integrity, {file in the package: name in vendor/}
PACKAGES = (
    (
        "butterchurn",
        "3.0.0-beta.5",
        "sha512-BStK4OAbBb9Pvt8PuQlS4WVmYBwU1KuDMRHF1V89QjoIFauAqq7tpV4EpYXj7K563r5daLrMX+2y5DBhZZ9Xig==",
        {
            "dist/butterchurn.min.js": "butterchurn.min.js",
            "LICENSE": "LICENSE.butterchurn",
        },
    ),
    (
        "butterchurn-presets",
        "3.0.0-beta.4",
        "sha512-TbQLUPvGOYMZAtWKoCmBtludh9aQZ6NaMGQU4lvPeadBPy3Du3yNmwBjlTMLP5c5mRWElxQPjTL1PtR7FZK3OQ==",
        {
            "dist/all.js": "butterchurn-presets.js",
            "LICENSE": "LICENSE.butterchurn-presets",
        },
    ),
)


def fetch(vendor: Path = VENDOR) -> None:
    """
    Download, check and unpack the packages into ``vendor``.

    :param vendor: Where the files go.
    :raises ValueError: When a download does not match its published checksum.
    """
    vendor.mkdir(parents=True, exist_ok=True)
    for name, version, integrity, files in PACKAGES:
        url = f"https://registry.npmjs.org/{name}/-/{name}-{version}.tgz"
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read()
        digest = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
        if digest != integrity:
            raise ValueError(f"{name} {version}: checksum mismatch, refusing to use it")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for inner, target in files.items():
                member = archive.extractfile(f"package/{inner}")
                if member is None:
                    raise ValueError(f"{name} {version}: {inner} is missing")
                (vendor / target).write_bytes(member.read())
        sys.stdout.write(f"Fetched {name} {version}\n")


if __name__ == "__main__":
    fetch(Path(sys.argv[1]) if len(sys.argv) > 1 else VENDOR)
