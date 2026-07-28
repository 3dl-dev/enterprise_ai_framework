"""Who the bill names, and who gets to choose it.

`end_user` is whatever the caller wrote in the request body's "user" field. Trusting it
everywhere let anyone holding any key put somebody else's name on their spend. On this
cluster a legitimate `baron::ide` key sent {"user":"veracity-probe-xyz"} and the row
appeared under `veracity-probe-xyz`; three further requests from the same key were
attributed to `student`, a real person who had not made them.

The rule now: only a key minted AS a shared surface may name someone else. Everything
else is attributed from the alias, which its holder cannot choose.

WHY THIS TEST IS SHAPED THE WAY IT IS

It runs the attribution expression lifted out of `control-plane/app/metering.py` itself
rather than a copy pasted in here, so editing the rule without editing this file breaks
the test instead of quietly passing. Rows are inserted and the whole thing ROLLED BACK,
because the ledger under test is the real bill and a test must not leave money in it.
"""

import re
import subprocess
from pathlib import Path

import pytest

NS = "enterprise-ai"
METERING = Path(__file__).resolve().parent.parent / "control-plane" / "app" / "metering.py"

# A key that is a shared surface, and one that is not. The shared list is a deployment
# setting; this is the value the deployment actually ships (provision-chat-key.sh).
SHARED = "chat-surface::chat"
PERSONAL = "zz-attribution-test::ide"


def _kubectl(*args: str) -> str:
    return subprocess.run(["kubectl", "-n", NS, *args],
                          capture_output=True, text=True, timeout=90, check=True).stdout


def _secret(key: str) -> str:
    import base64
    raw = _kubectl("get", "secret", "enterprise-ai-secrets", "-o", f"jsonpath={{.data.{key}}}")
    return base64.b64decode(raw).decode()


@pytest.fixture(scope="module")
def dsn() -> dict:
    import urllib.parse
    p = urllib.parse.urlparse(_secret("GATEWAY_DATABASE_URL"))
    return {"user": p.username, "password": p.password, "db": p.path.lstrip("/")}


@pytest.fixture(scope="module")
def username_expr() -> str:
    """The real attribution expression, lifted from the module under test."""
    src = METERING.read_text()
    alias = re.search(r'_ALIAS = """(.*?)"""', src, re.S)
    trusted = re.search(r'_TRUSTED_END_USER = f"""(.*?)"""', src, re.S)
    assert alias and trusted, (
        "metering.py no longer defines _ALIAS and _TRUSTED_END_USER as this test expects. "
        "If the attribution rule moved, move this test with it — do not delete the check."
    )
    expr = trusted.group(1).replace("{_ALIAS}", alias.group(1))
    # The module binds the shared-alias list as a query parameter; bind it literally here.
    expr = expr.replace("$SHARED", f"ARRAY['{SHARED}']")
    return (
        f"COALESCE({expr}, NULLIF(split_part({alias.group(1)}, '::', 1), ''), '(unattributed)')"
    )


def _attribute(dsn: dict, username_expr: str, rows: list[tuple[str, str]]) -> list[str]:
    """Insert (alias, end_user) rows, read back who each is attributed to, roll back."""
    values = ",\n".join(
        f"""('req-{i}', 'acompletion', 'tok-{i}', 0.001, 1, 1, 0, now(), now(), 'm',
             '{{"user_api_key_alias": "{alias}"}}'::jsonb, {"NULL" if eu is None else f"'{eu}'"})"""
        for i, (alias, eu) in enumerate(rows)
    )
    sql = f"""
    BEGIN;
    INSERT INTO "LiteLLM_SpendLogs"
      (request_id, call_type, api_key, spend, total_tokens, prompt_tokens,
       completion_tokens, "startTime", "endTime", model, metadata, end_user)
    VALUES {values};
    -- Tagged, because psql also prints BEGIN / INSERT / ROLLBACK command tags on stdout
    -- and picking "the last line" silently reads 'ROLLBACK' as the answer.
    SELECT 'RESULT:' || string_agg(x, '~' ORDER BY request_id) FROM (
      SELECT s.request_id, {username_expr} AS x
      FROM "LiteLLM_SpendLogs" s
      LEFT JOIN "LiteLLM_VerificationToken" v ON v.token = s.api_key
      WHERE s.request_id LIKE 'req-%'
    ) t;
    ROLLBACK;
    """
    out = subprocess.run(
        ["kubectl", "-n", NS, "exec", "-i", "postgres-0", "--",
         "env", f"PGPASSWORD={dsn['password']}", "psql", "-U", dsn["user"],
         "-d", dsn["db"], "-t", "-A", "-v", "ON_ERROR_STOP=1", "-f", "-"],
        input=sql, capture_output=True, text=True, timeout=90,
    )
    assert out.returncode == 0, f"psql failed: {out.stderr}"
    tagged = [l for l in out.stdout.splitlines() if l.startswith("RESULT:")]
    assert tagged, f"no result row came back; psql said:\n{out.stdout}\n{out.stderr}"
    return tagged[0][len("RESULT:"):].split("~")


def test_a_personal_key_cannot_bill_someone_else(dsn, username_expr):
    """The original exploit: a real key, an invented name in the body."""
    got = _attribute(dsn, username_expr, [(PERSONAL, "veracity-probe-xyz")])
    assert got == ["zz-attribution-test"], (
        f"a personal key named {got} instead of its own holder — attribution is forgeable again"
    )


def test_a_personal_key_cannot_bill_a_real_other_user(dsn, username_expr):
    """The worse case, which also happened: the forged name belongs to somebody real."""
    got = _attribute(dsn, username_expr, [(PERSONAL, "student")])
    assert got == ["zz-attribution-test"]


def test_a_shared_surface_key_still_attributes_per_person(dsn, username_expr):
    """The reason end_user was trusted in the first place. Do not regress this to fix the above."""
    got = _attribute(dsn, username_expr, [(SHARED, "person-a")])
    assert got == ["person-a"], (
        "chat attribution broke: one shared key serves everyone there, so without end_user "
        "every chat user collapses into a single row"
    )


def test_a_personal_key_with_no_end_user_is_unaffected(dsn, username_expr):
    got = _attribute(dsn, username_expr, [(PERSONAL, None)])
    assert got == ["zz-attribution-test"]


def test_mixed_traffic_lands_in_the_right_columns(dsn, username_expr):
    got = _attribute(dsn, username_expr, [
        (PERSONAL, "veracity-probe-xyz"),
        (SHARED, "person-a"),
        (PERSONAL, None),
    ])
    assert got == ["zz-attribution-test", "person-a", "zz-attribution-test"]


def test_the_ledger_was_not_modified(dsn, username_expr):
    """The rollback actually rolled back. A test that dirties the bill is worse than no test."""
    out = subprocess.run(
        ["kubectl", "-n", NS, "exec", "-i", "postgres-0", "--",
         "env", f"PGPASSWORD={dsn['password']}", "psql", "-U", dsn["user"], "-d", dsn["db"],
         "-t", "-A", "-c", "SELECT count(*) FROM \"LiteLLM_SpendLogs\" WHERE request_id LIKE 'req-%'"],
        capture_output=True, text=True, timeout=60,
    )
    assert out.stdout.strip() == "0", "synthetic spend rows survived into the real ledger"
