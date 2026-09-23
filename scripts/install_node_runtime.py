"""Install a checksum-pinned Node binary for this project only; never downloads."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
from uuid import uuid4


def no_links(path):
    if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
        raise ValueError("runtime path cannot traverse a link")


def install(project, archive, pin_path):
    if sys.platform != "linux" or os.geteuid() == 0:
        raise ValueError("use the existing Linux project owner, without sudo")
    project, archive, pin_path = Path(project).absolute(), Path(archive).absolute(), Path(pin_path).absolute()
    no_links(project); no_links(archive); no_links(pin_path)
    if project.stat().st_uid != os.geteuid() or not (project / "src/ashare_daily").is_dir():
        raise ValueError("not the owned A-share project")
    pin = json.loads(pin_path.read_text(encoding="utf-8"))
    if (pin.get("schema_version") != "node-runtime-v1" or pin.get("platform") != "linux-x64"
            or pin.get("executable") != ".tools/node/bin/node"
            or not re.fullmatch(r"v\d+\.\d+\.\d+", pin.get("version", ""))):
        raise ValueError("unexpected runtime pin contract")
    if any(not re.fullmatch(r"[a-f0-9]{64}", pin.get(k, "")) for k in ("archive_sha256", "executable_sha256")):
        raise ValueError("runtime pin requires SHA256")
    if archive.stat().st_size > 80_000_000 or hashlib.sha256(archive.read_bytes()).hexdigest() != pin["archive_sha256"]:
        raise ValueError("official runtime archive hash mismatch")
    prefix = "node-" + pin["version"] + "-linux-x64/"
    with tarfile.open(archive, "r:xz") as package:
        binary, license_file = package.getmember(prefix + "bin/node"), package.getmember(prefix + "LICENSE")
        if not binary.isfile() or not 1 <= binary.size <= 200_000_000 or not license_file.isfile() or license_file.size > 1_000_000:
            raise ValueError("unexpected runtime archive members")
        body, license_body = package.extractfile(binary).read(), package.extractfile(license_file).read()
    if hashlib.sha256(body).hexdigest() != pin["executable_sha256"]:
        raise ValueError("runtime executable hash mismatch")
    target = project / pin["executable"]
    no_links(target)
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != pin["executable_sha256"]:
        raise ValueError("existing project runtime differs; not overwritten")
    reused = target.exists()
    if not reused:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(".node-stage-" + uuid4().hex)
        with temporary.open("xb") as stream:
            stream.write(body)
        temporary.chmod(0o755)
        version = subprocess.run([str(temporary), "--version"], capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        if version != pin["version"]:
            temporary.unlink()
            raise ValueError("runtime executable version differs")
        temporary.replace(target)
    license_path = target.parent.parent / "LICENSE"
    no_links(license_path)
    if not license_path.exists():
        with license_path.open("xb") as stream:
            stream.write(license_body)
    return {"status": "already_installed" if reused else "installed", "executable": str(target),
        "version": pin["version"], "executable_sha256": pin["executable_sha256"],
        "archive_sha256": pin["archive_sha256"], "global_changes": False, "network_requests": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--pin", type=Path)
    args = parser.parse_args()
    print(json.dumps(install(args.project, args.archive, args.pin or args.project / "config/node_runtime.json"), indent=2))
