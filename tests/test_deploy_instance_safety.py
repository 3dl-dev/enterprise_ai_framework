"""A watcher deploy must never regress the operated instance (enterpriseaiframework-e7f).

The incident (2026-09-30): the watcher defaulted PUBLIC_BASE_URL to https://ai.example.org
when unset (always, under systemd), deploy.sh wrote it and OPENID_ISSUER into the live
enterprise-ai-secrets, deploy.sh carried no GATEWAY_PROVIDER so the new control plane had no
freerouter, and 30-gateway.yaml required a FORGE_API_KEY the secret no longer held.

These tests drive the REAL scripts through the inputs the incident came through: the env
file, the ambient environment, and the live cluster values. The only fake is `kubectl`, and
only for the live-value READ (secret + running-pod env), because a hermetic test cannot hold
the production cluster; a fake kubectl that records every call also proves a refusal
happens before any mutation. The read path against the real cluster is exercised separately
by tests-live-style operator run (see the item trail), not here.

Expected values are independent of the code under test: they are the literal production
values from the incident record, hand-written below.
"""
import base64
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy/bin/deploy.sh"
WATCHER = REPO / "deploy/bin/watch-and-deploy.sh"

LIVE_URL = "https://ai.3dl.one"
LIVE_ISSUER = f"{LIVE_URL}/realms/enterprise-ai"

# Every variable deploy.sh requires (no default) plus the operated ones, with dummy secrets.
BASE_ENV = {
    "POSTGRES_PASSWORD": "pgpw", "GATEWAY_MASTER_KEY": "gmk", "GATEWAY_SALT_KEY": "gsk",
    "CONTROL_PLANE_ADMIN_TOKEN": "cpat", "IDP_ADMIN_PASSWORD": "iap", "IDP_CLIENT_SECRET": "ics",
    "CHAT_CLIENT_SECRET": "ccs", "CHAT_SESSION_SECRET": "css", "CHAT_JWT_SECRET": "cjs",
    "CHAT_JWT_REFRESH_SECRET": "cjrs", "CHAT_CREDS_KEY": "cck", "CHAT_CREDS_IV": "cci",
    "CODEAPI_JWT_PRIVATE_KEY": "a", "CODEAPI_JWT_PUBLIC_KEY": "b",
    "CODEAPI_EXECUTION_MANIFEST_PRIVATE_KEY": "c", "SANDBOX_EXECUTION_MANIFEST_PUBLIC_KEY": "d",
    "CODEAPI_INTERNAL_SERVICE_TOKEN": "e", "CODEAPI_EGRESS_GRANT_SECRET": "f",
    "CODEAPI_REDIS_PASSWORD": "g", "MINIO_ROOT_USER": "mu", "MINIO_ROOT_PASSWORD": "mp",
    "WEBFETCH_TOKEN": "w", "RERANK_TOKEN": "r", "SEARXNG_SECRET": "s", "MEILI_MASTER_KEY": "m",
    "RAGVECTOR_PASSWORD": "rv",
}
OPERATED = {"PUBLIC_BASE_URL": LIVE_URL, "GATEWAY_PROVIDER": "freerouter", "IDP_REALM": "enterprise-ai"}


def _write_env(path: Path, values: dict) -> Path:
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items() if v is not None))
    return path


class Cluster:
    """A fake kubectl on PATH serving LIVE values; records every call."""

    def __init__(self, root: Path):
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir(parents=True)
        self.calls = root / "calls.log"
        self.secret = root / "secret.json"
        self.pods = root / "pods"
        self.pods.mkdir()
        k = self.bin / "kubectl"
        k.write_text(f"""#!/usr/bin/env bash
echo "$*" >> {self.calls}
args=" $* "
if [[ "$args" == *" get secret enterprise-ai-secrets -o json "* ]]; then
    [[ -f {self.secret} ]] || {{ echo 'Error from server (NotFound): secrets "enterprise-ai-secrets" not found' >&2; exit 1; }}
    cat {self.secret}; exit 0
fi
if [[ "$args" == *" exec "* && "$args" == *" -- printenv "* ]]; then
    app=$(sed -n 's/.*exec deploy\\/\\([a-z-]*\\) .*/\\1/p' <<<"$*")
    key="${{@: -1}}"
    f={self.pods}/$app.env
    [[ -f $f ]] || exit 1
    v=$(grep "^$key=" $f | head -1 | cut -d= -f2-) || true
    [[ -n "$v" ]] || exit 1
    printf '%s\\n' "$v"; exit 0
fi
echo "UNEXPECTED kubectl call: $*" >&2
exit 97
""")
        k.chmod(0o755)

    def set_secret(self, kv: dict):
        self.secret.write_text(json.dumps({"data": {k: base64.b64encode(v.encode()).decode() for k, v in kv.items()}}))

    def set_pod(self, app: str, kv: dict):
        (self.pods / f"{app}.env").write_text("".join(f"{k}={v}\n" for k, v in kv.items()))

    def verbs(self):
        return [c.split()[2] if c.startswith("-n ") else c.split()[0] for c in self.calls.read_text().splitlines()] if self.calls.exists() else []


@pytest.fixture
def cluster(tmp_path):
    c = Cluster(tmp_path / "cluster")
    # The live production state after the incident was repaired by hand: the secret holds the
    # right URL/issuer, GATEWAY_PROVIDER is '' in the secret but 'freerouter' in the running pod.
    c.set_secret({"PUBLIC_BASE_URL": LIVE_URL, "OPENID_ISSUER": LIVE_ISSUER, "GATEWAY_PROVIDER": "",
                  "FREEROUTER_MASTER_KEY": "", "POSTGRES_PASSWORD": "pgpw", "GATEWAY_MASTER_KEY": "gmk"})
    c.set_pod("control-plane", {"GATEWAY_PROVIDER": "freerouter", "PUBLIC_BASE_URL": LIVE_URL})
    c.set_pod("chat", {"OPENID_ISSUER": LIVE_ISSUER})
    return c


OPERATED_KEYS = set(OPERATED)   # live in the deploy-only overlay, never in bundle/.env


def split_sources(tmp_path, env_values):
    """bundle/.env-style shared file + the operated overlay, written the way production has them."""
    merged = {**BASE_ENV, **env_values}
    shared = _write_env(tmp_path / "instance.env", {k: v for k, v in merged.items() if k not in OPERATED_KEYS})
    overlay = _write_env(tmp_path / "operated.env", {k: v for k, v in merged.items() if k in OPERATED_KEYS})
    return shared, overlay


def run_deploy(tmp_path, cluster, env_values, ambient=None, overlay=True):
    envfile, overlay_file = split_sources(tmp_path, env_values)
    if not overlay:
        overlay_file.unlink()
    env = {"PATH": f"{cluster.bin}:{os.environ['PATH']}", "HOME": str(tmp_path),
           "DEPLOY_ENV_FILE": str(envfile), "DEPLOY_OVERLAY_FILE": str(overlay_file),
           "DEPLOY_CHECK_ONLY": "1", **(ambient or {})}
    return subprocess.run([str(DEPLOY)], capture_output=True, text=True, env=env, cwd=REPO)


def assert_no_mutation(cluster):
    assert set(cluster.verbs()) <= {"get", "exec"}, cluster.calls.read_text()


# ---------------------------------------------------------------- deploy.sh: the pre-deploy check

def test_current_good_values_report_no_change(tmp_path, cluster):
    r = run_deploy(tmp_path, cluster, OPERATED)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "no operated value changes or blanks" in r.stdout
    assert "DEPLOY_CHECK_ONLY set: stopping before any change" in r.stdout
    assert_no_mutation(cluster)


@pytest.mark.parametrize("bad", [
    "https://ai.example.org",   # the incident value
    "https://ai.3dl.one/",      # near-miss: trailing slash
    "http://ai.3dl.one",        # near-miss: scheme
])
def test_a_public_base_url_that_differs_from_live_is_refused_by_name(tmp_path, cluster, bad):
    r = run_deploy(tmp_path, cluster, {**OPERATED, "PUBLIC_BASE_URL": bad})
    assert r.returncode != 0
    assert "REFUSING" in r.stderr and "PUBLIC_BASE_URL" in r.stderr
    assert bad not in r.stdout + r.stderr, "a value was echoed"
    assert_no_mutation(cluster)


@pytest.mark.parametrize("gp", [None, ""])   # key absent from the overlay, and set-but-empty
def test_a_missing_gateway_provider_fails_closed(tmp_path, cluster, gp):
    r = run_deploy(tmp_path, cluster, {**OPERATED, "GATEWAY_PROVIDER": gp})
    assert r.returncode != 0
    assert "GATEWAY_PROVIDER is not set in the operated-instance overlay" in r.stderr
    assert cluster.verbs() == [], "fail-closed must come before any cluster call"


def test_the_predeploy_pod_check_still_refuses_a_blank_when_the_provider_is_allowed_through(tmp_path, cluster):
    # The overlay has a provider, but one that would change what the running pod holds.
    r = run_deploy(tmp_path, cluster, {**OPERATED, "GATEWAY_PROVIDER": "litellm"})
    assert r.returncode != 0 and "running control-plane pod differs" in r.stderr


def test_a_wrong_gateway_provider_is_refused(tmp_path, cluster):
    r = run_deploy(tmp_path, cluster, {**OPERATED, "GATEWAY_PROVIDER": "litellm"})
    assert r.returncode != 0 and "GATEWAY_PROVIDER" in r.stderr


def test_a_key_the_live_secret_holds_would_not_be_blanked(tmp_path, cluster):
    # POSTGRES_PASSWORD is live 'pgpw'; the file now carries a different one.
    r = run_deploy(tmp_path, cluster, {**OPERATED, "POSTGRES_PASSWORD": "rotated"})
    assert r.returncode != 0 and "POSTGRES_PASSWORD" in r.stderr
    assert "rotated" not in r.stdout + r.stderr and "pgpw" not in r.stdout + r.stderr


def test_the_freerouter_keys_are_empty_live_and_deploy_does_not_blank_them(tmp_path, cluster):
    # Item note (4): live FREEROUTER_MASTER_KEY is empty (the bearer lives on a PVC). deploy.sh
    # writes '' and the check agrees: no refusal, so it is not deploy.sh blanking anything.
    r = run_deploy(tmp_path, cluster, OPERATED)
    assert r.returncode == 0
    assert "FREEROUTER_MASTER_KEY" not in r.stderr


def test_ambient_shell_env_never_reaches_the_cluster_values(tmp_path, cluster):
    # The file omits PUBLIC_BASE_URL/GATEWAY_PROVIDER; the caller's shell (direnv, a watcher
    # default) supplies plausible-looking values. They must be ignored: fail closed.
    r = run_deploy(tmp_path, cluster, {"IDP_REALM": "enterprise-ai"},
                   ambient={"PUBLIC_BASE_URL": LIVE_URL, "GATEWAY_PROVIDER": "freerouter"})
    assert r.returncode != 0
    assert "PUBLIC_BASE_URL is not set in the operated-instance overlay" in r.stderr


def test_ambient_wrong_values_do_not_override_the_file(tmp_path, cluster):
    r = run_deploy(tmp_path, cluster, OPERATED,
                   ambient={"PUBLIC_BASE_URL": "https://ai.example.org", "GATEWAY_PROVIDER": "litellm"})
    assert r.returncode == 0, r.stdout + r.stderr


def test_explicit_override_allows_only_the_named_key(tmp_path, cluster):
    bad = {**OPERATED, "PUBLIC_BASE_URL": "https://ai.example.org"}
    ok = run_deploy(tmp_path, cluster, bad, ambient={"ALLOW_OPERATED_CHANGE": "PUBLIC_BASE_URL,OPENID_ISSUER"})  # issuer derives from the URL
    assert ok.returncode == 0, ok.stdout + ok.stderr
    other = run_deploy(tmp_path, cluster, bad, ambient={"ALLOW_OPERATED_CHANGE": "GATEWAY_PROVIDER"})
    assert other.returncode != 0 and "PUBLIC_BASE_URL" in other.stderr


def test_filling_an_empty_live_key_is_allowed(tmp_path, cluster):
    # Live secret GATEWAY_PROVIDER is '' today; the file declaring freerouter fills the hole.
    r = run_deploy(tmp_path, cluster, OPERATED)
    assert "note: GATEWAY_PROVIDER: live secret empty/absent, deploy fills it" in r.stdout


def test_first_deploy_has_nothing_to_compare(tmp_path, cluster):
    cluster.secret.unlink()
    r = run_deploy(tmp_path, cluster, {**OPERATED, "PUBLIC_BASE_URL": "https://new.example.net"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "first deploy" in r.stdout


def test_an_unreadable_live_secret_fails_closed(tmp_path, cluster):
    (cluster.bin / "kubectl").write_text("#!/usr/bin/env bash\necho 'connection refused' >&2\nexit 1\n")
    r = run_deploy(tmp_path, cluster, OPERATED)
    assert r.returncode != 0 and "cannot read the live secret" in r.stderr


# ---------------------------------------------------------------- watcher: fail closed

@pytest.fixture
def watcher_repo(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    repo = tmp_path / "repo"
    (repo / "deploy/bin").mkdir(parents=True)
    shutil.copy(WATCHER, repo / "deploy/bin/watch-and-deploy.sh")
    (repo / "deploy/bin/lib").mkdir()
    shutil.copy(REPO / "deploy/bin/lib/instance-source.sh", repo / "deploy/bin/lib/instance-source.sh")
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    g("init", "-q", "-b", "main"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (repo / ".gitignore").write_text("bundle/.env\n")
    g("add", "-A"); g("commit", "-qm", "seed")
    g("remote", "add", "origin", str(origin)); g("push", "-q", "origin", "main")
    return repo


def run_watcher(repo, tmp_path, env_values, ambient=None, write_file=True, shared_values=None):
    envfile = tmp_path / "instance.env"
    overlay = tmp_path / "operated.env"
    _write_env(envfile, shared_values or {})
    if write_file:
        _write_env(overlay, env_values)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "WATCH_STATE_DIR": str(tmp_path / "state"),
           "DEPLOY_ENV_FILE": str(envfile), "DEPLOY_OVERLAY_FILE": str(overlay),
           # A floor no disk meets: a run that gets past the instance checks stops here, before
           # `make up`, so this test can never start a real stack or deploy.
           "MIN_FREE_GB": "999999999", **(ambient or {})}
    return subprocess.run([str(repo / "deploy/bin/watch-and-deploy.sh")], capture_output=True, text=True, env=env, cwd=repo)


@pytest.mark.parametrize("values,why", [
    ({}, "unset"),
    ({"PUBLIC_BASE_URL": ""}, "empty"),
    ({"PUBLIC_BASE_URL": "https://ai.example.org"}, "placeholder"),
    ({"PUBLIC_BASE_URL": "http://ai.3dl.one"}, "plaintext"),
])
def test_watcher_fails_closed_without_a_real_public_base_url(watcher_repo, tmp_path, values, why):
    # Ambient carries the incident's default AND the live value: neither may rescue an unset file.
    r = run_watcher(watcher_repo, tmp_path, {"GATEWAY_PROVIDER": "freerouter", **values}, ambient={"PUBLIC_BASE_URL": LIVE_URL})
    out = r.stdout + r.stderr
    assert r.returncode == 1 and "STOP:" in out and "PUBLIC_BASE_URL" in out, (why, out)
    assert "disk" not in out, "reached the disk check: the instance check did not come first"


def test_watcher_fails_closed_when_the_instance_file_is_missing(watcher_repo, tmp_path):
    r = run_watcher(watcher_repo, tmp_path, {}, write_file=False, ambient={"PUBLIC_BASE_URL": LIVE_URL, "GATEWAY_PROVIDER": "freerouter"})
    out = r.stdout + r.stderr
    assert r.returncode == 1 and "operated-instance overlay" in out and "missing" in out
    assert "disk" not in out


def test_watcher_fails_closed_when_the_overlay_lacks_a_gateway_provider(watcher_repo, tmp_path):
    r = run_watcher(watcher_repo, tmp_path, {"PUBLIC_BASE_URL": LIVE_URL})
    out = r.stdout + r.stderr
    assert r.returncode == 1 and "STOP:" in out and "GATEWAY_PROVIDER is not set in the operated-instance overlay" in out
    assert "disk" not in out


def test_watcher_does_not_accept_operated_values_from_the_shared_file_alone(watcher_repo, tmp_path):
    # The incident shape inverted: operated values sitting in bundle/.env (compose reads it) do not
    # satisfy a missing overlay; they must be declared in the overlay.
    r = run_watcher(watcher_repo, tmp_path, {}, write_file=False,
                    shared_values={"PUBLIC_BASE_URL": LIVE_URL, "GATEWAY_PROVIDER": "freerouter"})
    assert r.returncode == 1 and "operated-instance overlay" in r.stdout + r.stderr


def test_watcher_with_a_declared_https_url_gets_past_the_instance_check(watcher_repo, tmp_path):
    r = run_watcher(watcher_repo, tmp_path, {"PUBLIC_BASE_URL": LIVE_URL, "GATEWAY_PROVIDER": "freerouter"})
    out = r.stdout + r.stderr
    assert f"declares PUBLIC_BASE_URL={LIVE_URL}" in out
    assert "only " in out and "GB free" in out, out   # stopped at the (unmeetable) disk floor, nothing deployed


def test_watcher_carries_no_placeholder_default_and_passes_nothing_ambient_to_deploy():
    b = WATCHER.read_text()
    assert "ai.example.org" not in re.sub(r"#.*", "", b).replace('*example.org*', "")
    assert 'PUBLIC_BASE_URL="$PUBLIC_BASE_URL" deploy/bin/deploy.sh' not in b


# ---------------------------------------------------------------- the overlay

def test_overlay_wins_over_the_shared_file(tmp_path, cluster):
    # bundle/.env carries a stale (wrong) URL and provider; the overlay carries the live ones.
    shared, overlay = split_sources(tmp_path, OPERATED)
    with shared.open("a") as f:
        f.write("PUBLIC_BASE_URL=https://ai.example.org\nGATEWAY_PROVIDER=litellm\n")
    env = {"PATH": f"{cluster.bin}:{os.environ['PATH']}", "HOME": str(tmp_path), "DEPLOY_ENV_FILE": str(shared),
           "DEPLOY_OVERLAY_FILE": str(overlay), "DEPLOY_CHECK_ONLY": "1"}
    r = subprocess.run([str(DEPLOY)], capture_output=True, text=True, env=env, cwd=REPO)
    assert r.returncode == 0, r.stdout + r.stderr   # would be REFUSING if the shared file won
    assert_no_mutation(cluster)


def test_a_missing_overlay_fails_closed_before_any_cluster_call(tmp_path, cluster):
    r = run_deploy(tmp_path, cluster, OPERATED, overlay=False,
                   ambient={"PUBLIC_BASE_URL": LIVE_URL, "GATEWAY_PROVIDER": "freerouter"})
    assert r.returncode != 0 and "operated-instance overlay" in r.stderr and "missing" in r.stderr
    assert cluster.verbs() == []


def test_operated_values_in_the_shared_file_alone_do_not_satisfy_deploy(tmp_path, cluster):
    shared, overlay = split_sources(tmp_path, {})
    overlay.write_text("")
    with shared.open("a") as f:
        f.write(f"PUBLIC_BASE_URL={LIVE_URL}\nGATEWAY_PROVIDER=freerouter\n")
    env = {"PATH": f"{cluster.bin}:{os.environ['PATH']}", "HOME": str(tmp_path), "DEPLOY_ENV_FILE": str(shared),
           "DEPLOY_OVERLAY_FILE": str(overlay), "DEPLOY_CHECK_ONLY": "1"}
    r = subprocess.run([str(DEPLOY)], capture_output=True, text=True, env=env, cwd=REPO)
    assert r.returncode != 0 and "not set in the operated-instance overlay" in r.stderr


def test_default_overlay_path_is_outside_the_repo_and_under_home(tmp_path, cluster):
    # No DEPLOY_OVERLAY_FILE: the script must look in $HOME/.config/enterprise-ai/operated.env.
    shared, _ = split_sources(tmp_path, OPERATED)
    (tmp_path / ".config/enterprise-ai").mkdir(parents=True)
    _write_env(tmp_path / ".config/enterprise-ai/operated.env", OPERATED)
    env = {"PATH": f"{cluster.bin}:{os.environ['PATH']}", "HOME": str(tmp_path), "DEPLOY_ENV_FILE": str(shared),
           "DEPLOY_CHECK_ONLY": "1"}
    r = subprocess.run([str(DEPLOY)], capture_output=True, text=True, env=env, cwd=REPO)
    assert r.returncode == 0, r.stdout + r.stderr


OVERLAY_NAMES = ("operated.env", "DEPLOY_OVERLAY_FILE", ".env.operated", "instance-source.sh")

# The files compose and the bundle scripts run from: what git tracks under bundle/ plus the
# Makefile. Two things are deliberately NOT scanned, and both are why the e7f version of this
# scan went red only in the primary checkout:
#   * the vendored submodule bundle/codeapi (ClickHouse/code-interpreter, pinned by gitlink).
#     It is not repo-owned, and it cannot reach the overlay: compose uses it only as a docker
#     BUILD CONTEXT (no host mounts on any codeapi service), so its code runs inside containers
#     whose environment is exactly what docker-compose.yml declares, and docker-compose.yml IS
#     scanned. Its `process.env.X` (JavaScript attribute access) and its .dockerignore's `.env.*`
#     (an exclusion) are not env-file reads at all.
#   * untracked/ignored output (bundle/.env, generated configs, exports). It is written by the
#     tracked renderers, which are scanned, and it differs per checkout, which is what made the
#     result depend on WHICH checkout ran the suite.
def _repo_owned_bundle_files():
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "--", "bundle", "Makefile"],
                         capture_output=True, check=True).stdout.decode()
    return [REPO / p for p in out.split("\0") if p and (REPO / p).is_file()]


# The only env file compose and the bundle scripts may read: the shared bundle/.env.
SHARED_ENV = {".env", "./.env", "bundle/.env", "$(BUNDLE)/.env", "${BUNDLE}/.env", "$BUNDLE/.env"}
# `.env` used as a PATH (after start, whitespace, quote, `/`, `=`, a bracket, `:` or `,`) — not
# attribute access like JavaScript `process.env.X` / `process.env[k]`, where an identifier,
# `]` or `)` precedes it.
_PATH_ENV = r"(?<![\w$\])])\.env"
_SHELL_WORD = r"(\"[^\"]*\"|'[^']*'|(?:\$\([^)]*\)|\$\{[^}]*\}|[^\s;|&()\"'])+)"


def _unquote(w):
    return w[1:-1] if len(w) >= 2 and w[0] == w[-1] and w[0] in "\"'" else w


def _is_shell(path, text):
    return path.suffix == ".sh" or path.name == "Makefile" or re.match(r"#!.*\b(ba|z|da)?sh\b", text) is not None


def _overlay_reads(path, text):
    """Every way `text` (the content of `path`) could read the operated-instance overlay.

    The overlay lives at ~/.config/enterprise-ai/operated.env, outside the repo, so a read has
    to name it, name its directory, or read an env FILE whose path it controls. What counts as
    an env-file read is the set of forms that load a file into an environment: compose
    `env_file:`, `--env-file`, COMPOSE_ENV_FILES, shell `source`/`.`, and dotenv loaders; plus
    any path token or glob that names a `.env` sibling (`.env.operated`, `.env*`, `*.env`),
    whatever verb reads it.
    """
    hits = [n for n in OVERLAY_NAMES if n in text]
    hits += [n for n in ("enterprise-ai/operated", ".config/enterprise-ai") if n in text]
    # the overlay's parent directory, however $HOME is spelled
    hits += [f"home .config {m}" for m in re.findall(r"(?:~|\$HOME|\$\{HOME[^}]*\})/\.config\b", text)]
    # compose: an env_file key in any spelling (block, flow, quoted, JSON), and the env var form
    hits += [f"env_file {m}" for m in re.findall(r"\benv_file\b[\"']?\s*:", text)]
    hits += [f"compose env {m}" for m in re.findall(r"\bCOMPOSE_ENV_FILES\b", text)]
    # --env-file with anything but the shared file, quoted or not, `=` or space
    hits += [f"--env-file {m}" for m in re.findall(r"--env-file(?:=|[ \t]+)" + _SHELL_WORD, text)
             if _unquote(m) not in SHARED_ENV]
    hits += [f"--env-file {m}" for m in re.findall(r"--env-file(?:=|[ \t]*)(\\?\n|$)", text, re.M)]
    # dotenv loaders (python-dotenv, node dotenv, the dotenv/env-cmd CLIs)
    hits += [f"dotenv {m}" for m in re.findall(
        r"\b(load_dotenv|dotenv_values|find_dotenv|from\s+dotenv|dotenv/config|require\(\s*['\"]dotenv|"
        r"dotenv\.config|dotenv\s+(?:-f|-e|run)|env-cmd)\b", text)]
    # shell source / . of anything but the shared file
    if _is_shell(path, text):
        code = re.sub(r"(?m)(^|\s)#.*$", r"\1", text)   # a comment executes nothing
        for m in re.findall(r"(?:^|[;&|({!]|\bthen|\bdo|\belse)[ \t]*(?:source|\.)[ \t]+" + _SHELL_WORD, code, re.M):
            if _unquote(m) not in SHARED_ENV:
                hits.append(f"source {m}")
    # a path naming a .env sibling, or a glob/brace over .env
    hits += [f"glob {m}" for m in re.findall(_PATH_ENV + r"\.?[*?\[{]", text)]
    hits += [f"glob {m}" for m in re.findall(r"\*\.env\b", text)]
    hits += [f"sibling {m}" for m in re.findall(_PATH_ENV + r"\.[A-Za-z0-9*?_-]+", text)
             if m not in {".env.example", ".env.tmp"} and not m.startswith(".env.ALLOW_")]
    return hits


def test_compose_files_and_bundle_scripts_never_read_the_overlay():
    """The incident: operated values in a file compose reads flipped the local stack to freerouter."""
    files = _repo_owned_bundle_files()
    assert any(f.name == "docker-compose.yml" for f in files) and any(f.parent.name == "bin" for f in files)
    assert (REPO / "Makefile") in files
    offenders = []
    for f in files:
        try:
            text = f.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        offenders += [f"{f.relative_to(REPO)}: {h}" for h in _overlay_reads(f, text)]
    assert not offenders, offenders


# Controls for the scan above, fed through the same input it reads (a file's text).
# Every form here reaches the overlay (or an env file compose/the scripts would load) and must
# be caught; the benign forms below it must not be.
_READS = [
    ("Makefile", "COMPOSE := docker compose --env-file $(HOME)/.config/enterprise-ai/operated.env\n"),
    ("Makefile", "COMPOSE := docker compose --env-file $(BUNDLE)/.env --env-file $(OVERLAY)\n"),
    ("Makefile", "COMPOSE := docker compose --env-file=$(BUNDLE)/.env.operated\n"),
    ("x.sh", 'docker compose --env-file "$OVERLAY" up -d\n'),
    ("x.sh", "docker compose --env-file '$ENVF' up -d\n"),
    ("x.sh", "docker compose --env-file \\\n  \"$ENVF\" up -d\n"),
    ("x.sh", "export COMPOSE_ENV_FILES=.env,/etc/x.env\n"),
    ("docker-compose.yml", "services:\n  gw:\n    env_file:\n      - ../x.env\n"),
    ("docker-compose.yml", "services:\n  gw:\n    env_file: [.env]\n"),
    ("docker-compose.yml", "services: {gw: {env_file: .env}}\n"),
    ("docker-compose.yml", "services:\n  gw:\n    \"env_file\" : x\n"),
    ("docker-compose.yml", "services:\n  gw:\n    env_file:\n      - path: ./x\n        required: false\n"),
    ("docker-compose.yml", "services:\n  gw:\n    volumes:\n      - ${HOME}/.config:/cfg:ro\n"),
    ("docker-compose.yml", "services:\n  gw:\n    volumes:\n      - ~/.config:/cfg:ro\n"),
    ("x.json", '{"services": {"gw": {"env_file": "x"}}}'),
    ("x.sh", "set -a; . \"$DEPLOY_OVERLAY_FILE\"; set +a\n"),
    ("x.sh", "set -a; . \"$OVERLAY\"; set +a\n"),
    ("x.sh", "source ~/.config/enterprise-ai/operated.env\n"),
    ("x.sh", "source \"$F\"\n"),
    ("x.sh", "if true; then . ./.env.operated; fi\n"),
    ("x.sh", ". \"$HOME/.config/x\"\n"),
    ("x.sh", "for f in .env*; do cat \"$f\"; done\n"),
    ("x.sh", "cat ./.env.? >> .env\n"),
    ("x.sh", "cat .env.[a-z]* >> .env\n"),
    ("x.sh", "cat .env{,.operated} >> .env\n"),
    ("x.sh", "cat \"$DIR\"/*.env >> .env\n"),
    ("x.sh", "export $(grep -v '^#' .env.operated | xargs)\n"),
    ("x.py", "from dotenv import load_dotenv\nload_dotenv('/etc/x')\n"),
    ("x.py", "vals = dotenv_values(p)\n"),
    ("x.py", "open(os.path.join(root, '.env.local')).read()\n"),
    ("x.py", "glob.glob('.env*')\n"),
    ("x.js", "require('dotenv').config({ path: p })\n"),
    ("x.js", "import 'dotenv/config'\n"),
    ("x.sh", "dotenv -f /x run -- up\n"),
    ("x.sh", "env-cmd -f x node s.js\n"),
    ("x.sh", "cp \"$DEPLOY_OVERLAY_FILE\" .\n"),
    ("x.sh", "o=\"${HOME:-/root}/.config/enterprise-ai\"; cat \"$o/op\"\n"),
    ("x.sh", ". deploy/bin/lib/instance-source.sh\n"),
]
_NOT_READS = [
    # the false positives from bundle/codeapi/test-sandbox.sh (JavaScript attribute access)
    ("x.sh", "const payload = JSON.parse(process.env.PAYLOAD_JSON);\n"),
    ("x.sh", "  const value = Number(process.env[name]);\n"),
    ("x.sh", "const k = process.env.CODEAPI_EXECUTION_MANIFEST_PRIVATE_KEY.trim();\n"),
    ("x.py", "os.environ['X']\nos.environ.get('Y')\n"),
    # the shared file, every spelling the bundle uses
    ("Makefile", "COMPOSE := docker compose -f $(BUNDLE)/docker-compose.yml --env-file $(BUNDLE)/.env\n"),
    ("x.sh", "COMPOSE=(docker compose -f docker-compose.yml --env-file .env)\n"),
    ("x.sh", "set -a; . ./.env; set +a\n"),
    ("x.sh", "source \"./.env\"\n"),
    ("x.sh", "cp .env.example .env\n"),
    # a sentence ending in a period in shell prose is not a source
    ("x.sh", "# the key lives in .env. Next line.\necho hi\n"),
]


@pytest.mark.parametrize("name,text", _READS, ids=[f"read{i}" for i in range(len(_READS))])
def test_the_overlay_scan_catches_every_env_file_read_form(name, text):
    assert _overlay_reads(Path(name), text), f"missed: {text!r}"


@pytest.mark.parametrize("name,text", _NOT_READS, ids=[f"benign{i}" for i in range(len(_NOT_READS))])
def test_the_overlay_scan_does_not_flag_attribute_access_or_the_shared_file(name, text):
    assert _overlay_reads(Path(name), text) == []


def test_the_vendored_submodule_is_not_in_the_scan_and_cannot_reach_the_host():
    """Why bundle/codeapi is out of scope: it enters compose only as a build context, never as a
    host mount, so nothing in it runs with the host filesystem (and the overlay) visible."""
    files = {f.relative_to(REPO).as_posix() for f in _repo_owned_bundle_files()}
    assert not any(f.startswith("bundle/codeapi/") for f in files)
    compose = yaml.safe_load((REPO / "bundle/docker-compose.yml").read_text())
    for name, svc in compose["services"].items():
        build = svc.get("build")
        ctx = build.get("context") if isinstance(build, dict) else build
        if not (ctx and ctx.rstrip("/").endswith("codeapi")):
            continue
        for v in svc.get("volumes") or []:
            src = v.get("source", "") if isinstance(v, dict) else str(v).split(":")[0]
            assert not src.startswith(("/", ".", "~", "$")), f"{name} mounts host path {src}"


def test_the_overlay_default_path_is_not_inside_the_repo():
    lib = (REPO / "deploy/bin/lib/instance-source.sh").read_text()
    assert '${HOME:-/root}/.config/enterprise-ai/operated.env' in lib


# ---------------------------------------------------------------- manifests

def _secret_refs():
    refs = set()

    def walk(o):
        if isinstance(o, dict):
            r = o.get("secretKeyRef")
            if r and r.get("name") == "enterprise-ai-secrets" and not r.get("optional"):
                refs.add(r["key"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    for f in (REPO / "deploy/k8s").glob("*.yaml"):
        for d in yaml.safe_load_all(f.read_text()):
            walk(d)
    return refs


def test_forge_is_retired_from_the_manifests():
    for f in (REPO / "deploy/k8s").glob("*.yaml"):
        assert "FORGE_API_KEY" not in f.read_text(), f"{f.name} still requires the retired Forge credential"


# Keys the manifests require that NO script in this repo writes: they exist in the live secret
# because an operator put them there once, and deploy.sh's apply leaves keys it does not name
# alone. Listed so a NEW such key is a test failure, not a silent CreateContainerConfigError.
# A fresh cluster deployed from the repo alone would not have these (filed as a finding).
OPERATOR_PROVISIONED = {"PORTAL_CLIENT_SECRET", "PORTAL_COOKIE_SECRET", "PORTAL_REDIRECT_URL",
                        "WORKSPACE_INTERNAL_TOKEN"}


def test_every_required_secret_key_has_a_writer():
    """A manifest that hard-requires a secret key nobody writes is CreateContainerConfigError on
    the next roll (the FORGE_API_KEY failure, generalised)."""
    others = "".join(p.read_text() for p in (REPO / "deploy/bin").glob("*.sh") if p != DEPLOY)
    written_by_deploy = set(re.findall(r"--from-literal=([A-Z_0-9]+)=", DEPLOY.read_text()))
    for key in _secret_refs() - written_by_deploy - OPERATOR_PROVISIONED:
        assert re.search(rf"\b{key}\b", others), f"nothing writes {key}"
