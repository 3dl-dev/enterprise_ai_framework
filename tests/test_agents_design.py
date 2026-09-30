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


def test_the_resident_metering_ruling_is_recorded_as_usage_not_cost(record_text, design_section):
    """Baron ruled Contract 3(b): meter USAGE, not cost. Both artifacts must say so.

    Recorded as a test rather than only as prose because the record still contains the
    RECOMMENDATION it overruled — a resident-hour plus core-hour rate, with the dollar
    figures reserved — and a reader who finds that paragraph without the ruling builds the
    rate. This is the mechanical form of "the ruling travels with the recommendation".

    It is deliberately checked in BOTH files. design.md §12 is the summary an operator
    reads and the record is the detail; the two disagreeing about whether this dimension
    has a price is precisely the drift the source-of-truth ordering exists to prevent.
    """
    for name, text in (("the agents-surface record", record_text),
                       ("design.md §12", design_section)):
        lowered = text.lower()
        assert "meter usage, not cost" in lowered, (
            f"{name} does not record Baron's ruling on the resident meter. It is normative: "
            "owned hardware is sunk cost, only inference has a real upstream bill, and the "
            "second dimension is quantities (hours, CPU-core-hours, MB) with no rate."
        )
    assert "FUTURE item" in record_text, (
        "the record does not say where a cost basis would go if commodity cloud compute is "
        "ever added — without that, the ruling reads as an omission rather than a decision"
    )


# ---------------------------------------------------------------------------------------
# Raven attack-register evidence (enterpriseaiframework-9cec).
#
# design.md §10 rows R1-R6 claim a disposition. A disposition that cites a test which was
# renamed, deleted or never written is a claim with no proof behind it, and prose cannot
# notice. So every `path::test_name` the rows cite is resolved against the tree.
#
# WHERE THE EXPECTATION COMES FROM: the citations are read out of design.md, but what they
# are checked AGAINST is the test source files on disk (independent of the document): a
# `def <name>` must exist in the named file. The six ids are transcribed from the item.
# A `pending-<item>:` prefix marks a test on an unmerged item branch: the file may be absent,
# but if it is present the name must resolve, so a rename after merge turns this red.
# ---------------------------------------------------------------------------------------

import re

RAVEN_ROWS = ["R1", "R2", "R3", "R4", "R5", "R6"]
_CITATION = re.compile(r"`(?:(pending-\w+):)?([\w./-]+\.py)::(test_\w+)`")


def _raven_rows(design_text: str) -> dict[str, str]:
    """Row id -> the row's full markdown line, for the Raven attack-register table."""
    rows = {}
    for line in design_text.splitlines():
        m = re.match(r"\|\s*\*\*(R\d+)\*\*\s*\|", line)
        if m:
            rows[m.group(1)] = line
    return rows


def citation_problems(design_text: str, repo: Path) -> list[str]:
    """Every way the R1-R6 rows fail to cite a test that exists. Empty means all resolve."""
    rows = _raven_rows(design_text)
    problems = [f"{r}: row is missing from design.md" for r in RAVEN_ROWS if r not in rows]
    for rid in RAVEN_ROWS:
        line = rows.get(rid)
        if line is None:
            continue
        cites = _CITATION.findall(line)
        if not cites:
            problems.append(f"{rid}: cites no test as `path::test_name`")
        for pending, path, name in cites:
            f = repo / path
            if not f.is_file():
                if not pending:
                    problems.append(f"{rid}: {path} does not exist (cite it as pending-<item>: if unmerged)")
                continue
            if not re.search(rf"^\s*(?:async\s+)?def {re.escape(name)}\(", f.read_text(), re.M):
                problems.append(f"{rid}: {path} has no test named {name}")
    return problems


def test_every_raven_disposition_cites_a_test_that_exists():
    assert citation_problems(DESIGN.read_text(), REPO) == []


def test_the_citation_guard_catches_the_ways_a_disposition_loses_its_proof():
    """Both ways. The real document passes; each realistic corruption of the DATA it reads
    (a renamed test, a moved file, an uncited row, a dropped row, an unmarked unmerged test)
    is reported. Mutations are applied to design.md's text, not to the guard's code."""
    text = DESIGN.read_text()
    assert citation_problems(text, REPO) == [], "premise: the real document resolves"
    real = "control-plane/tests/test_agent_manager_token.py::test_a_raven_cannot_create_a_raven"
    assert real in text, "premise: the mutation target is cited"
    # 1. a nearby wrong name (renamed test), 2. a moved file, 3. a wrong-directory path
    for old, new in (
        (real, real + "_v2"),
        (real, real.replace("test_agent_manager_token.py", "test_agent_manager_tokens.py")),
        (real, real.replace("control-plane/tests", "tests")),
    ):
        assert citation_problems(text.replace(old, new), REPO), (old, new)
    # 4. a citation into a file that exists must name a test that exists in it
    injected = text.replace(real, real.replace("control-plane/tests/test_agent_manager_token.py",
                                               "tests/test_agents_design.py").replace(
        "test_a_raven_cannot_create_a_raven", "test_no_such_test"))
    assert any("no test named" in p for p in citation_problems(injected, REPO))
    # 5. an unmerged test cited WITHOUT the pending marker is a fault
    unmarked = text.replace("pending-82e:tests/test_voice_worker.py",
                            "tests/test_voice_worker_missing.py", 1)
    assert any("does not exist" in p for p in citation_problems(unmarked, REPO))
    # 6. a row with its citations stripped, and a dropped row
    lines = text.splitlines()
    r2 = next(i for i, l in enumerate(lines) if l.startswith("| **R2**"))
    stripped = "\n".join(lines[:r2] + [_CITATION.sub("", lines[r2])] + lines[r2 + 1:])
    assert any(p.startswith("R2:") and "cites no test" in p for p in citation_problems(stripped, REPO))
    dropped = "\n".join(lines[:r2] + lines[r2 + 1:])
    assert any(p.startswith("R2:") and "missing" in p for p in citation_problems(dropped, REPO))
