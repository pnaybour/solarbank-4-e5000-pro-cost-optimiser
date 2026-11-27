"""The release archive carries every document that the deployed documents link to.

The deployed README, the operating guide and the deployment guide link to the plan and the testing report. A document missing from the
archive leaves a dead link on the Pi (found by the peer review of 2 November 2026). The list of archived names is read from the deploy
script, so adding a link without adding the file fails here, on the Mac, before anything is deployed.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent
LINK = re.compile(r"\]\(([^)#\s]+\.md)(?:#[^)]*)?\)")


def archived_names():
    script = (ROOT / "scripts" / "deploy-pi-shadow.sh").read_text(encoding="utf-8")
    listed = re.search(r"for name in \((.*?)\):", script, flags=re.S)
    assert listed, "the archive list was not found in deploy-pi-shadow.sh"
    return re.findall(r"'([^']+)'", listed.group(1))


def is_archived(path, names):
    relative = path.resolve().relative_to(ROOT).as_posix()
    return any(relative == name or relative.startswith(name.rstrip("/") + "/") for name in names)


def test_every_document_the_archived_documents_link_to_is_in_the_archive():
    names = archived_names()
    documents = [ROOT / name for name in names if name.endswith(".md")] + [ROOT / "deployment" / "README.md"]
    missing = []
    for document in documents:
        for target in LINK.findall(document.read_text(encoding="utf-8")):
            if "://" in target:
                continue
            path = (document.parent / target).resolve()
            if path.exists() and not is_archived(path, names):
                missing.append(f"{document.relative_to(ROOT).as_posix()} links to {path.relative_to(ROOT).as_posix()}")
    assert missing == [], "not in the release archive: " + "; ".join(sorted(set(missing)))


def test_the_plan_and_the_testing_report_are_in_the_archive():
    names = archived_names()
    assert "POST_SHADOW_PLAN.md" in names and "TESTING_REPORT_2026-11-01.md" in names
    for name in names:
        if name.endswith(".md"):
            assert (ROOT / name).exists(), name              # an archive entry that does not exist would stop the release script
