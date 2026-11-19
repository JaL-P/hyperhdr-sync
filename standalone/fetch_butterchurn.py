"""
Fetch Butterchurn and its presets for the MilkDrop effect.

Butterchurn (https://github.com/jberg/butterchurn, MIT) is MilkDrop rewritten for WebGL;
butterchurn-presets (MIT) holds the presets. The versions and packs are the ones Music
Assistant's web interface bundles for its visualizer (Butterchurn 3.0.0-beta.5, presets
2.4.7 base and extra: 245 presets), so the LEDs show the same presets. The
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
        "2.4.7",
        "sha512-4MdM8ripz/VfH1BCldrIKdAc/1ryJFBDvqlyow6Ivo1frwj0H3duzvSMFC7/wIjAjxb1QpwVHVqGqS9uAFKhpg==",
        {
            "lib/butterchurnPresets.min.js": "butterchurn-presets.min.js",
            "lib/butterchurnPresetsExtra.min.js": "butterchurn-presets-extra.min.js",
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
