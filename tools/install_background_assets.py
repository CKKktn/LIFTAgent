"""Restore pinned local background scripts from checksum-verified npm archives."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = {
    "three": {
        "version": "0.134.0",
        "integrity": "sha512-LbBerg7GaSPjYtTOnu41AMp7tV6efUNR3p4Wk5NzkSsNTBuA5mDGOfwwZL1jhhVMLx9V20HolIUo0+U3AXehbg==",
        "files": {"package/build/three.min.js": "three.min.js", "package/LICENSE": "LICENSE-three.txt"},
    },
    "vanta": {
        "version": "0.5.24",
        "integrity": "sha512-fvieEbHy1ZS23zrcX+topzqAgA4Uct1enngOEWLFBgs9TtOf6RDFOYatH7KSVdrABzQDMCQ5myQy+nTSZZwLzg==",
        "files": {"package/dist/vanta.fog.min.js": "vanta.fog.min.js",
            "package/dist/vanta.net.min.js": "vanta.net.min.js",
            "package/dist/vanta.waves.min.js": "vanta.waves.min.js",
            "package/dist/vanta.birds.min.js": "vanta.birds.min.js",
            "package/dist/vanta.globe.min.js": "vanta.globe.min.js",
            "package/dist/vanta.halo.min.js": "vanta.halo.min.js",
            "package/dist/vanta.rings.min.js": "vanta.rings.min.js",
            "package/LICENSE.md": "LICENSE-vanta.txt"},
    },
}


def install(cache_dir):
    cache_dir = Path(cache_dir).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    assets, packages = {}, {}
    for name, spec in PACKAGES.items():
        filename = name + "-" + spec["version"] + ".tgz"
        url = "https://registry.npmjs.org/" + name + "/-/" + filename
        archive = cache_dir / filename
        if not archive.is_file():
            partial = archive.with_suffix(".part")
            subprocess.run(["curl.exe", "-s", "--show-error", "--fail", "--location", "--max-time", "60",
                "--max-filesize", "33554432", url, "--output", str(partial)], check=True)
            partial.replace(archive)
        actual = "sha512-" + base64.b64encode(hashlib.sha512(archive.read_bytes()).digest()).decode("ascii")
        if actual != spec["integrity"]:
            raise ValueError("Package checksum failed; no background asset was changed: " + name)
        with tarfile.open(archive, "r:gz") as source:
            for member_name, output_name in spec["files"].items():
                member = source.getmember(member_name)
                if not member.isfile() or not 0 < member.size < 2 * 1024 * 1024:
                    raise ValueError("Invalid background package member")
                with source.extractfile(member) as stream:
                    content = stream.read()
                if len(content) != member.size:
                    raise ValueError("Incomplete background package member")
                assets[output_name] = content
        packages[name] = {"version": spec["version"], "url": url, "integrity": actual, "license": "MIT"}
    # All inputs are verified before writing; never unpack arbitrary tar paths.
    output = ROOT / "frontend/vendor/vanta"
    output.mkdir(parents=True, exist_ok=True)
    manifest = {"packages": packages, "files": {}}
    for filename, content in assets.items():
        destination = output / filename
        partial = destination.with_suffix(destination.suffix + ".part")
        partial.write_bytes(content)
        partial.replace(destination)
        manifest["files"][filename] = {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    (output / "asset-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"files": len(assets), "bytes": sum(map(len, assets.values())), "destination": str(output)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "runtime-logs/background-assets")
    args = parser.parse_args()
    print(json.dumps(install(args.cache_dir), ensure_ascii=True))
