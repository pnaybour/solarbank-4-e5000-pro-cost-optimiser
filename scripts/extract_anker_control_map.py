#!/usr/bin/env python3
"""Turn the vendor's pinned Solarbank 4 register map (YAML) into JSON, so tests and tools can read it without PyYAML (the Pi has none).

    python3 scripts/extract_anker_control_map.py     # needs PyYAML on this machine; rewrites reference/anker-solix-official/control-map-extract.json

The YAML itself is never modified. The JSON holds the whole map (product info, batch read ranges, read quantities, write quantities, control items) plus the
SHA-256 of the YAML it came from. tests/test_anker_vendor.py checks src/solarbank_optimiser/devices/anker_vendor.py against it."""
import hashlib
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1] / "reference" / "anker-solix-official"
SOURCE = ROOT / "58f0132b5f7979b2cfa43a0eb1fca770053288032386ff6a4da5ed2d72d4ea35.yaml"
OUT = ROOT / "control-map-extract.json"

raw = SOURCE.read_bytes()
data = yaml.safe_load(raw)
OUT.write_text(json.dumps({"source": SOURCE.name, "source_sha256": hashlib.sha256(raw).hexdigest(), "map": data}, indent=1, sort_keys=True) + "\n")
print("Wrote", OUT, "from", SOURCE.name)
