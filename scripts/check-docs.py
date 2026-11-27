#!/usr/bin/env python3
"""Consistency check for the planning documents. Standard library only. Run it before every commit to the post-shadow-fixes branch:

    python3 scripts/check-docs.py        # from any folder; prints the problems and exits 1 when the documents disagree
    python3 scripts/check-docs.py --root DIR   # check another copy of the documents, for example an older commit

POST_SHADOW_PLAN.md (working rule 9) makes the 'After' columns of section 4 the only dependency list. This script derives the rest from them:

  * every work package, decision and contract that a document mentions is defined, and every active package is in exactly one release and
    one lane;
  * no release ships a package before something it waits for (a soak such as "plus two weeks" needs an earlier release), and no lane puts
    a package before something it waits for;
  * the 'Covers' line and the issue index agree, and every issue labelled go-live blocker belongs to a gate;
  * pinned library versions, the specification version and the test count are quoted the same way everywhere;
  * phrases the revised approach retired do not come back, and the documents link only to files that exist or that the plan lists as missing.

It does not run the test suite. The archive of documents that the deploy script ships is checked by tests/test_docs_in_archive.py.
"""
import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[sys.argv.index("--root") + 1]).resolve() if "--root" in sys.argv else Path(__file__).resolve().parent.parent
DOCS = ["README.md", "SOFTWARE_SPECIFICATION.md", "REQUIREMENTS_AMENDMENTS.md", "DEVELOPMENT_PLAN.md", "OPERATIONS.md",
        "deployment/README.md", "POST_SHADOW_PLAN.md"]
PINS = {"transitions": "0.9.3", "whisper": "1.1.10", "Hypothesis": "6.170.14"}
WP = re.compile(r"WP\d+[abc]?")
# Phrases that earlier versions of the documents used and that are now wrong. Add a line when a review retires a statement.
STALE = {
    r"Tier F": "the plan no longer has a Tier F",
    r"Skip if D5": "D5 is resolved",
    r"Trial `transitions`": "transitions is adopted, not trialled",
    r"used only if the WP11 trial": "transitions is adopted, not trialled",
    r"Whisper move comes last": "Whisper is a foundation",
    r"long-term Whisper move": "Whisper is a foundation",
    r"WP22, WP23\b": "WP22 is retired and WP23 is split",
    r"Shelly channel check in step 0": "the Shelly mapping is established",
    r"the code's `ch[12]`": "the code names the channels by role since WP25",
    r"code calls `ch[12]`": "the code names the channels by role since WP25",
    r"an undeclared trigger raises `MachineError`": "in transitions 0.9.3 an undeclared name raises AttributeError; MachineError is for a declared event in the wrong state",
    r"none blocks A to D": "Stage E1 and E2 block unattended operation (gate G3)",
    r"about 90 MB": "with the count files the telemetry folder is about 125 MB",
    r"SOFTWARE_SPECIFICATION\.md version 1\.7": "the specification is at 1.8",
    r"\| 0\.5\.46 \| WP23b, WP23c": "WP23c ships in a later release than WP23b (rollback contract RC1)",
    r"Four separate systemd services": "the controller is the fifth service",
    r"solarbank-optimiser-\{web,collector,providers,shadow\}": "the status command must include the controller",
    r"No car yet": "a car now exists (owner, 2 November)",
    r"waived (in writing )?by the owner": "D16 is resolved: a car exists, so nothing is waived",
    r"hand back only when VALID": "the conditional hold was withdrawn; every release writes the default mode (LC1)",
    r"still hands back at any time": "software cannot hand control back to the Anker app",
    r"cannot send anything yet": "the sending transport is part of every release; `live` sends when the three root-owned conditions hold (GitHub 55)",
    r"No transport that sends is installed": "the sending transport is part of every release (GitHub 55)",
    r"no commissioned command transport is installed\"\)": "the sending transport is part of every release (GitHub 55)",
    r"`WP1` to `WP25`": "the plan has WP26",
    r"stay off until a car exists": "a car now exists",
}


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def section(text, start, end=None):
    head = text.split(start, 1)
    if len(head) < 2:
        return ""
    body = head[1]
    return body.split(end, 1)[0] if end and end in body else body


def main():
    problems = []
    plan = read("POST_SHADOW_PLAN.md")
    texts = {name: read(name) for name in DOCS}

    # ---- the work packages and their dependency column ----------------------------------------------------------------------
    after, retired = {}, set()
    for line in plan.splitlines():
        if re.match(r"\| WP\d+[abc]? \|", line):
            row = cells(line)
            if len(row) != 6:
                problems.append(f"{row[0]}: the row has {len(row)} cells, expected 6 (a stray '|' in a cell?)")
                continue
            after[row[0]] = row[5]
            if row[2].startswith("**Superseded") or row[2].startswith("**Retired"):
                retired.add(row[0])
    defined = set(after)
    active = sorted(defined - retired, key=lambda s: (int(re.match(r"WP(\d+)", s).group(1)), s))
    deps = {wp: [d for d in WP.findall(after[wp]) if d in defined and d not in retired] for wp in defined}
    for wp, cell in after.items():
        for name in WP.findall(cell):
            if name not in defined:
                problems.append(f"{wp}: 'After' names {name}, which is not a defined package")
    for name in sorted(set(WP.findall(plan)) - defined):
        if not (name == "WP23" and {"WP23a", "WP23b", "WP23c"} <= defined):
            problems.append(f"the plan mentions {name}, which is not a defined package")
    print(f"work packages: {len(defined)} defined, {len(active)} active, {len(retired)} retired")

    # ---- the releases ---------------------------------------------------------------------------------------------------------
    release_block = section(plan, "**Proposed releases**", "**Routine for every release**")
    releases = []                                # (version, packages cell) in table order
    for line in release_block.splitlines():
        if re.match(r"\| 0\.5\.\d+ \|", line):
            row = cells(line)
            releases.append((row[0], row[1]))
    order = [version for version, _ in releases]
    if order != sorted(order, key=lambda v: [int(x) for x in v.split(".")]):
        problems.append("the release table is not in ascending order")
    release_of = {}
    for version, cell in releases:
        for wp in WP.findall(cell):
            if wp not in defined:
                problems.append(f"release {version} names {wp}, which is not a defined package")
            release_of.setdefault(wp, []).append(version)
    for wp in active:
        found = release_of.get(wp, [])
        if len(found) != 1:
            problems.append(f"{wp} appears in {len(found)} release rows: {found}")
    index = {version: i for i, version in enumerate(order)}
    for wp in active:
        if len(release_of.get(wp, [])) != 1:
            continue
        mine = index[release_of[wp][0]]
        strict = bool(re.search(r"soak|plus", after[wp]))
        for dep in deps[wp]:
            if len(release_of.get(dep, [])) != 1:
                continue
            theirs = index[release_of[dep][0]]
            if theirs > mine or (strict and theirs == mine):
                problems.append(f"{wp} ships in {release_of[wp][0]} but waits for {dep}, which ships in {release_of[dep][0]}"
                                + (" (a soak needs an earlier release)" if strict and theirs == mine else ""))

    # ---- the lanes ------------------------------------------------------------------------------------------------------------
    lane_block = section(plan, "**Three lanes**", "**Proposed releases**")
    lanes = {}
    for line in lane_block.splitlines():
        match = re.match(r"(\d)\. \*\*[^*]+\*\* \(.*?\): (.*)$", line)
        if match:
            chain = match.group(2).split(". ")[0]
            lanes[match.group(1)] = WP.findall(chain)
    where = {}
    for lane, chain in lanes.items():
        for wp in chain:
            where.setdefault(wp, []).append(lane)
    for wp in active:
        found = where.get(wp, [])
        if len(found) != 1:
            problems.append(f"{wp} is in {len(found)} lanes: {found}")
    for lane, chain in lanes.items():
        for position, wp in enumerate(chain):
            for dep in deps.get(wp, []):
                if dep in chain and chain.index(dep) > position:
                    problems.append(f"lane {lane} puts {wp} before {dep}, which it waits for")
    for lane, chain in lanes.items():
        shipped = [(wp, index[release_of[wp][0]]) for wp in chain if len(release_of.get(wp, [])) == 1]
        for (earlier, a), (later, b) in zip(shipped, shipped[1:]):
            if b < a:
                problems.append(f"lane {lane} lists {later} after {earlier}, but {later} ships in {order[b]} and {earlier} in {order[a]}")
    print(f"releases: {len(releases)}; lanes: {len(lanes)}")

    # ---- decisions and contracts ------------------------------------------------------------------------------------------------
    decisions = set(re.findall(r"^\| (D\d+) \|", plan, flags=re.M))
    for name in sorted(set(re.findall(r"\bD\d+\b", plan)) - decisions):
        problems.append(f"the plan mentions {name}, which is not in the decisions table")
    contracts = set(re.findall(r"^\*\*((?:LC|TC|RC)\d)\. ", plan, flags=re.M)) | set(re.findall(r"^### ((?:LC|TC|RC))\.", plan, flags=re.M))
    for name, text in texts.items():
        for item in sorted(set(re.findall(r"\b((?:LC|TC|RC)[1-9])\b", text)) - contracts):
            problems.append(f"{name} refers to {item}, which section 11 of the plan does not define")
    hints = set(re.findall(r"^\| (H\d+) \|", plan, flags=re.M))
    for item in sorted(set(re.findall(r"\bH\d+\b", plan)) - hints):
        problems.append(f"the plan mentions {item}, which is not a hint in section 12")
    status = {}
    for line in plan.splitlines():
        match = re.match(r"\| (D\d+) \|", line)
        if match:
            answer = cells(line)[-1]
            kind = next((word for word in ("Decided", "Resolved", "Open") if answer.startswith(f"**{word}")), None)
            if kind is None:
                problems.append(f"{match.group(1)}: the status cell must start with **Decided, **Resolved or **Open")
            status[match.group(1)] = kind
    waiting = sorted((d for d, k in status.items() if k == "Open"), key=lambda d: int(d[1:]))
    print(f"decisions: {len(decisions)} ({sum(k == 'Decided' for k in status.values())} decided, {sum(k == 'Resolved' for k in status.values())} resolved, "
          f"{len(waiting)} open: {', '.join(waiting)}); contract items: {len(contracts)}; research hints: {len(hints)}")

    # ---- issues, blockers and gates ---------------------------------------------------------------------------------------------
    covers_line = plan.split("**Covers:**")[1].split("\n")[0]
    covered = set()
    for a, b in re.findall(r"#(\d+)–#(\d+)", covers_line):
        covered.update(range(int(a), int(b) + 1))
    covered.update(int(n) for n in re.findall(r"#(\d+)(?!–)", covers_line))
    covered -= {4, 7}                                    # not open issues
    indexed = {int(n) for n in re.findall(r"#(\d+)", plan.split("## 10. Issue index")[1].split("## 11.")[0])}
    for n in sorted(covered - indexed):
        problems.append(f"issue #{n} is in 'Covers' but not in the issue index")
    for n in sorted(indexed - covered):
        problems.append(f"issue #{n} is in the issue index but not in 'Covers'")
    blockers = {int(n) for n in re.findall(r"#(\d+)", plan.split("carry the `go-live blocker` label:")[1].split(".")[0])}
    gate_text = "\n".join(line for line in plan.splitlines() if re.match(r"\| G[123] ", line))
    in_gates = {int(n) for n in re.findall(r"#(\d+)", gate_text)}
    for n in sorted(blockers - in_gates):
        problems.append(f"go-live blocker #{n} belongs to no gate in RC2")
    top = max(covered)
    for name, text in texts.items():
        for match in re.finditer(r"(?<![\w/])#(\d+)\b", text):
            if int(match.group(1)) > top:
                problems.append(f"{name}: refers to issue #{match.group(1)}, which does not exist")

    # ---- versions and counts ----------------------------------------------------------------------------------------------------
    for name, text in texts.items():
        for lib, version in PINS.items():
            for match in re.finditer(rf"{lib}[`)*\s=]*(?:==|\(|tested|version|, )?\s*([0-9]+\.[0-9]+\.[0-9]+)", text):
                if match.group(1) != version:
                    problems.append(f"{name}: {lib} quoted as {match.group(1)}, expected {version}")
    for name in ("requirements.lock", "pyproject.toml"):
        path = ROOT / name
        if path.exists():
            for lib, version in PINS.items():
                for match in re.finditer(rf"(?im)^\s*{lib}\s*==\s*([0-9.]+)", path.read_text(encoding="utf-8")):
                    if match.group(1) != version:
                        problems.append(f"{name}: {lib}=={match.group(1)}, the documents say {version}")
    spec_version = re.search(r"\*\*Version:\*\* ([0-9.]+)", texts["SOFTWARE_SPECIFICATION.md"]).group(1)
    for match in re.finditer(r"SOFTWARE_SPECIFICATION\.md\*{0,2}`?,? (?:version |\()([0-9.]+)", texts["DEVELOPMENT_PLAN.md"] + texts["POST_SHADOW_PLAN.md"]):
        if match.group(1) != spec_version:
            problems.append(f"a document quotes specification version {match.group(1)}; the specification itself is {spec_version}")
    readme_count = re.search(r"The suite has ([\d,]+) tests", texts["README.md"])
    plan_count = re.search(r"\*\*Test baseline: ([\d,]+) passed", plan)
    if readme_count and plan_count and readme_count.group(1) != plan_count.group(1):
        problems.append(f"README.md says {readme_count.group(1)} tests but the plan's baseline is {plan_count.group(1)}")
    print(f"specification {spec_version}; plan {re.search(r'Version:.. ([0-9.]+)', plan).group(1)}")

    # ---- retired phrases and links ----------------------------------------------------------------------------------------------
    for name, text in texts.items():
        for pattern, why in STALE.items():
            for match in re.finditer(pattern, text):
                problems.append(f"{name}:{text.count(chr(10), 0, match.start()) + 1}: stale phrase {pattern!r} ({why})")
    listed_missing = set(re.findall(r"`([\w.-]+\.md)`", section(plan, "5. **Documents cited but not in this repository**", "6. **Differences")))
    for name, text in texts.items():
        for target in re.findall(r"\]\(([^)#\s]+\.md)(?:#[^)]*)?\)", text):
            if "://" in target:
                continue
            path = (ROOT / name).parent / target
            if not path.exists() and Path(target).name not in listed_missing:
                problems.append(f"{name} links to {target}, which does not exist and is not listed as missing in section 8 of the plan")

    print()
    if problems:
        print(f"{len(problems)} PROBLEMS")
        for problem in problems:
            print(" -", problem)
        return 1
    print("no problems found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
