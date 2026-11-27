"""A plain-words summary of the register write journal (`solarbank control writes`), for the logged-mode soak before go-live.

The journal has one JSON line per write the action machine made (`wrote`), tried and failed (`failed`) or would have made (`would_write`). In logged mode every line must be
`would_write`: a `wrote` or `failed` line means a register write reached (or tried to reach) the battery, and the command exits 1 so a script notices.
"""
from __future__ import annotations

import collections
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

LONDON = ZoneInfo("Europe/London")
REGISTERS = {10064: "operating mode", 10071: "battery power set-point"}
MODES = {0: "0 (self-consumption)", 3: "3 (the mode the controller drives)", 6: "6 (the Anker's own Dynamic Tariff)"}


def read_journal(path):
    rows, bad = [], 0
    for name in (str(path) + ".1", str(path)):                      # the rotated file first, so entries stay in order
        try:
            lines = Path(name).read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows, bad


def _when(row):
    """The wall-clock time of the entry in London time (`wall`; older entries only have `t`, the controller's own monotonic seconds, shown as it is)."""
    for key in ("wall", "t"):
        t = row.get(key)
        if isinstance(t, (int, float)) and t > 1e9:
            return datetime.fromtimestamp(t, LONDON).strftime("%d %b %H:%M:%S")
    return f"t={row.get('t')}"


def _what(row):
    if row.get("kind") == "record":                                  # an observation (specification 11.4, 10.4; GitHub issue 72), not a write
        return f"record: {row.get('record')}"
    register, value = row.get("register"), row.get("value")
    name = REGISTERS.get(register, f"register {register}")
    shown = MODES.get(value, str(value)) if register == 10064 else f"{value} W"
    return f"{name} -> {shown}"


def summarise_writes(path, last=8):
    """(text, sent): `sent` is True when any line says a write was actually sent or attempted."""
    rows, bad = read_journal(path)
    observed = [r for r in rows if r.get("kind") == "record"]
    if not [r for r in rows if r.get("kind") != "record"] and not observed:
        return (f"No register writes journaled at {path}.\nThat is normal in shadow mode: the journal starts when the mode file says `logged` (or `live`) and the controller "
                "has been restarted."), False
    kinds = collections.Counter(r.get("kind") for r in rows)
    sent = bool(kinds.get("wrote") or kinds.get("failed"))
    out = [f"{len(rows)} journal entries from {_when(rows[0])} to {_when(rows[-1])}" + (f" ({bad} unreadable lines skipped)" if bad else ""),
           "Kinds: " + ", ".join(f"{k} {n}" for k, n in sorted(kinds.items(), key=lambda kv: str(kv[0])))]
    if sent:
        out.append("*** A write was SENT or attempted (kind 'wrote' or 'failed'). In logged mode none should be. Do not go live until this is understood. ***")
    else:
        out.append("Every entry is a would-be write: nothing was sent to the battery.")
        out.append("This was logged mode. It does NOT show: the identity check, the write-switch gate, mode-reversion supervision or HANDS_OFF, effect checks, or failed-write handling (commissioning tests).")
    out.append("What it would write (register -> value: times):")
    for (register, value), n in collections.Counter((r.get("register"), r.get("value")) for r in rows if r.get("kind") != "record").most_common(8):
        out.append(f"  {_what({'register': register, 'value': value})}: {n}")
    if observed:
        out.append("Records (what the battery showed, not writes): " + "; ".join(f"{k} ({n})" for k, n in collections.Counter(r.get("record") for r in observed).most_common()))
    why = collections.Counter(r.get("why_not_sent") for r in rows if r.get("why_not_sent"))
    if why:
        out.append("Why not sent: " + "; ".join(f"{k} ({n})" for k, n in why.most_common(3)))
    out.append(f"Newest {min(last, len(rows))}:")
    for r in rows[-last:]:
        out.append(f"  {_when(r)}  {r.get('kind')}  {_what(r)}  [{r.get('reason', '')}]")
    return "\n".join(out), sent
