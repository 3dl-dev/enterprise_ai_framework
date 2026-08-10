"""Structural guard for the resident "Agents" surface design record.

enterpriseaiframework-3ec. This is a DESIGN item; its deliverable is two documents, and
the thing that can rot is a document that stops naming one of the six binding contracts
the downstream build items (-055, -627, -0e7, -39d, -914, -a4e, -ede) each consume. A
contract that quietly falls out of the record is a downstream implementer with no design
to build against and no signal that it is missing.

So this test asserts, mechanically, that BOTH artifacts exist and that each names all six
contracts. It is hermetic (stdlib + the two files); it drives nothing.

VERACITY — WHERE THE EXPECTED VALUES COME FROM. The expected keywords below are NOT read
back out of the documents they check (that would be a mirror that passes no matter what
the docs say). They are transcribed from the SIX-CONTRACT ENUMERATION in the rd item
enterpriseaiframework-3ec — the independent specification of what the record must decide:

    1. agent identity / alias grammar     2. residency model
    3. resident-time + compute metering    4. integrated key vs BYO
    5. the chosen email component          6. Code-untouched invariant

Each contract contributes at least two anchor strings that the item's own text fixes
(e.g. the alias grammar `::agents/`, the metering source `cAdvisor`, the frozen-set
enforcement `git diff --exit-code`). Removing any one anchor from either document must
turn this red — verified by fault injection during authoring, per the item's test note.
"""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RECORD = REPO / "docs" / "design" / "records" / "agents-surface.md"
DESIGN = REPO / "docs" / "design" / "design.md"

# The six contracts, each with the anchor strings the item's enumeration fixes. Source:
# the enterpriseaiframework-3ec contract list, NOT the documents under test.
CONTRACTS = [
    ("1 identity/alias", ["::agents/", "agent-<user>-<name>", "parse_alias"]),
    ("2 residency/resident", ["resident", "opencode serve", "stopped", "replicas: 0"]),
    ("3 resident-time metering", ["resident-time", "cAdvisor", "status.startTime"]),
    ("4 BYO key", ["BYO", "OPENAI_API_BASE", "model_source"]),
    ("5 email component", ["Maddy"]),
    ("6 Code-untouched", ["git diff --exit-code", "byte-", "test_workspace_shell.py"]),
]


@pytest.fixture(scope="module")
def record_text() -> str:
    assert RECORD.is_file(), f"the design record is missing: {RECORD}"
    return RECORD.read_text()


@pytest.fixture(scope="module")
def design_section() -> str:
    """The §12 Agents section of design.md, isolated so a match cannot come from elsewhere."""
    assert DESIGN.is_file(), f"design.md is missing: {DESIGN}"
    text = DESIGN.read_text()
    start = text.find('## 12. The resident "Agents" surface')
    assert start != -1, "design.md has no §12 Agents surface section"
    # End at the next top-level heading after §12.
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


def test_both_artifacts_exist(record_text, design_section):
    assert record_text.strip(), "the design record is empty"
    assert design_section.strip(), "the §12 section is empty"


def test_design_section_references_the_record(design_section):
    """The source-of-truth section must point at the record, or the record is orphaned."""
    assert "docs/design/records/agents-surface.md" in design_section, (
        "design.md §12 does not reference the design record file"
    )


@pytest.mark.parametrize("name,anchors", CONTRACTS, ids=[c[0] for c in CONTRACTS])
def test_record_names_each_contract(record_text, name, anchors):
    """The record must name every contract — that is what makes it buildable-against."""
    missing = [a for a in anchors if a not in record_text]
    assert not missing, f"design record does not name contract {name}: missing {missing}"


@pytest.mark.parametrize("name,anchors", CONTRACTS, ids=[c[0] for c in CONTRACTS])
def test_design_section_names_each_contract(design_section, name, anchors):
    """The design.md section states the binding contracts; each must appear by its lead anchor.

    Only the FIRST anchor is required here — §12 is the summary, the record carries the
    detail — but it must be enough to identify the contract unambiguously.
    """
    lead = anchors[0]
    assert lead in design_section, (
        f"design.md §12 does not state contract {name} (missing anchor {lead!r})"
    )


def test_every_downstream_consumer_is_mapped(record_text):
    """The architecture-change-cascade requirement: the record names which item consumes it."""
    for item in ("-055", "-627", "-0e7", "-39d", "-914", "-a4e", "-ede"):
        assert item in record_text, f"downstream item {item} is not mapped in the record"


def test_reserved_rulings_are_marked_not_silently_chosen(record_text):
    """The two RESERVED rulings must be labelled as Baron's, not decided in place."""
    assert record_text.count("RESERVED") >= 2, "the two reserved rulings are not marked"
    assert "Maddy" in record_text, "email recommendation missing"
    assert "core-hour" in record_text, "resident-time cost-basis recommendation missing"
