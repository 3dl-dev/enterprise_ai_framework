r"""enterpriseaiframework-dc0: deploy.sh must refuse to push a fakes-only catalogue over
a cluster that is currently Forge-backed.

bundle/litellm/config.generated.yaml is gitignored local state rendered by
bundle/bin/render-gateway-config.py. Whether it holds three hardcoded fakes or 148 real,
priced Forge models depends only on whether FORGE_API_KEY was visible in the ambient
environment when it was last rendered — nothing in `make up` or `deploy.sh` signals which
one just happened. deploy.sh ships that file straight into the cluster's `gateway-config`
ConfigMap, so whichever render happened last on whoever's machine wins for the live
cluster too. A deploy from a shell without Forge creds loaded silently reduces a
Forge-backed dogfood cluster to three fake models, with no error at deploy time.

deploy/bin/lib/catalogue-guard.sh's `assert_catalogue_not_downgraded` is the fix: read the
cluster's CURRENT ConfigMap (read-only `kubectl get`, never a mutation) and refuse the
deploy when the cluster is presently Forge-backed and the local file about to be pushed is
not.

No real cluster is touched by any test in this file. `kubectl` and `docker` are replaced
on PATH with stub scripts that log every invocation to a file and (unless configured to
simulate a specific cluster response) simply succeed — deploy.sh cannot tell the
difference between the stub and a real, reachable k3s cluster, and none of these tests can
mutate anything real even if the guard had a bug, because there is nothing real behind the
stub to mutate.

WAVE 7 CHALLENGES, AND WHAT CHANGED IN RESPONSE (the design — deploy.sh refuses a
fakes-only push over a Forge-backed cluster — was ruled correct and is unchanged; only the
proof was found insufficient):

1. CIRCULAR FIXTURE. The previous version of this file hand-wrote FORGE_YAML / FAKES_ONLY
   YAML strings to match the guard author's own belief about what render-gateway-config.py
   emits ("lowercase `forge` appears in a Forge render, never in a fakes-only one").
   Nothing tied that belief to the renderer's actual output, so the guard and the test
   could be simultaneously, consistently wrong. Fixed by `_render_real()` below: it runs
   the REAL bundle/bin/render-gateway-config.py, unmodified, both ways (no FORGE_API_KEY
   at all -- the exact `env -u FORGE_API_KEY -u FORGE_ADMIN_KEY` case; and FORGE_API_KEY
   set with --offline against a small, schema-correct synthetic catalog/price pair so the
   real code path -- yaml_entry(), the `{base_url}/v1` api_base line, `_without_fakes` --
   actually runs and decides for itself whether the text contains "forge"). See
   `test_the_guards_textual_signal_holds_against_a_genuinely_rendered_artifact`, which
   asserts exactly that claim against the real renderer's own output, once, so every other
   test in this file can safely reuse the two resulting fixtures.

2. THE OLD kubectl STUB COULD NOT DETECT A WRONG JSONPATH. Confirmed against the live
   cluster this session: a wrong or unescaped jsonpath key (e.g. `config.yaml` instead of
   the correctly-escaped `config\.yaml`) returns rc=0 with EMPTY stdout from kubectl --
   indistinguishable from "the cluster has no Forge catalogue", the exact condition the
   guard exists to detect. The old stub ignored the `-o jsonpath=...` argument entirely and
   always answered from the fixture file regardless of what path was asked for, so a
   jsonpath typo in catalogue-guard.sh would have silently disabled the guard while every
   test in this file stayed green. Fixed: the stub is now a real (if minimal) jsonpath
   evaluator (see `_KUBECTL_STUB`) that walks a `{"data": {"config.yaml": ...}}` structure
   respecting `\.`-escaped literal dots the same way client-go's jsonpath does, and returns
   empty -- not an error -- when the path does not resolve, matching the confirmed live
   behaviour.

   Proof this actually closes the gap (performed once, this session, NOT committed as a
   test because it exercises a deliberately-broken copy of production code rather than
   production code itself -- see the task's summary/test_decisions for the transcript):
   sourcing a copy of catalogue-guard.sh with the escape stripped from its jsonpath
   (`{.data.config.yaml}` instead of `{.data.config\.yaml}`), against this smarter stub,
   with a genuinely Forge-backed cluster fixture and a genuinely fakes-only local file --
   returned 0 (wrongly ALLOWED the downgrade) where the real, unmutated guard returns 1
   (refuses). The OLD stub returned 1 (refuses) for BOTH the real and the mutated guard --
   it could not tell them apart, which is exactly the defect. Under the new stub,
   `test_refuses_when_cluster_is_forge_backed_and_local_is_fakes_only` below would itself
   fail against that mutation (it asserts `returncode == 1`), so the existing exhaustive
   test suite is now the regression guard for the jsonpath string, with no new test
   required for that specific string. `test_kubectl_stub_reproduces_the_confirmed_live_
   cluster_behaviour_on_a_wrong_jsonpath` pins the stub's own fidelity directly, so a
   future rewrite of the stub cannot silently regress back to ignoring the path.

3. THE FORGE-LOCAL PATH NEVER REACHED THE GUARD END TO END. Every deploy.sh-level
   (Layer 2) test used a local catalogue WITHOUT forge, so deploy.sh's PRE-EXISTING sibling
   check ("local has forge but FORGE_API_KEY is empty", above the new guard in deploy.sh)
   never had anything to interact with, and neither did the new guard's "local IS
   Forge-backed" branch, at the deploy.sh level. Fixed by `make_deploy_fixture(forge_api_
   key=...)` and two new tests: `test_deploy_sh_reaches_the_guard_when_upgrading_a_fakes_
   only_cluster_to_forge` and `test_deploy_sh_reaches_the_guard_when_cluster_and_local_are_
   both_forge_backed`, both of which set a non-empty FORGE_API_KEY in the fixture's
   bundle/.env (which is what deploy.sh actually reads -- `set -a; . ./bundle/.env`
   overwrites any inherited value) so the sibling check does not fire, and the real,
   unmutated deploy.sh runs the new guard's "local has forge" branch all the way to a
   completed, unmodified deploy.

Two layers, per the class of drift this bug belongs to (a presence check is not a
correctness check — README/CLAUDE.md "signature defect" note):

  - `test_assert_catalogue_not_downgraded_*` / `test_refuses_*` / `test_allows_*` /
    `test_override_*` / `test_fails_closed_*`: call the real bash function directly
    (sourced from the real file, not reimplemented) for every branch: refuse, allow
    same-catalogue, allow an upgrade, allow a first deploy (no ConfigMap yet), the
    explicit override, and fail CLOSED (not open) on an unrelated kubectl error. This is
    the exhaustive one — one assertion per branch of the actual refusal logic.
  - `test_deploy_sh_*`: run the real, unmodified `deploy/bin/deploy.sh` end-to-end against
    a throwaway fixture tree, to prove the guard is actually wired in — that a function
    existing but never called would not be caught by the first layer alone.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GUARD_LIB = REPO / "deploy" / "bin" / "lib" / "catalogue-guard.sh"
DEPLOY_SH = REPO / "deploy" / "bin" / "deploy.sh"
RENDER_SCRIPT = REPO / "bundle" / "bin" / "render-gateway-config.py"
CONFIG_BASE = REPO / "bundle" / "litellm" / "config.base.yaml"


# --------------------------------------------------------------------------------------
# Real fixtures — rendered by the actual render-gateway-config.py, not hand-written
# --------------------------------------------------------------------------------------


def _render_real(root: Path, *, forge_backed: bool) -> str:
    """Run the REAL, unmodified render-gateway-config.py and return what it actually
    wrote, instead of a string we believe matches its output. `root` becomes a throwaway
    `bundle/` — the script derives its own paths from `__file__`'s location, so copying it
    (and config.base.yaml) into `root/bundle/bin` and `root/bundle/litellm` is enough for
    it to run exactly as it would in the real repo, writing to `root/bundle/litellm/
    config.generated.yaml`, without touching anything in the real checkout.
    """
    (root / "bundle" / "bin").mkdir(parents=True)
    (root / "bundle" / "litellm").mkdir(parents=True)
    shutil.copy2(RENDER_SCRIPT, root / "bundle" / "bin" / "render-gateway-config.py")
    shutil.copy2(CONFIG_BASE, root / "bundle" / "litellm" / "config.base.yaml")

    env = dict(os.environ)
    for k in ("FORGE_API_KEY", "FORGE_ADMIN_KEY", "FORGE_BASE_URL", "FORGE_ACCOUNT_ID"):
        env.pop(k, None)

    args = ["python3", str(root / "bundle" / "bin" / "render-gateway-config.py")]
    if forge_backed:
        # A small, schema-correct synthetic catalog/price pair -- enough for the real
        # renderer's own code (yaml_entry(), the `_without_fakes` marker substitution, the
        # `{base_url}/v1` api_base line) to run for real and decide, on its own, whether
        # the output contains lowercase `forge`. We supply INPUT data, never the output
        # YAML itself -- that is the whole point of the fix. Synthetic (rather than a real
        # Forge account's cached catalog) so this test needs no credentials and no network
        # and is reproducible on any machine, including CI.
        catalog = {
            "data": [{
                "id": "test-catalogue-model", "object": "model", "path": "converse",
                "sovereignty": "us-only", "max_context_window": 100000,
            }]
        }
        pricing = {
            "data": [{
                "model_id": "test-catalogue-model", "source": "default",
                "input_per_mtok": 1.0, "output_per_mtok": 2.0,
                "cache_read_per_mtok": 0, "cache_write_per_mtok": 0, "priced": True,
            }]
        }
        (root / "bundle" / "litellm" / "forge-catalog.cache.json").write_text(
            json.dumps(catalog))
        (root / "bundle" / "litellm" / "forge-pricing.cache.json").write_text(
            json.dumps(pricing))
        env["FORGE_API_KEY"] = "test-dummy-key-not-a-real-credential"
        args.append("--offline")

    proc = subprocess.run(
        args, cwd=root, capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    out = root / "bundle" / "litellm" / "config.generated.yaml"
    assert out.exists(), "the real renderer did not produce config.generated.yaml"
    return out.read_text()


@pytest.fixture(scope="session")
def real_fakes_only_yaml(tmp_path_factory) -> str:
    """A genuine render with no FORGE_API_KEY visible -- exactly `env -u FORGE_API_KEY
    -u FORGE_ADMIN_KEY bundle/bin/render-gateway-config.py`, the case this whole item is
    about."""
    return _render_real(tmp_path_factory.mktemp("real-fakes"), forge_backed=False)


@pytest.fixture(scope="session")
def real_forge_yaml(tmp_path_factory) -> str:
    """A genuine Forge-backed render (real code path, synthetic input data)."""
    return _render_real(tmp_path_factory.mktemp("real-forge"), forge_backed=True)


def test_the_guards_textual_signal_holds_against_a_genuinely_rendered_artifact(
    real_fakes_only_yaml, real_forge_yaml,
):
    """The guard's entire correctness rests on one textual claim: a fakes-only render
    contains no lowercase `forge`, a Forge-backed render does. Assert that claim against
    what the real renderer actually emits, once, here -- every other test in this file
    reuses these two fixtures rather than re-asserting this."""
    assert "forge" not in real_fakes_only_yaml
    assert "forge" in real_forge_yaml
    assert real_fakes_only_yaml.count("model_name:") == 3, (
        "expected exactly the 3 hardcoded fake models with no Forge credentials visible"
    )


# --------------------------------------------------------------------------------------
# Stub kubectl / docker
# --------------------------------------------------------------------------------------

# A minimal but real jsonpath evaluator, not a fixed-response stub. The old stub matched
# on `-n <ns> get configmap <name>` and then answered from the fixture file NO MATTER WHAT
# `-o jsonpath=...` expression was given -- so a wrong or unescaped jsonpath in
# catalogue-guard.sh was invisible to every test in this file. Confirmed against the live
# cluster: kubectl itself returns rc=0 with EMPTY stdout when a jsonpath does not resolve
# (not an error) -- indistinguishable from "the field is genuinely empty" -- which is
# reproduced here by walking a `{"data": {"config.yaml": ...}}` structure and returning ""
# on any unresolved path, exactly like a real apiserver does for `kubectl get -o jsonpath`.
_KUBECTL_STUB = r"""#!/usr/bin/env python3
import os
import sys


def resolve_jsonpath(expr, data):
    # expr looks like "{.data.config\.yaml}" -- a leading '.' after '{' means "root",
    # segments split on unescaped '.', and '\.' is a literal dot WITHIN one key segment
    # (this is how client-go's jsonpath addresses a ConfigMap key that itself contains a
    # dot, e.g. "config.yaml"). Losing the escape (a real typo this stub must catch)
    # changes the token split and the lookup below fails to resolve, exactly reproducing
    # the confirmed live-cluster behaviour.
    assert expr.startswith("{") and expr.endswith("}"), expr
    body = expr[1:-1]
    if body.startswith("."):
        body = body[1:]
    tokens, cur, i = [], "", 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            cur += body[i + 1]
            i += 2
            continue
        if c == ".":
            tokens.append(cur)
            cur = ""
            i += 1
            continue
        cur += c
        i += 1
    tokens.append(cur)
    obj = data
    for t in tokens:
        if isinstance(obj, dict) and t in obj:
            obj = obj[t]
        else:
            return None
    return obj


def main():
    args = sys.argv[1:]
    calls_file = os.environ.get("STUB_CALLS_FILE")
    if calls_file:
        with open(calls_file, "a") as f:
            f.write(" ".join(args) + "\n")

    if len(args) >= 4 and args[0] == "-n" and args[2] == "get" and args[3] == "configmap":
        cm_mode = os.environ.get("STUB_CM_MODE", "")
        if cm_mode == "missing":
            cm_name = args[4] if len(args) > 4 else "?"
            sys.stderr.write(
                'Error from server (NotFound): configmaps "%s" not found\n' % cm_name
            )
            sys.exit(1)
        elif cm_mode == "connerror":
            sys.stderr.write(
                "Unable to connect to the server: dial tcp 10.0.0.1:6443: i/o timeout\n"
            )
            sys.exit(1)
        elif cm_mode == "present":
            content_file = os.environ["STUB_CM_CONTENT_FILE"]
            with open(content_file) as f:
                content = f.read()
            configmap = {"data": {"config.yaml": content}}
            jsonpath_expr = None
            if "-o" in args:
                idx = args.index("-o")
                if idx + 1 < len(args) and args[idx + 1].startswith("jsonpath="):
                    jsonpath_expr = args[idx + 1][len("jsonpath="):]
            if jsonpath_expr is None:
                sys.stderr.write("test stub: no jsonpath given\n")
                sys.exit(99)
            resolved = resolve_jsonpath(jsonpath_expr, configmap)
            # A wrong/unescaped key resolves to nothing. Real kubectl (confirmed against
            # the live cluster this session) exits 0 with EMPTY output in that case -- not
            # an error -- which is indistinguishable from "the field is genuinely empty".
            sys.stdout.write(resolved if resolved is not None else "")
            sys.exit(0)
        else:
            sys.stderr.write("test stub misconfigured: STUB_CM_MODE=%r\n" % cm_mode)
            sys.exit(99)

    # rollout status / get pods / create secret|configmap / apply -f - : just succeed.
    sys.exit(0)


if __name__ == "__main__":
    main()
"""

_DOCKER_STUB = """#!/usr/bin/env bash
echo "$*" >> "$STUB_CALLS_FILE"
exit 0
"""


def _make_stub(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture()
def stub_bin(tmp_path) -> Path:
    d = tmp_path / "stubbin"
    d.mkdir()
    _make_stub(d / "kubectl", _KUBECTL_STUB)
    _make_stub(d / "docker", _DOCKER_STUB)
    return d


def test_kubectl_stub_reproduces_the_confirmed_live_cluster_behaviour_on_a_wrong_jsonpath(
    tmp_path, stub_bin, real_forge_yaml,
):
    """Meta-test: pins the stub's own fidelity, independent of the guard. Confirmed
    against the live cluster this session: a correctly-escaped jsonpath resolves the
    ConfigMap's `config.yaml` key; the same key WITHOUT the escape (a plausible typo) does
    NOT resolve, and kubectl still exits 0 with empty stdout -- not an error. If a future
    rewrite of this stub regresses to ignoring the jsonpath argument (the exact defect this
    item's wave-7 review found), this test catches it directly, without needing to go
    through the guard or a mutated copy of it."""
    cm_content_file = tmp_path / "cm.yaml"
    cm_content_file.write_text(real_forge_yaml)
    env = {
        **os.environ,
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
        "STUB_CALLS_FILE": str(tmp_path / "calls.log"),
        "STUB_CM_MODE": "present",
        "STUB_CM_CONTENT_FILE": str(cm_content_file),
    }
    (tmp_path / "calls.log").touch()

    correct = subprocess.run(
        ["kubectl", "-n", "enterprise-ai", "get", "configmap", "gateway-config",
         "-o", r"jsonpath={.data.config\.yaml}"],
        capture_output=True, text=True, env=env, timeout=10,
    )
    wrong = subprocess.run(
        ["kubectl", "-n", "enterprise-ai", "get", "configmap", "gateway-config",
         "-o", r"jsonpath={.data.config.yaml}"],  # missing the backslash escape
        capture_output=True, text=True, env=env, timeout=10,
    )

    assert correct.returncode == 0
    assert correct.stdout == real_forge_yaml
    assert wrong.returncode == 0, "a bad jsonpath must not itself look like an error"
    assert wrong.stdout == "", (
        "a bad jsonpath must resolve to EMPTY, not to the fixture content -- if it "
        "returns the content regardless of the path, this stub cannot catch a jsonpath "
        "typo in catalogue-guard.sh, which is the exact defect this test exists to catch"
    )


# --------------------------------------------------------------------------------------
# Layer 1 — the function itself, every branch
# --------------------------------------------------------------------------------------


def _run_guard(tmp_path, stub_bin, calls_file, cm_mode, cm_content, local_content, extra_env=None):
    """Source the real catalogue-guard.sh and call assert_catalogue_not_downgraded."""
    local_cfg = tmp_path / "config.generated.yaml"
    if local_content is None:
        assert not local_cfg.exists()
    else:
        local_cfg.write_text(local_content)

    cm_content_file = tmp_path / "cluster_cm.yaml"
    cm_content_file.write_text(cm_content or "")

    env = {
        **os.environ,
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
        "STUB_CALLS_FILE": str(calls_file),
        "STUB_CM_MODE": cm_mode,
        "STUB_CM_CONTENT_FILE": str(cm_content_file),
    }
    for k in ("FORGE_API_KEY", "FORGE_ADMIN_KEY"):
        env.pop(k, None)
    if extra_env:
        env.update(extra_env)

    script = (
        f"set -euo pipefail\n"
        f"source '{GUARD_LIB}'\n"
        f"assert_catalogue_not_downgraded enterprise-ai gateway-config '{local_cfg}'\n"
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30,
    )


def test_refuses_when_cluster_is_forge_backed_and_local_is_fakes_only(
    tmp_path, stub_bin, real_forge_yaml, real_fakes_only_yaml,
):
    calls = tmp_path / "calls.log"
    calls.touch()
    proc = _run_guard(
        tmp_path, stub_bin, calls,
        cm_mode="present", cm_content=real_forge_yaml, local_content=real_fakes_only_yaml,
    )
    assert proc.returncode == 1, proc.stderr
    assert "refusing to deploy" in proc.stderr
    assert "downgrade" in proc.stderr.lower() or "fakes-only" in proc.stderr.lower()
    # The guard itself must not have called anything beyond the read it needed.
    logged = calls.read_text().splitlines()
    assert logged == [
        r"-n enterprise-ai get configmap gateway-config -o jsonpath={.data.config\.yaml}"
    ], logged


def test_allows_when_cluster_and_local_are_both_forge_backed(
    tmp_path, stub_bin, real_forge_yaml,
):
    """The unchanged path: redeploying an already-Forge-backed cluster with a fresh
    Forge-backed render (e.g. an updated price list) must not be blocked."""
    calls = tmp_path / "calls.log"
    calls.touch()
    proc = _run_guard(
        tmp_path, stub_bin, calls,
        cm_mode="present", cm_content=real_forge_yaml, local_content=real_forge_yaml,
    )
    assert proc.returncode == 0, proc.stderr


def test_allows_when_cluster_and_local_are_both_fakes_only(
    tmp_path, stub_bin, real_fakes_only_yaml,
):
    """The unchanged path: a demo cluster kept fakes-only stays deployable."""
    calls = tmp_path / "calls.log"
    calls.touch()
    proc = _run_guard(
        tmp_path, stub_bin, calls,
        cm_mode="present", cm_content=real_fakes_only_yaml, local_content=real_fakes_only_yaml,
    )
    assert proc.returncode == 0, proc.stderr


def test_allows_upgrading_a_fakes_only_cluster_to_forge(
    tmp_path, stub_bin, real_fakes_only_yaml, real_forge_yaml,
):
    """The opposite direction (fakes -> real) is not a downgrade and must be allowed."""
    calls = tmp_path / "calls.log"
    calls.touch()
    proc = _run_guard(
        tmp_path, stub_bin, calls,
        cm_mode="present", cm_content=real_fakes_only_yaml, local_content=real_forge_yaml,
    )
    assert proc.returncode == 0, proc.stderr


def test_allows_first_deploy_when_configmap_does_not_exist_yet(
    tmp_path, stub_bin, real_fakes_only_yaml,
):
    calls = tmp_path / "calls.log"
    calls.touch()
    proc = _run_guard(
        tmp_path, stub_bin, calls,
        cm_mode="missing", cm_content="", local_content=real_fakes_only_yaml,
    )
    assert proc.returncode == 0, proc.stderr


def test_override_bypasses_the_refusal(
    tmp_path, stub_bin, real_forge_yaml, real_fakes_only_yaml,
):
    calls = tmp_path / "calls.log"
    calls.touch()
    proc = _run_guard(
        tmp_path, stub_bin, calls,
        cm_mode="present", cm_content=real_forge_yaml, local_content=real_fakes_only_yaml,
        extra_env={"DEPLOY_CONFIRM_CATALOGUE_DOWNGRADE": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "skipped" in proc.stderr.lower()


def test_fails_closed_not_open_on_an_unrelated_kubectl_error(
    tmp_path, stub_bin, real_fakes_only_yaml,
):
    """A guard that treats every kubectl error as 'no ConfigMap yet' is not a guard — a
    transient network blip would silently wave a real downgrade through. Only the
    NotFound case (genuinely no ConfigMap) is allowed to pass; anything else refuses."""
    calls = tmp_path / "calls.log"
    calls.touch()
    proc = _run_guard(
        tmp_path, stub_bin, calls,
        cm_mode="connerror", cm_content="", local_content=real_fakes_only_yaml,
    )
    assert proc.returncode == 1, proc.stderr
    assert "could not read" in proc.stderr.lower()


# --------------------------------------------------------------------------------------
# Layer 2 — deploy.sh actually wires the guard in, before any other kubectl/docker call
# --------------------------------------------------------------------------------------


@pytest.fixture()
def make_deploy_fixture(tmp_path_factory):
    """Factory for a throwaway copy of just enough of the repo for the real deploy.sh to
    run, against stubbed kubectl/docker, all the way to completion when allowed.

    `forge_api_key` controls what ends up in the fixture's bundle/.env, which is what
    deploy.sh actually reads (`set -a; . ./bundle/.env; set +a` overwrites any inherited
    value) -- so it is the only way to make deploy.sh's PRE-EXISTING sibling check ("local
    has forge but FORGE_API_KEY is empty") NOT fire, which is required to let a
    Forge-backed local render reach the new guard at all (enterpriseaiframework-dc0
    challenge #3).
    """

    def _make(forge_api_key: str = "") -> Path:
        root = tmp_path_factory.mktemp("deploy-fixture")

        (root / "deploy" / "bin" / "lib").mkdir(parents=True)
        (root / "deploy" / "k8s").mkdir(parents=True)
        (root / "deploy" / "gateway").mkdir(parents=True)
        (root / "bundle" / "litellm").mkdir(parents=True)
        (root / "bundle" / "librechat").mkdir(parents=True)

        shutil.copy2(DEPLOY_SH, root / "deploy" / "bin" / "deploy.sh")
        (root / "deploy" / "bin" / "deploy.sh").chmod(0o755)
        shutil.copy2(GUARD_LIB, root / "deploy" / "bin" / "lib" / "catalogue-guard.sh")

        for f in ("00-namespace", "10-postgres", "11-data", "20-identity", "30-gateway",
                  "50-chat", "40-control-plane"):
            (root / "deploy" / "k8s" / f"{f}.yaml").write_text(f"# placeholder {f}\n")
        (root / "deploy" / "gateway" / "strip_reasoning.py").write_text("# placeholder\n")
        (root / "bundle" / "librechat" / "librechat.yaml").write_text(
            "endpoints:\n  custom:\n    - baseURL: http://gateway:4000/v1\n"
        )

        env_vars = {
            "POSTGRES_USER": "eai", "POSTGRES_PASSWORD": "x", "GATEWAY_MASTER_KEY": "x",
            "GATEWAY_SALT_KEY": "x", "CONTROL_PLANE_ADMIN_TOKEN": "x",
            "IDP_ADMIN_USER": "admin", "IDP_ADMIN_PASSWORD": "x", "IDP_CLIENT_SECRET": "x",
            "CHAT_CLIENT_SECRET": "x", "CHAT_SESSION_SECRET": "x", "CHAT_JWT_SECRET": "x",
            "CHAT_JWT_REFRESH_SECRET": "x", "CHAT_CREDS_KEY": "x", "CHAT_CREDS_IV": "x",
            "CHAT_VIRTUAL_KEY": "", "IDP_REALM": "enterprise-ai",
            "FORGE_API_KEY": forge_api_key,
        }
        (root / "bundle" / ".env").write_text(
            "\n".join(f"{k}={v}" for k, v in env_vars.items()) + "\n"
        )
        return root

    return _make


@pytest.fixture()
def deploy_fixture(make_deploy_fixture) -> Path:
    return make_deploy_fixture()


def _run_deploy_sh(root, stub_bin, calls_file, cm_mode, cm_content, local_content):
    (root / "bundle" / "litellm" / "config.generated.yaml").write_text(local_content)
    cm_content_file = root / "cluster_cm.yaml"
    cm_content_file.write_text(cm_content or "")

    env = {
        **os.environ,
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
        "PUBLIC_BASE_URL": "https://ai.example.test",
        "STUB_CALLS_FILE": str(calls_file),
        "STUB_CM_MODE": cm_mode,
        "STUB_CM_CONTENT_FILE": str(cm_content_file),
    }
    for k in ("FORGE_API_KEY", "FORGE_ADMIN_KEY"):
        env.pop(k, None)

    return subprocess.run(
        ["deploy/bin/deploy.sh"], cwd=root, capture_output=True, text=True, env=env,
        timeout=60,
    )


def test_deploy_sh_refuses_before_any_other_kubectl_or_docker_call(
    deploy_fixture, stub_bin, real_forge_yaml, real_fakes_only_yaml,
):
    calls = deploy_fixture / "calls.log"
    calls.touch()
    proc = _run_deploy_sh(
        deploy_fixture, stub_bin, calls,
        cm_mode="present", cm_content=real_forge_yaml, local_content=real_fakes_only_yaml,
    )
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "refusing to deploy" in proc.stderr

    logged = calls.read_text().splitlines()
    # The only permitted call is the guard's own read of the current ConfigMap. No
    # namespace apply, no secret, no configmap write, no docker build/push.
    assert logged == [
        r"-n enterprise-ai get configmap gateway-config -o jsonpath={.data.config\.yaml}"
    ], f"deploy.sh made calls beyond the guard's own read before refusing: {logged}"


def test_deploy_sh_still_completes_a_legitimate_deploy(
    deploy_fixture, stub_bin, real_fakes_only_yaml,
):
    """The unchanged path: cluster and local catalogue agree (both fakes-only, as a
    freshly-`make up`'d compose bundle and a freshly-installed demo cluster would), so
    the guard must not be in the way of a real deploy completing."""
    calls = deploy_fixture / "calls.log"
    calls.touch()
    proc = _run_deploy_sh(
        deploy_fixture, stub_bin, calls,
        cm_mode="present", cm_content=real_fakes_only_yaml, local_content=real_fakes_only_yaml,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)

    logged = calls.read_text().splitlines()
    assert any("apply -f deploy/k8s/00-namespace.yaml" in line for line in logged), logged
    assert any("create secret" in line for line in logged), logged
    assert any("gateway-config" in line and "create configmap" in line for line in logged), logged


def test_deploy_sh_reaches_the_guard_when_upgrading_a_fakes_only_cluster_to_forge(
    make_deploy_fixture, stub_bin, real_fakes_only_yaml, real_forge_yaml,
):
    """enterpriseaiframework-dc0 challenge #3: this is the first deploy.sh-level test with
    a Forge-backed LOCAL render. FORGE_API_KEY is set in the fixture's bundle/.env so
    deploy.sh's pre-existing sibling check ('local has forge but FORGE_API_KEY empty')
    does not fire, letting the new guard's "local IS Forge-backed" branch run end-to-end
    for the first time. Cluster is fakes-only -> local is Forge-backed is an upgrade, which
    must be allowed all the way to a completed deploy."""
    root = make_deploy_fixture(forge_api_key="dummy-forge-key-for-sibling-check")
    calls = root / "calls.log"
    calls.touch()
    proc = _run_deploy_sh(
        root, stub_bin, calls,
        cm_mode="present", cm_content=real_fakes_only_yaml, local_content=real_forge_yaml,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)

    logged = calls.read_text().splitlines()
    assert logged[0] == (
        r"-n enterprise-ai get configmap gateway-config -o jsonpath={.data.config\.yaml}"
    ), "the guard's read must still happen, and happen first"
    assert any(
        "gateway-config" in line and "create configmap" in line for line in logged
    ), logged


def test_deploy_sh_reaches_the_guard_when_cluster_and_local_are_both_forge_backed(
    make_deploy_fixture, stub_bin, real_forge_yaml,
):
    """Same gap as above, other unchanged-path branch: a Forge-backed cluster redeployed
    with a fresh Forge-backed render (e.g. an updated price list), FORGE_API_KEY present so
    the sibling check does not fire, must complete."""
    root = make_deploy_fixture(forge_api_key="dummy-forge-key-for-sibling-check")
    calls = root / "calls.log"
    calls.touch()
    proc = _run_deploy_sh(
        root, stub_bin, calls,
        cm_mode="present", cm_content=real_forge_yaml, local_content=real_forge_yaml,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


def test_deploy_sh_refuses_downgrade_even_when_forge_api_key_is_present(
    make_deploy_fixture, stub_bin, real_forge_yaml, real_fakes_only_yaml,
):
    """The dangerous case is not limited to a shell with no Forge creds at all: an
    operator can have FORGE_API_KEY loaded and still be about to push a stale, fakes-only
    render (e.g. rendered before `direnv allow`, or on a machine mid-`op signin`). The
    sibling check does not fire here either (it only fires when the LOCAL file has forge),
    so this exercises the new guard, not the old one, with credentials present."""
    root = make_deploy_fixture(forge_api_key="dummy-forge-key-for-sibling-check")
    calls = root / "calls.log"
    calls.touch()
    proc = _run_deploy_sh(
        root, stub_bin, calls,
        cm_mode="present", cm_content=real_forge_yaml, local_content=real_fakes_only_yaml,
    )
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "refusing to deploy" in proc.stderr
