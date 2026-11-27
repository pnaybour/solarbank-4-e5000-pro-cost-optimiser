#!/usr/bin/env python3
"""Compare our pinned copy of Anker's official Home Assistant integration with the upstream repository (standard library only; needs the network).

    python3 scripts/check_anker_upstream.py            # report whether upstream has moved and which of our reference files changed
    python3 scripts/check_anker_upstream.py record     # (re)write source-manifest.json from the local copies and the pinned commit

The reference copies live in reference/anker-solix-official/source/ and are kept byte-for-byte as fetched. This script never changes them; to take an
upstream update, follow the procedure in ANKER_INTEGRATION_NOTES.md (section "Taking an upstream update"). Nothing here touches the equipment."""
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

REPO = "anker-charging/ha-anker-solix-official"
ROOT = Path(__file__).resolve().parents[1] / "reference" / "anker-solix-official"
SOURCE = ROOT / "source"
MANIFEST = ROOT / "source-manifest.json"
PINNED_YAMLS = ROOT / "manifest.json"                  # the original, untouched provenance record for the two pinned register maps


def git_blob_sha1(data):
    """The hash Git (and GitHub's tree listing) uses for a file's content."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def fetch_json(url):
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "solarbank-upstream-check"})
    with urllib.request.urlopen(request, timeout=30) as handle:
        return json.load(handle)


def upstream_tree(ref, fetch=fetch_json):
    """{path: git blob sha1} for every file at `ref`, and the commit it resolved to."""
    commit = fetch(f"https://api.github.com/repos/{REPO}/commits/{ref}")["sha"]
    tree = fetch(f"https://api.github.com/repos/{REPO}/git/trees/{commit}?recursive=1")
    if tree.get("truncated"):
        raise RuntimeError("GitHub returned a truncated listing")
    return commit, {e["path"]: e["sha"] for e in tree["tree"] if e["type"] == "blob"}


def local_files():
    return {p.relative_to(SOURCE).as_posix(): p.read_bytes() for p in sorted(SOURCE.rglob("*")) if p.is_file()}


def compare(manifest, tree):
    """What changed upstream for what we hold: files changed or removed, the two pinned device maps changed, and code or device-map files that did not exist when we last looked."""
    prefix = "custom_components/anker_solix_official/"
    changed = [f for f in manifest["files"] if f in tree and tree[f] != manifest["files"][f]["git_blob_sha1"]]
    removed = [f for f in manifest["files"] if f not in tree]
    maps = {prefix + "config/" + name: info for name, info in manifest["pinned_config_files"].items()}
    changed += [path for path, info in maps.items() if path in tree and tree[path] != info["git_blob_sha1"]]
    removed += [path for path in maps if path not in tree]
    known = set(manifest["files"]) | set(manifest["not_copied"]) | set(maps) | set(manifest["other_device_maps"])
    new = sorted(p for p in tree if p.startswith(prefix) and p.endswith((".py", ".yaml")) and p not in known and not p.startswith(prefix + "translations/"))
    return {"changed": changed, "removed": removed, "new": new}


def record(commit_ref):
    commit, tree = upstream_tree(commit_ref)
    files = local_files()
    problems = [f for f, data in files.items() if f not in tree or git_blob_sha1(data) != tree[f]]
    maps = {p.name: p.read_bytes() for p in sorted(ROOT.glob("*.yaml"))}
    prefix = "custom_components/anker_solix_official/"
    problems += [n for n, data in maps.items() if tree.get(prefix + "config/" + n) != git_blob_sha1(data)]
    if problems:
        raise SystemExit("These local copies do not match upstream at " + commit + ": " + ", ".join(problems))
    code = sorted(p for p in tree if p.startswith(prefix) and p.endswith(".py") and p not in files)
    others = sorted(p for p in tree if p.startswith(prefix + "config/") and p.endswith(".yaml") and p.rsplit("/", 1)[1] not in maps)
    manifest = {
        "repository": "https://github.com/" + REPO, "licence": "MIT, Copyright (c) 2026 Anker Innovations (LICENSE in this folder)", "commit": commit,
        "note": "Byte-for-byte copies of the vendor's files for reference and diffing. They need Home Assistant to run and are never imported. Adapted functions live in "
                "src/solarbank_optimiser/devices/anker_vendor.py; see ANKER_INTEGRATION_NOTES.md.",
        "files": {f: {"sha256": hashlib.sha256(d).hexdigest(), "git_blob_sha1": git_blob_sha1(d), "bytes": len(d)} for f, d in files.items()},
        "pinned_config_files": {n: {"sha256": hashlib.sha256(d).hexdigest(), "git_blob_sha1": git_blob_sha1(d), "bytes": len(d)} for n, d in maps.items()},
        "not_copied": code, "other_device_maps": others,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    print(f"Recorded {len(files)} source files and {len(maps)} device maps at {commit}")


def check():
    manifest = json.loads(MANIFEST.read_text())
    commit, tree = upstream_tree("HEAD")
    print(f"Pinned commit   {manifest['commit']}\nUpstream HEAD   {commit}")
    result = compare(manifest, tree)
    if commit == manifest["commit"] and not any(result.values()):
        print("Up to date: nothing has changed upstream.")
        return 0
    for title, key in (("Changed upstream", "changed"), ("Removed upstream", "removed"), ("New upstream (code or device maps we have not seen)", "new")):
        if result[key]:
            print(f"\n{title}:\n  " + "\n  ".join(result[key]))
    if commit != manifest["commit"] and not any(result.values()):
        print("\nUpstream has newer commits, but none touch the files we hold.")
    print("\nSee ANKER_INTEGRATION_NOTES.md, 'Taking an upstream update'.")
    return 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "record":
        record(sys.argv[2] if len(sys.argv) > 2 else json.loads(MANIFEST.read_text())["commit"] if MANIFEST.exists() else "HEAD")
    else:
        raise SystemExit(check())
