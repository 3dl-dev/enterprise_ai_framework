"""The agents alias grammar, and the promise that adding it changed nothing else.

Contract 1 of docs/design/records/agents-surface.md exists because of a specific, silent
failure mode. Two splitters read the alias and they do not agree:

  * `gateway.parse_alias` uses `rpartition("::")` — the LAST separator.
  * `metering.py`'s SQL uses `split_part(alias, '::', 1|2)` — the FIRST separators.

With two fields they agree. The obvious three-field spelling `baron::agents::scraper`
makes them disagree in DIFFERENT directions: Python reads the username as `baron::agents`,
SQL reads the surface as `agents` and drops the instance entirely. Nothing errors. The
bill just quietly names the wrong thing.

So the grammar folds the instance into the surface field — `baron::agents/scraper`, one
`::` — and the property under test is that BOTH splitters recover the same two strings
from it. `split_part` is reproduced here from its definition (split on the separator, take
the nth field, 1-indexed), which is the same operation Postgres performs; that it holds
against the REAL ledger in the real database is proven separately and on a live cluster by
tests-live/test_agent_model_api.py, which reads the row back through
`metering.ledger_attribution_sql`'s own SQL.

The second half of this file is the additivity claim. `chat`, `ide` and `terminal` are
what the camp runs on; the agents surface is worth nothing if it perturbs them, and
"we only added things" is a promise until something checks it.
"""

import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# `app.db` binds asyncpg at import and `app.issuance` imports it. The test venv
# deliberately carries no database driver (bundle/bin/run-tests.sh: the venv exists to
# prove behaviour, not to host a database) and nothing below opens a connection, so the
# driver is a shell. `app.gateway` — the module actually under test — is imported for real.
if "asyncpg" not in sys.modules:
    _pg = types.ModuleType("asyncpg")
    _pg.Pool = object

    async def _create_pool(*a, **kw):  # pragma: no cover - never reached
        raise RuntimeError("no database in this suite")

    _pg.create_pool = _create_pool
    sys.modules["asyncpg"] = _pg

from app import db, gateway, issuance  # noqa: E402


# The three surfaces as they were before this item, spelled out literally rather than read
# from `gateway.SURFACES`. Reading the constant would make this test agree with whatever
# the code says, including a change that dropped one.
BASE_SURFACES = ("chat", "ide", "terminal")


def split_part(text: str, sep: str, field: int) -> str:
    """Postgres `split_part`, by its definition. The FIRST-separator splitter."""
    parts = text.split(sep)
    return parts[field - 1] if 0 < field <= len(parts) else ""


# ---------------------------------------------------------------- the grammar

def test_the_agent_alias_is_exactly_the_contract_one_spelling():
    assert gateway.agent_key_alias("baron", "scraper") == "baron::agents/scraper"


def test_the_alias_keeps_exactly_one_separator():
    """The whole mechanism, in one assertion.

    Every property below follows from this: one `::` is what makes the last-separator and
    first-separator splitters the same operation.
    """
    alias = gateway.agent_key_alias("baron", "scraper")
    assert alias.count("::") == 1, alias


def test_both_splitters_recover_the_same_user_and_surface():
    """Python's rpartition and SQL's split_part, over the same alias, side by side."""
    alias = gateway.agent_key_alias("baron", "scraper")

    assert gateway.parse_alias(alias) == ("baron", "agents/scraper")
    assert split_part(alias, "::", 1) == "baron"
    assert split_part(alias, "::", 2) == "agents/scraper"


def test_the_rejected_grammar_would_have_made_them_disagree():
    """The losing spelling, kept as a live demonstration rather than a comment.

    If somebody later "simplifies" the grammar to `<user>::agents::<name>`, the assertions
    above still pass for whatever the code produces — so the reason for the choice has to
    be checkable too, or it is just folklore.
    """
    trap = "baron::agents::scraper"
    assert trap.rpartition("::")[0] == "baron::agents"   # Python: username is wrong
    assert split_part(trap, "::", 2) == "agents"          # SQL: the instance is lost
    # Different answers from the same string, which is the failure the grammar avoids.
    assert trap.rpartition("::")[2] != split_part(trap, "::", 2)


def test_the_portals_own_partition_reads_an_agent_alias_correctly():
    """`portal.my_keys` splits with `partition("::")`, a THIRD splitter over the same string.

    It renders the caller's own keys. An agent key it read as surface `agents` would
    collapse every one of a user's agents into one row on their own page.
    """
    owner, _, surface = gateway.agent_key_alias("baron", "scraper").partition("::")
    assert (owner, surface) == ("baron", "agents/scraper")


@pytest.mark.parametrize("name", ["scraper", "a", "a-b-c", "agent1", "0", "x" * 39])
def test_names_the_workspace_slug_allows_round_trip(name):
    user, surface = gateway.parse_alias(gateway.agent_key_alias("baron", name))
    assert (user, surface) == ("baron", f"agents/{name}")
    assert gateway.agent_instance(surface) == name


@pytest.mark.parametrize(
    "name",
    [
        "",            # no instance at all
        "-lead",       # slug must start alphanumeric
        "Scraper",     # uppercase: the workspace rejects it, so must this
        "a/b",         # a "/" would add a second field to the surface
        "a::b",        # a "::" would add a third field to the alias
        "x" * 40,      # over the slug length
        "my agent",
    ],
)
def test_names_the_slug_forbids_are_refused_rather_than_rewritten(name):
    """Constrained, not sanitised — the reason deploy/workspace/shell-server.py gives.

    A rewritten name is a name the user did not choose pointing at objects they cannot
    find. A `/` or `::` slipping through is worse: it silently re-fields the alias.
    """
    with pytest.raises(ValueError):
        gateway.agent_key_alias("baron", name)


@pytest.mark.parametrize(
    "alias",
    [
        "baron::agents",           # the family is not a spendable surface
        "baron::agents/",          # prefix with no instance
        "baron::agents/Bad",       # instance that is not a slug
        "baron::agents/a/b",       # two-level instance
        "baron::agents::scraper",  # the rejected grammar
        "baron::nope",
        "agents/scraper",          # no principal at all
        "",
    ],
)
def test_parse_alias_refuses_everything_that_is_not_a_real_surface(alias):
    """Fail closed. An alias parsed into a surface nobody mints is an unattributable key."""
    assert gateway.parse_alias(alias) is None


# ---------------------------------------------------------------- additivity

@pytest.mark.parametrize("surface", BASE_SURFACES)
def test_the_existing_surfaces_alias_exactly_as_before(surface):
    """chat/ide/terminal, byte-for-byte. This is the camp's surface set."""
    assert gateway.key_alias("baron", surface) == f"baron::{surface}"
    assert gateway.parse_alias(f"baron::{surface}") == ("baron", surface)
    # And the SQL side is untouched too — the query has not changed, so this must not.
    assert split_part(f"baron::{surface}", "::", 1) == "baron"
    assert split_part(f"baron::{surface}", "::", 2) == surface


@pytest.mark.parametrize("surface", BASE_SURFACES)
def test_the_new_dispatcher_returns_the_old_function_for_old_surfaces(surface):
    """`surface_alias` is what `generate_key` now calls. For a base surface it must BE
    `key_alias` — not a second spelling that happens to agree today."""
    assert gateway.surface_alias("baron", surface) == gateway.key_alias("baron", surface)


def test_key_alias_still_refuses_an_agent_surface():
    """The old entry point did not gain the agents family, deliberately.

    Agents mint through `agent_key_alias`/`surface_alias`. If `key_alias` quietly started
    accepting `agents/x`, its `surface in SURFACES` guard — which is what stops an
    arbitrary string becoming a surface — would have been widened by accident.
    """
    with pytest.raises(ValueError):
        gateway.key_alias("baron", "agents/scraper")


def test_the_surface_set_itself_is_unchanged():
    assert gateway.SURFACES == BASE_SURFACES


@pytest.mark.parametrize("surface", [*BASE_SURFACES, "agents/scraper"])
def test_is_known_surface_accepts_the_old_set_and_the_new_one(surface):
    assert gateway.is_known_surface(surface)


@pytest.mark.parametrize("surface", ["agents", "agents/", "agents/Bad", "nope", ""])
def test_is_known_surface_rejects_everything_else(surface):
    assert not gateway.is_known_surface(surface)


# ---------------------------------------------------------------- issuance and storage

@pytest.mark.parametrize("surface", ["agents", "agents/", "agents/Bad", "nope"])
def test_issuance_refuses_a_surface_that_is_not_one(surface):
    """The guard runs before any database or gateway call, so this needs neither."""
    import asyncio

    with pytest.raises(Exception) as exc:
        asyncio.run(issuance.issue("baron", surface, actor="admin"))
    assert getattr(exc.value, "status_code", None) == 400, exc.value


def test_the_database_check_constraint_admits_exactly_the_slug_gateway_admits():
    """The two halves of "what is a valid agent surface" must not drift.

    Postgres enforces the column's CHECK; `gateway.AGENT_SLUG` enforces what we mint. If
    they disagree, the failure is an IntegrityError at the moment an operator provisions
    an agent — read as "the agents surface is broken" rather than as a naming rule.
    """
    patterns = re.findall(r"surface ~ '\^agents/([^']+)\$'", db.SCHEMA)
    assert patterns, "no agents CHECK pattern found in db.SCHEMA"
    assert len(set(patterns)) == 1, f"the schema carries two different patterns: {patterns}"
    # AGENT_SLUG is anchored with ^...$; the SQL pattern is the same body under ^agents/…$.
    assert f"^{patterns[0]}$" == gateway.AGENT_SLUG.pattern


def test_the_base_surfaces_are_still_in_the_check_constraint():
    for surface in BASE_SURFACES:
        assert f"'{surface}'" in db.SCHEMA
