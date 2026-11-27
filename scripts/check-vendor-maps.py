"""Pin and watch the vendor's register maps (read-only, no repository or remote is configured).

    python scripts/check-vendor-maps.py snapshot [COMMIT]   fetch the two maps + licence at a commit and write the snapshot
    python scripts/check-vendor-maps.py check               compare upstream main with the snapshot; changes nothing

Source: https://github.com/anker-charging/ha-anker-solix-official (MIT, Copyright (c) 2026 Anker Innovations).
Needs PyYAML (development only). The runtime and the tests read only the committed JSON extract.
The extract keeps `read_quantities` and `batch_read_ranges`. The write, control and auto-mode sections are
deliberately left out; commissioning reads them from the original YAML when it needs them.
"""
import hashlib
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO = "anker-charging/ha-anker-solix-official"
BASE = "custom_components/anker_solix_official"
ROOT = Path(__file__).resolve().parents[1] / "reference" / "anker-solix-official"
MAPS = {      # role -> vendor file name (named by hash of the product code)
    "solarbank": "58f0132b5f7979b2cfa43a0eb1fca770053288032386ff6a4da5ed2d72d4ea35.yaml",
    "meter": "42bcf12f6663b19b5eaa517f050b6cbf286037eed3c569a5ccae6724c19de64e.yaml",
}


def fetch(ref, path):
    url = f"https://raw.githubusercontent.com/{REPO}/{ref}/{path}"
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read()


def extract(raw):
    document = yaml.safe_load(raw)
    ranges = document.get("batch_read_ranges", [])
    if isinstance(ranges, dict):                    # {input: ["10000-10050", ...], holding: [...]}
        ranges = [(kind, text) for kind, items in ranges.items() for text in items]
    else:                                           # ["10620-10647", ...] (all input registers)
        ranges = [("input", text) for text in ranges]
    quantities = {}
    for key, item in (document.get("read_quantities") or {}).items():
        quantities[key] = {name: item.get(name) for name in ("address", "data_type", "gain", "count", "unit", "register_type") if name in item}
    return {"product": document["product_info"]["default_name"],
            "batch_read_ranges": [{"type": kind, "start": int(a), "end": int(b)}
                                  for kind, text in ranges for a, b in [str(text).split("-")]],
            "read_quantities": quantities}


def snapshot(ref):
    ROOT.mkdir(parents=True, exist_ok=True)
    files, extracts = {}, {}
    licence = fetch(ref, "LICENSE")
    (ROOT / "LICENSE").write_bytes(licence)
    for role, name in MAPS.items():
        raw = fetch(ref, f"{BASE}/config/{name}")
        (ROOT / name).write_bytes(raw)
        files[role] = {"file": name, "sha256": hashlib.sha256(raw).hexdigest()}
        extracts[role] = extract(raw)
    (ROOT / "read-map-extract.json").write_text(json.dumps(extracts, indent=1, sort_keys=True) + "\n")
    manifest = {"repository": f"https://github.com/{REPO}", "licence": "MIT, Copyright (c) 2026 Anker Innovations",
                "commit": ref, "fetched_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "licence_sha256": hashlib.sha256(licence).hexdigest(), "maps": files,
                "note": "Original YAML kept unchanged for provenance. read-map-extract.json holds reads only."}
    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    print(f"Snapshot written for {ref}")


def check():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    committed = json.loads((ROOT / "read-map-extract.json").read_text())
    print(f"Pinned commit {manifest['commit'][:12]} ({manifest['fetched_utc']})")
    listing = json.load(urllib.request.urlopen(f"https://api.github.com/repos/{REPO}/contents/{BASE}/config", timeout=30))
    upstream = {item["name"] for item in listing}
    known = {entry["file"] for entry in manifest["maps"].values()}
    others = sorted(upstream - known)
    print(f"Upstream map files: {len(upstream)}; tracked here: {len(known)}; other models/devices: {len(others)}")
    problems = 0
    for role, entry in manifest["maps"].items():
        if entry["file"] not in upstream:
            print(f"  {role}: file no longer present upstream"); problems += 1; continue
        raw = fetch("main", f"{BASE}/config/{entry['file']}")
        if hashlib.sha256(raw).hexdigest() == entry["sha256"]:
            print(f"  {role}: unchanged"); continue
        new, old = extract(raw), committed[role]
        changes = []
        for key in sorted(set(new["read_quantities"]) | set(old["read_quantities"])):
            if new["read_quantities"].get(key) != old["read_quantities"].get(key):
                changes.append(f"    {key}: {old['read_quantities'].get(key)} -> {new['read_quantities'].get(key)}")
        if new["batch_read_ranges"] != old["batch_read_ranges"]:
            changes.append(f"    ranges: {old['batch_read_ranges']} -> {new['batch_read_ranges']}")
        print(f"  {role}: CHANGED upstream" + ("" if changes else " (comments or write sections only)"))
        print("\n".join(changes))
        problems += bool(changes)
    print("Nothing was changed. To adopt: snapshot <commit>, run the tests, update the readers if they fail.")
    return 1 if problems else 0


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "check"
    if command == "snapshot":
        snapshot(sys.argv[2] if len(sys.argv) > 2 else "21c94833993cc14dbb03fd1a945106f7b1afaafe")
    elif command == "check":
        sys.exit(check())
    else:
        sys.exit(__doc__)
