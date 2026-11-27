"""Verified, idempotent seed import. Supplied evidence stays byte-for-byte intact."""

from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path

from .models import Observation, encoded


def verify_manifest(root):
    root = Path(root).resolve()
    entries = json.loads((root / "history/manifest.json").read_text())["files"]
    for entry in entries:
        path = (root / entry["path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Manifest path escapes repository")
        raw = path.read_bytes()
        if len(raw) != entry["bytes"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError(f"Historical evidence failed verification: {entry['path']}")
    return len(entries)


def import_household(store, root):
    verify_manifest(root)
    path = Path(root) / "history/source/household-minute-source-10-to-17-oct.json"
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    data = json.loads(raw)
    if data["unit"] != "kW":
        raise ValueError("Unexpected source units")
    acquired = datetime.fromisoformat(data["captured_utc"]).timestamp()
    rows = []
    seen = set()
    for epoch, value, legacy_count in data["records"]:
        if epoch in seen or epoch % 60:
            raise ValueError("Duplicate or non-minute historical timestamp")
        seen.add(epoch)
        # Legacy counts establish neither full coverage nor fresh device updates.
        o = Observation("seed:household", "household_power", acquired, value, "kW",
                        raw_value=value, raw_unit="kW", interval_start=epoch,
                        provenance="historical-minute-mean;coverage-unverified")
        rows.append((o.id, o.source, o.metric, o.sample_at, encoded(o.__dict__)))
    with closing(store.connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM imports WHERE sha256=?", (digest,)).fetchone():
            return 0
        db.executemany("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?)", rows)
        db.execute("INSERT INTO imports VALUES(?,?)", (digest, encoded({"file": path.name, "rows": len(rows), "legacy_count_semantics": "not_device_freshness"})))
    return len(rows)

