"""The whole Agents surface, composed, on the live cluster — and the Code surface unmoved.

Run against the live k3s cluster:  pytest tests-live/test_agents_e2e.py

This is `enterpriseaiframework-ede`, the integration gate for epic -da7. Every one of the
six feature items was proven in isolation and each has its own file next to this one. What
none of them can prove is that they COMPOSE: that one agent, provisioned once, is at the
same time a resident daemon (-055), reachable through the portal's console (-0e7), spending
through the gateway onto the one bill (-39d), metered for resident time and compute
(-914), owned and lifecycled by its user (-627), and holding a mailbox (-a4e) — and that
none of it moved the surface the camp runs on tomorrow.

So this file provisions ONE agent and drives ONE continuous scenario across it, in the
order a person would live it, and then asserts the Code surface is byte-identical and its
own suite still green.

WHY THE PORTAL IS RUN HERE INSTEAD OF BEING CALLED ON THE CLUSTER
=================================================================
The deployed `control-plane` Deployment is running image `04e98a9`, which PREDATES this
epic. Measured, not assumed — `/openapi.json` on that pod lists no `/agents/{name}/`, no
`/portal/api/agents` and no `/admin/agents/usage`, and `app.gateway` there has no
`is_known_surface`, so it answers `unknown surface: agents/<name>` to a mint. Rolling it
forward would be the obvious fix and it is exactly what this item forbids: the external
OIDC front door is flapping (gate -6f3) and the camp's login is downstream of that pod.

So NOTHING here touches that Deployment. The NEW app is run IN THIS PROCESS on loopback,
the way -0e7's file does it, and the substitution is pushed as far towards "the cluster"
and away from "the code" as it will go:

  * IDENTITY AND AUTHORITY ARE REAL AND ARE THE SHIPPED ONES. `deploy/k8s/39-control-plane-rbac.yaml`
    — the epic's own RBAC — is applied, a token is minted for the `control-plane`
    ServiceAccount it creates, and the app reads it through `KUBE_SA_DIR` exactly as a pod
    reads a projected token. The API server is addressed over TLS verified against the
    cluster CA, and the kubelet scrape is the shipped direct-to-`hostIP` one. The app's
    own cluster reads never borrow the operator's admin rights — no `kubectl proxy` stands
    in front of them — so if that RBAC manifest grants too little, this suite goes red,
    which is itself worth having. (The operator's kubeconfig IS used, deliberately, for
    the INDEPENDENT cAdvisor cross-check below: a different identity on a different route
    is the point of a cross-check.)
  * THE DATABASES, THE GATEWAY AND THE IDP ARE THE LIVE ONES, reached by port-forward and
    named in the environment. `/portal/api/spend`'s inference half is therefore the real
    LiteLLM ledger, and its usage half is the real `agent_usage` table.
  * ONLY TWO THINGS ARE SUBSTITUTED, and both are the CLUSTER rather than the code: the
    ADDRESS of each service (which a pod gets from its environment) and the RESOLUTION of
    the agent Service's NAME (which a pod gets from cluster DNS).

`provision-agent.sh` mints through "the control plane on localhost:18091" — it stands its
own port-forward up to reach one. This file is already listening there with the NEW app,
so the script's port-forward finds the port taken and its `curl` reaches the new control
plane instead of the old one. The script is unmodified and its full mint path runs,
alias assertion included. `test_the_control_plane_under_test_is_the_new_one_on_18091`
checks that substitution rather than assuming it.

WHERE THE EXPECTED VALUES COME FROM
===================================
Not from the code under test, in every claim that matters:

  * the resident pid, its start time and the container restart count are read with
    `kubectl exec` / `kubectl get pod`;
  * THE INFERENCE LEDGER ROW IS READ BY THE DEPLOYED CONTROL PLANE, not by the app under
    test — a different image, a different process, a different route to the same gateway
    database. That it renders `baron / agents/e2eede` at all is the composition result:
    the pre-agents bill reader needed no change to see the agents surface, because the
    surface rides in the alias;
  * resident time is bounded against the pod's own `.status.startTime` and compute against
    an INDEPENDENT scrape of the same cAdvisor endpoint taken with the operator's
    kubeconfig through `nodes/proxy` — a different identity on a different route;
  * the mailbox proof's expected values come out of a real IMAP mailbox on a throwaway
    GreenMail this file owns, never out of `agent-email`;
  * "the Code surface is unchanged" is `git diff` against the pinned baseline plus a real
    run of `tests/test_workspace_shell.py`.

THE ONE THING THAT IS NOT DONE IN THE POD, AND WHY
==================================================
`agent-email`'s send and read are executed on the TEST HOST, with the agent's own tool
bytes (read out of the running pod) and the agent's own environment (read out of the
running pod), against the fixture. They are not executed inside the pod because THEY
CANNOT BE, and that is a correct property being measured rather than a gap being papered
over: `deploy/k8s/63-agent-common.yaml` excludes `192.168.0.0/16` from an agent's egress,
the fixture is on this host at its own LAN address inside that range, and
`test_the_agent_pod_cannot_reach_the_fixture_because_the_policy_forbids_it` proves the pod
is fenced off from it while the gateway stays reachable. A real mailbox is an external
provider on a public address (-a4e's ruling), which that same policy admits; a
private-range fixture is exactly what it must refuse.

COST. Two real model calls on the cheapest catalogue entry with `max_tokens=8`.

CLEANUP. Everything is named `agent-baron-e2eede*`. Module teardown deletes both agents,
their PVCs, Services and Secrets, revokes the virtual keys at the gateway and in the
control plane, deletes the usage rows, removes the RBAC objects this file applied, and
kills the GreenMail container — and runs even when a test fails. The camp fixtures
ws-baron / ws-claire / ws-student are never touched, and the last test in this file looks
at them to make sure.
"""

import base64
import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "control-plane"))

NS = "enterprise-ai"

# A REAL principal. An integrated agent's virtual key needs one to be minted for, exactly
# as tests-live/test_agent_model_api.py and tests-live/test_agent_usage.py record.
USER = "baron"
NAME = "e2eede"
BYO = "e2eedebyo"
OBJ = f"agent-{USER}-{NAME}"
BYO_OBJ = f"agent-{USER}-{BYO}"
SURFACE = f"agents/{NAME}"
BYO_SURFACE = f"agents/{BYO}"
ALIAS = f"{USER}::{SURFACE}"
BYO_ALIAS = f"{USER}::{BYO_SURFACE}"

# The cheapest entry in this cluster's catalogue.
MODEL = "gemma-3-4b"

# Where a BYO agent's inference is pointed: a real HTTP server the agent NetworkPolicy
# admits, which is NOT our gateway. Same choice, same reason, as -39d's file.
BYO_BASE = "http://mcp-echo:8080/v1"

# The Service's own port. `kubectl port-forward` binds the SAME number locally because the
# console proxy dials it by number and takes only the ADDRESS from name resolution.
SERVE_PORT = 4096

# provision-agent.sh reaches "the control plane" here. Not a choice this file makes — it
# is the port the script itself uses, and taking it is how the NEW app gets to answer.
CP_PORT = 18091

# The RBAC this file applies so the locally-run app holds the authority the shipped
# Deployment is meant to hold, and no more. Removed again in teardown.
RBAC_MANIFEST = ROOT / "deploy/k8s/39-control-plane-rbac.yaml"

# The frozen Code surface, quoted from Contract 6 of docs/design/records/agents-surface.md,
# and the commit the epic began at. Spelled out here rather than imported from
# tests/test_agents_code_untouched.py: this file is the independent check, and a check
# that imports its own subject follows a rename instead of catching one.
PRE_AGENTS_BASELINE = "5942a5ccc3acea87b048a02d904cf33407718c6d"
FROZEN = (
    "deploy/workspace",
    "deploy/k8s/60-workspace-common.yaml",
    "deploy/k8s/61-workspace.template.yaml",
    "deploy/bin/provision-workspace.sh",
    "tests/test_workspace_shell.py",
)

READY_TIMEOUT_S = 480
LEDGER_TIMEOUT_S = 240
DISCONNECT_WINDOW_S = 20

# Where deploy/agent/entrypoint.sh mounts the agent ConfigMap and puts the mail tool. The
# directory is what the entrypoint appends to PATH; the absolute path is used throughout
# this file because a `kubectl exec` shell does not inherit that export — see
# test_configuring_email_puts_a_real_mailbox_on_the_live_agent.
AGENT_EMAIL_DIR = "/etc/agent"
AGENT_EMAIL_PATH = f"{AGENT_EMAIL_DIR}/agent-email"

GREENMAIL_IMAGE = "greenmail/standalone:2.1.9"
MAIL_USER = "agent-e2eede@agent.test"
MAIL_PASSWORD = "e2eede-mailbox-password"
MAIL_PEER = "peer-e2eede@agent.test"
MAIL_PEER_PASSWORD = "e2eede-peer-password"


# ---------------------------------------------------------------- shelling out


def _run(*args, check=True, timeout=900, **kw):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          check=check, **kw)


def _kubectl(*args, check=True, timeout=300) -> str:
    return _run("kubectl", "-n", NS, *args, check=check, timeout=timeout).stdout


def _secret_value(secret: str, key: str) -> str:
    raw = _kubectl("get", "secret", secret, "-o", f"jsonpath={{.data.{key}}}",
                   check=False).strip()
    return base64.b64decode(raw).decode() if raw else ""


def _in_control_plane(script: str) -> str:
    """Run python inside the DEPLOYED control-plane container.

    Used for exactly two things, and never to exercise the code under test: reading the
    one bill from an image that predates this epic, and the credentialed cleanup that
    needs the gateway master key. The pod is only ever read from and exec'd into; it is
    never restarted, scaled or patched.
    """
    pod = _kubectl("get", "pod", "-l", "app=control-plane",
                   "-o", "jsonpath={.items[0].metadata.name}").strip()
    return _run("kubectl", "-n", NS, "exec", pod, "-c", "control-plane", "--",
                "python3", "-c", script, timeout=300).stdout


def _deployed_spend_rows() -> list[dict]:
    """The one bill, rendered by the DEPLOYED control plane over the real gateway ledger."""
    out = _in_control_plane(
        "import os,httpx,json;"
        "r=httpx.get('http://127.0.0.1:8000/admin/spend',"
        "headers={'Authorization':'Bearer '+os.environ['CONTROL_PLANE_ADMIN_TOKEN']},"
        "timeout=120);"
        "r.raise_for_status();"
        "print(json.dumps(r.json()['by_user_and_surface']))"
    )
    return json.loads(out.strip().splitlines()[-1])


def _gateway_aliases() -> list[str]:
    """EVERY key alias the gateway holds, paged to exhaustion.

    Paged rather than asked for in one go, and the page size is 100 because LiteLLM
    REFUSES anything larger — `size=200` is a 422, not a longer list. Getting that wrong
    is how a negative assertion goes quiet: this cluster carries well over a hundred keys
    for the camp's fixtures, so a single unpaged page would silently omit whichever ones
    fell off the end, and "the BYO alias is not in this list" would be satisfied by a
    truncated list rather than by an absent key. The status is checked so that a future
    cap change fails loudly instead of returning nothing.
    """
    out = _in_control_plane(
        "import os,httpx,json\n"
        "h={'Authorization':'Bearer '+os.environ['GATEWAY_MASTER_KEY']}\n"
        "aliases=[];page=1\n"
        "while True:\n"
        "    r=httpx.get('http://gateway:4000/key/list',headers=h,"
        "params={'return_full_object':'true','page':page,'size':100},timeout=120)\n"
        "    r.raise_for_status()\n"
        "    ks=r.json()['keys']\n"
        "    aliases += [k.get('key_alias') for k in ks if isinstance(k,dict)]\n"
        "    if len(ks) < 100 or page > 50: break\n"
        "    page += 1\n"
        "print(json.dumps(aliases))\n"
    )
    return json.loads(out.strip().splitlines()[-1])


def _deployed_rows_for(surface: str) -> list[dict]:
    return [r for r in _deployed_spend_rows()
            if r.get("username") == USER and r.get("surface") == surface]


def _workspace_image() -> str:
    """The image the Code surface is ACTUALLY running. An agent runs it byte-for-byte."""
    image = _kubectl(
        "get", "pod", "-l", "app.kubernetes.io/component=workspace",
        "-o", 'jsonpath={.items[0].spec.containers[?(@.name=="ttyd")].image}',
    ).strip()
    assert image, "no running workspace pod to read the agent image from"
    return image


def _provision(name: str, *extra: str, env: dict | None = None,
               check=False) -> subprocess.CompletedProcess:
    argv = ["env", f"AGENT_IMAGE={_workspace_image()}"]
    for k, v in (env or {}).items():
        argv.append(f"{k}={v}")
    argv += ["deploy/bin/provision-agent.sh", USER, name, *extra]
    return _run(*argv, check=check, timeout=1500, cwd=str(ROOT))


def _pod_json(name: str) -> dict:
    out = _kubectl(
        "get", "pod", "-l",
        f"agent.enterprise-ai/user={USER},agent.enterprise-ai/name={name}",
        "-o", "json",
    )
    items = [p for p in json.loads(out)["items"]
             if (p.get("metadata") or {}).get("deletionTimestamp") is None]
    assert items, f"no pod for agent-{USER}-{name}"
    return items[0]


def _wait_ready(name: str, timeout_s=READY_TIMEOUT_S) -> str:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            pod = _pod_json(name)
        except AssertionError:
            time.sleep(3)
            continue
        if pod["status"].get("phase") == "Running" and all(
            c.get("ready") for c in pod["status"].get("containerStatuses", [])
        ):
            return pod["metadata"]["name"]
        time.sleep(3)
    raise AssertionError(f"agent-{USER}-{name} never became Ready within {timeout_s}s")


def _exec(name: str, script: str, check=False) -> subprocess.CompletedProcess:
    return _run("kubectl", "-n", NS, "exec", _pod_json(name)["metadata"]["name"], "--",
                "bash", "-c", script, check=check, timeout=300)


def resident(name: str = NAME) -> tuple[str, str, int]:
    """The resident daemon's identity, read off the pod: (pid, start time, restarts).

    THE ground truth for "attach, not spawn". Read with kubectl, so nothing the console
    returns can satisfy it.
    """
    pid = _exec(name, "pgrep -f 'opencode serve' | head -1").stdout.strip()
    started = _exec(
        name, "ps -o lstart= -p $(pgrep -f 'opencode serve' | head -1)").stdout.strip()
    restarts = _pod_json(name)["status"]["containerStatuses"][0]["restartCount"]
    return pid, started, restarts


def _call_model_from_pod(name: str, credential: str = "$OPENAI_API_KEY") -> str:
    """The agent's OWN model call, from inside the agent's OWN pod.

    The base URL comes from the pod's environment — the value opencode itself uses — so
    this measures the configuration the provisioner installed rather than one chosen here.
    The credential never leaves the pod; only the HTTP status comes back.
    """
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
    })
    proc = _exec(name, (
        "curl -sS -m 120 -o /tmp/resp.json -w '%{http_code}' "
        f'-H "Authorization: Bearer {credential}" '
        "-H 'Content-Type: application/json' "
        f"-d '{body}' \"$OPENAI_API_BASE/chat/completions\""
    ))
    return proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "000"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _listening(port: int, host="127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _api_server() -> tuple[str, str]:
    """The API server's host and port, out of the operator's kubeconfig."""
    server = _run("kubectl", "config", "view", "--minify", "-o",
                  "jsonpath={.clusters[0].cluster.server}", timeout=120).stdout.strip()
    host, _, port = server.rsplit("//", 1)[1].partition(":")
    return host, (port or "443")


def _lan_address() -> str:
    """This host's address ON THE ROUTE TO THE CLUSTER, i.e. how a node would dial back.

    Derived rather than hard-coded, and derived towards the API server specifically: the
    fixture has to sit at an address the cluster could route to, or
    `test_the_agent_pod_cannot_reach_the_fixture_because_the_policy_forbids_it` would be
    measuring an unroutable address instead of the NetworkPolicy.
    """
    host, port = _api_server()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((host, int(port)))
        return s.getsockname()[0]


@contextlib.contextmanager
def _hold_ipv6_loopback(port: int):
    """Hold `::1:<port>` as well as `127.0.0.1:<port>`, relaying one to the other.

    NOT a nicety — without it this file silently tests the WRONG CONTROL PLANE, and it
    did once before this was added. `provision-agent.sh` reaches its control plane at
    `http://localhost:18091`, `localhost` resolves to `::1` FIRST on this host, and
    uvicorn binds a single address. So the app would hold 127.0.0.1:18091, the script's
    own `kubectl port-forward` would still get `::1:18091` (it warns and carries on when
    only one of the two binds), and `curl` would follow ::1 straight to the DEPLOYED
    pre-agents control plane — which answers `unknown surface: agents/<name>`. The
    symptom looks like a broken feature and is a broken test.

    The relay dials the app over 127.0.0.1, so `require_user` still sees a loopback peer
    and the shipped identity predicate is not weakened to make this work.
    """
    srv = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("::1", port))
    srv.listen(64)

    def pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                with contextlib.suppress(OSError):
                    s.close()

    def accept_loop() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            try:
                up = socket.create_connection(("127.0.0.1", port), timeout=30)
            except OSError:
                conn.close()
                continue
            for a, b in ((conn, up), (up, conn)):
                threading.Thread(target=pump, args=(a, b), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()
    try:
        yield
    finally:
        srv.close()


@contextlib.contextmanager
def _background(*args, ready, what: str, timeout=90):
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True)
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                raise AssertionError(f"{what} exited: {proc.stderr.read()[:500]}")
            if ready():
                break
            time.sleep(0.3)
        else:
            raise AssertionError(f"{what} was not usable within {timeout}s")
        yield proc
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)


# ---------------------------------------------------------------- teardown


def _teardown_agents() -> None:
    """Remove every object this file creates. The camp runs here; leave nothing behind."""
    for obj in (OBJ, BYO_OBJ):
        _kubectl("delete", "deployment", obj, "--ignore-not-found", "--wait=true",
                 check=False, timeout=300)
        _kubectl("delete", "service", obj, "--ignore-not-found", check=False)
        for suffix in ("key", "byo", "email"):
            _kubectl("delete", "secret", f"{obj}-{suffix}", "--ignore-not-found",
                     check=False)
        _kubectl("delete", "pvc", obj, "--ignore-not-found", "--wait=true",
                 check=False, timeout=300)

    # The virtual keys, at the gateway AND in the control plane's own table, plus the
    # usage rows. A deleted Deployment does not stop its key spending money, and a usage
    # row left behind would be a permanent fixture in an operator's view.
    _run("kubectl", "-n", NS, "exec", "deploy/control-plane", "-c", "control-plane", "--",
         "python3", "-c",
         "import os,asyncio,httpx,asyncpg\n"
         "async def go():\n"
         "    h={'Authorization':'Bearer '+os.environ['GATEWAY_MASTER_KEY']}\n"
         "    httpx.post('http://gateway:4000/key/delete',headers=h,"
         f"json={{'key_aliases':['{ALIAS}','{BYO_ALIAS}']}},timeout=60)\n"
         "    c=await asyncpg.connect(os.environ['CONTROL_PLANE_DATABASE_URL'])\n"
         "    await c.execute('DELETE FROM virtual_key WHERE key_alias = ANY($1)',"
         f"['{ALIAS}','{BYO_ALIAS}'])\n"
         "    await c.execute('DELETE FROM agent_usage WHERE agent_user=$1 AND "
         f"agent_name = ANY($2)','{USER}',['{NAME}','{BYO}'])\n"
         "    await c.close()\n"
         "asyncio.run(go())",
         check=False, timeout=300)

    deadline = time.time() + 300
    while time.time() < deadline:
        remaining = [o for o in (OBJ, BYO_OBJ)
                     if _kubectl("get", "pvc", o, "--ignore-not-found", "-o", "name",
                                 check=False).strip()]
        if not remaining:
            return
        time.sleep(3)
    raise AssertionError(f"PVCs still present 300s after delete: {remaining}")


# ---------------------------------------------------------------- the live world


class Live:
    """The one live world this file drives: a real pod, real databases, the new app."""

    def __init__(self, base: str, mail: "Mail"):
        self.base = base
        self.mail = mail
        self.admin_token = _secret_value("enterprise-ai-secrets",
                                         "CONTROL_PLANE_ADMIN_TOKEN")

    # -- the portal, as a signed-in browser -------------------------------------
    def client(self, user: str = USER):
        """One signed-in browser. Identity is the header oauth2-proxy sets, honoured only
        because this connection comes from 127.0.0.1 — the shipped predicate, unmodified."""
        import httpx
        return httpx.Client(base_url=self.base, timeout=60.0,
                            headers={"X-Auth-Request-Preferred-Username": user})

    def admin(self, path: str, method: str = "get") -> dict:
        import httpx
        r = getattr(httpx, method)(
            f"{self.base}{path}",
            headers={"Authorization": f"Bearer {self.admin_token}"}, timeout=180.0)
        r.raise_for_status()
        return r.json()

    def direct(self, path: str, **kw):
        """The agent pod itself, bypassing the portal. For measuring the daemon."""
        import httpx
        return httpx.get(f"http://127.0.0.1:{SERVE_PORT}{path}", timeout=20.0, **kw)

    def collect(self) -> dict:
        """Force one usage sample. The SAME `collect_once` the timer runs."""
        return self.admin("/admin/agents/usage/collect", method="post")

    def usage_row(self, agent: str = NAME) -> dict:
        rows = self.admin(f"/admin/agents/usage?username={USER}")["agents"]
        mine = [r for r in rows if r["agent"] == agent]
        assert mine, f"no usage row for {USER}/{agent}: {rows}"
        return mine[0]


class Mail:
    """A throwaway real SMTP+IMAP server, owned and torn down by this file.

    Deliberately GreenMail (Apache-2.0) in a container speaking real protocols on real
    sockets, and deliberately NOT a stub: -a4e's whole claim is that `agent-email` holds a
    real conversation with a real mail host, and a fake mailer would assert only that this
    file can call a function.
    """

    def __init__(self, host: str, smtp: int, imap: int, container: str):
        self.host = host
        self.smtp = smtp
        self.imap = imap
        self.container = container

    def config_lines(self) -> str:
        """The `KEY=value` file provision-agent.sh takes, naming this fixture."""
        return (
            f"AGENT_EMAIL_ADDRESS={MAIL_USER}\n"
            f"AGENT_EMAIL_USERNAME={MAIL_USER}\n"
            f"AGENT_EMAIL_PASSWORD={MAIL_PASSWORD}\n"
            f"AGENT_EMAIL_SMTP_HOST={self.host}\n"
            f"AGENT_EMAIL_SMTP_PORT={self.smtp}\n"
            f"AGENT_EMAIL_SMTP_SECURITY=none\n"
            f"AGENT_EMAIL_IMAP_HOST={self.host}\n"
            f"AGENT_EMAIL_IMAP_PORT={self.imap}\n"
            f"AGENT_EMAIL_IMAP_SECURITY=none\n"
        )

    def fetch(self, login: str, password: str, subject: str, timeout=90) -> bytes | None:
        """Retrieve a message from the fixture with this file's OWN IMAP client.

        The expected value comes out of the mailbox, never out of `agent-email`.
        """
        import imaplib
        deadline = time.time() + timeout
        while time.time() < deadline:
            conn = imaplib.IMAP4(self.host, self.imap, timeout=30)
            try:
                conn.login(login, password)
                conn.select("INBOX")
                typ, data = conn.search(None, "ALL")
                if typ == "OK":
                    for uid in reversed((data[0] or b"").split()):
                        t, d = conn.fetch(uid, "(RFC822)")
                        if t != "OK" or not d or not isinstance(d[0], tuple):
                            continue
                        raw = d[0][1]
                        if subject.encode() in raw:
                            return raw
            finally:
                with contextlib.suppress(Exception):
                    conn.logout()
            time.sleep(2)
        return None

    def inject(self, sender: str, recipient: str, subject: str, body: str) -> None:
        """Put a message in a mailbox over a REAL SMTP transaction from this file.

        The injection side of the READ proof: `agent-email read` must find a message that
        arrived the way mail arrives, not one written into a store behind the protocol.
        """
        import smtplib
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = recipient
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.smtp, timeout=30) as conn:
            conn.send_message(msg)


def _start_greenmail(host: str) -> Mail:
    if _run("docker", "version", check=False, timeout=60).returncode != 0:
        pytest.fail("docker is required: the mailbox leg proves SMTP and IMAP against a "
                    "real mail server, and there is no stub standing in for one.")
    smtp, imap = _free_port(), _free_port()
    name = f"ede-greenmail-{uuid.uuid4().hex[:8]}"
    opts = (
        "-Dgreenmail.setup.test.all "
        # Without this GreenMail binds 127.0.0.1 INSIDE the container and every published
        # port answers nothing.
        "-Dgreenmail.hostname=0.0.0.0 "
        f"-Dgreenmail.users={MAIL_USER}:{MAIL_PASSWORD},{MAIL_PEER}:{MAIL_PEER_PASSWORD} "
        "-Dgreenmail.verbose"
    )
    # Published on all interfaces, because the isolation assertion needs the fixture to be
    # at an address the cluster could route to — so that "the agent pod cannot reach it"
    # is a statement about the NetworkPolicy and not about the network.
    _run("docker", "run", "-d", "--rm", "--name", name,
         "-p", f"0.0.0.0:{smtp}:3025", "-p", f"0.0.0.0:{imap}:3143",
         "-e", f"GREENMAIL_OPTS={opts}", GREENMAIL_IMAGE, timeout=300)
    mail = Mail(host, smtp, imap, name)

    import imaplib
    import smtplib
    deadline = time.time() + 180
    last = ""
    while time.time() < deadline:
        try:
            with smtplib.SMTP(host, smtp, timeout=10) as c:
                c.noop()
            c = imaplib.IMAP4(host, imap, timeout=10)
            c.login(MAIL_USER, MAIL_PASSWORD)
            c.logout()
            return mail
        except Exception as exc:  # noqa: BLE001 - retried until the deadline
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(2)
    _run("docker", "rm", "-f", name, check=False, timeout=120)
    raise AssertionError(f"GreenMail never came up on {host}:{smtp}/{imap} — {last}")


def _serve_new_control_plane(sa_dir: Path, ports: dict):
    """The shipped app, in this process, wired to the live cluster through the ENVIRONMENT.

    Every knob below is one the app already reads from its environment because a pod sets
    it. Nothing here patches a function, and the only monkeypatch in the whole file is the
    agent Service's NAME resolution, which is cluster DNS and not code.
    """
    import uvicorn

    host, port = _api_server()

    def sec(key: str) -> str:
        return _secret_value("enterprise-ai-secrets", key)

    os.environ.update({
        # The in-cluster credential, read the way a pod reads a projected token.
        "KUBE_SA_DIR": str(sa_dir),
        "KUBERNETES_SERVICE_HOST": host,
        "KUBERNETES_SERVICE_PORT": port,
        # The live databases, the live gateway, the live IdP — addresses only.
        "CONTROL_PLANE_DATABASE_URL": sec("CONTROL_PLANE_DATABASE_URL").replace(
            "@postgres:5432", f"@127.0.0.1:{ports['postgres']}"),
        "GATEWAY_DATABASE_URL": sec("GATEWAY_DATABASE_URL").replace(
            "@postgres:5432", f"@127.0.0.1:{ports['postgres']}"),
        "GATEWAY_URL": f"http://127.0.0.1:{ports['gateway']}",
        "GATEWAY_MASTER_KEY": sec("GATEWAY_MASTER_KEY"),
        "CONTROL_PLANE_ADMIN_TOKEN": sec("CONTROL_PLANE_ADMIN_TOKEN"),
        "CHAT_MONGO_URL": f"mongodb://127.0.0.1:{ports['chatdb']}",
        "CHAT_MONGO_DB": "librechat",
        "IDP_URL": f"http://127.0.0.1:{ports['identity']}",
        "IDP_REALM": "enterprise-ai",
        "IDP_CLIENT_ID": "control-plane",
        "IDP_CLIENT_SECRET": sec("IDP_CLIENT_SECRET"),
        "PORTAL_ADMINS": "baron",
        "PUBLISHED_INTERNAL_URL": "http://127.0.0.1:1",
        "WORKSPACE_INTERNAL_TOKEN": sec("WORKSPACE_INTERNAL_TOKEN"),
        # Sampled often enough that a real tick lands inside the windows this file waits
        # out. The collector is the shipped one; only its period is configuration.
        "AGENT_USAGE_SAMPLE_SECONDS": "15",
    })

    # Imported AFTER the environment is set, because the modules read it at import the way
    # they do in a pod that was started with it.
    from app.main import app as shipped_app

    # And removed again immediately. `KUBERNETES_SERVICE_*` is how client-go decides it is
    # running INSIDE a cluster; leaving it set would change what `kubectl` does in every
    # subprocess this file spawns afterwards, including the provisioner. The app has
    # already resolved them into `agent_usage.KUBE_API`, which is what a pod would hold.
    for leaked in ("KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT"):
        os.environ.pop(leaked, None)

    from app import agent_usage as _usage
    assert _usage.KUBE_API == f"https://{host}:{port}", _usage.KUBE_API
    assert _usage.enabled(), (
        f"the app cannot meter: no service-account token at {_usage.TOKEN_FILE}")

    # Cluster DNS, and only that. `agent-<user>-<name>` resolves inside the namespace and
    # not on this host; the port-forward puts the pod on the Service's OWN port at
    # 127.0.0.1, so the shipped code dials exactly what it dials in the cluster. A name it
    # derived WRONGLY is not in this map and does not resolve.
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(hostname, port_, *args, **kwargs):
        key = hostname.decode() if isinstance(hostname, (bytes, bytearray)) else hostname
        if key in (OBJ, BYO_OBJ):
            return real_getaddrinfo("127.0.0.1", port_, *args, **kwargs)
        return real_getaddrinfo(hostname, port_, *args, **kwargs)

    socket.getaddrinfo = fake_getaddrinfo

    server = uvicorn.Server(uvicorn.Config(shipped_app, host="127.0.0.1", port=CP_PORT,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 60
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "the new control plane never came up on loopback"
    try:
        yield f"http://127.0.0.1:{CP_PORT}"
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        socket.getaddrinfo = real_getaddrinfo


@pytest.fixture(scope="module")
def live():
    """The whole live world: RBAC, credentials, forwards, the new app, a real agent, mail.

    One fixture rather than six because this file's subject is the composition — a test
    that could pass with half of it missing would not be the gate this item asks for.
    """
    _teardown_agents()
    sa_dir = Path(tempfile.mkdtemp(prefix="ede-sa-"))
    stack = contextlib.ExitStack()
    applied_rbac = False
    mail = None
    try:
        # The epic's OWN RBAC, so the app below holds the authority the shipped Deployment
        # is meant to hold — and no more. Additive; removed in teardown.
        _run("kubectl", "apply", "-f", str(RBAC_MANIFEST), timeout=180)
        applied_rbac = True
        (sa_dir / "token").write_text(_run(
            "kubectl", "create", "token", "control-plane", "-n", NS,
            "--duration=24h", timeout=120).stdout.strip())
        (sa_dir / "namespace").write_text(NS)
        (sa_dir / "ca.crt").write_text(base64.b64decode(_run(
            "kubectl", "config", "view", "--raw", "-o",
            "jsonpath={.clusters[0].cluster.certificate-authority-data}",
            timeout=120).stdout.strip()).decode())
        sa_dir.chmod(0o700)

        ports = {name: _free_port() for name in
                 ("postgres", "gateway", "chatdb", "identity")}
        for name, target in (("postgres", "pod/postgres-0"),
                             ("gateway", "svc/gateway"),
                             ("chatdb", "pod/chatdb-0"),
                             ("identity", "svc/identity")):
            remote = {"postgres": 5432, "gateway": 4000,
                      "chatdb": 27017, "identity": 8080}[name]
            stack.enter_context(_background(
                "kubectl", "-n", NS, "port-forward", target,
                f"{ports[name]}:{remote}",
                ready=lambda p=ports[name]: _listening(p),
                what=f"port-forward {target}"))

        base = stack.enter_context(
            contextlib.contextmanager(_serve_new_control_plane)(sa_dir, ports))
        stack.enter_context(_hold_ipv6_loopback(CP_PORT))

        # CHECKED BEFORE ANYTHING DEPENDS ON IT: `http://localhost:18091` — the exact URL
        # provision-agent.sh builds, resolved the way its `curl` resolves it — must reach
        # the NEW app. If it reaches the deployed one instead, every mint below goes to an
        # image that predates this epic and the failure looks like a broken feature.
        import httpx
        probe = httpx.get(f"http://localhost:{CP_PORT}/openapi.json", timeout=60).json()
        assert "/admin/agents/usage" in probe["paths"], (
            f"http://localhost:{CP_PORT} is NOT the app under test — the provisioner "
            "would mint against the deployed pre-agents control plane. Check that "
            "nothing else holds that port on either loopback address.")

        # THE AGENT. The real provisioner, integrated mode, minting through the NEW
        # control plane now listening on the port the script reaches for.
        proc = _provision(NAME, "--model", MODEL)
        assert proc.returncode == 0, (
            f"provision-agent.sh failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
        assert "(minted)" in proc.stdout, (
            f"the provisioner did not mint a virtual key:\n{proc.stdout}")
        _wait_ready(NAME)

        stack.enter_context(_background(
            "kubectl", "-n", NS, "port-forward", f"svc/{OBJ}",
            f"{SERVE_PORT}:{SERVE_PORT}",
            ready=lambda: _listening(SERVE_PORT), what="port-forward agent"))

        mail = _start_greenmail(_lan_address())
        yield Live(base, mail)
    finally:
        stack.close()
        if mail is not None:
            _run("docker", "rm", "-f", mail.container, check=False, timeout=180)
        _teardown_agents()
        if applied_rbac:
            _run("kubectl", "delete", "-f", str(RBAC_MANIFEST), "--ignore-not-found",
                 check=False, timeout=300)
        with contextlib.suppress(OSError):
            for f in sa_dir.iterdir():
                f.unlink()
            sa_dir.rmdir()


@pytest.fixture(scope="module")
def bill_before(live) -> list[dict]:
    """The whole bill before this file spends anything, from the DEPLOYED reader."""
    return _deployed_spend_rows()


# ================================================================ 0. the substitution


def test_the_control_plane_under_test_is_the_new_one_on_18091(live):
    """Checked, not assumed: the port `provision-agent.sh` reaches is answered by the NEW app.

    The whole method of this file rests on it. If the deployed control plane's own
    port-forward had won that port, every mint and every usage read below would have gone
    to an image that predates this epic — and the failure would have looked like a broken
    feature rather than a broken test. So the two are told apart by a route that only
    exists in the new app, and the OLD one is confirmed to still be old and still running.
    """
    import httpx
    paths = httpx.get(f"{live.base}/openapi.json", timeout=60).json()["paths"]
    for route in ("/portal/api/agents", "/admin/agents/usage",
                  "/admin/agents/usage/collect"):
        assert route in paths, (
            f"{route} is missing from the app answering on {CP_PORT} — this is not the "
            f"app under test. Routes seen: {sorted(paths)[:40]}")
    # The console route is `include_in_schema=False`, so it is confirmed by reaching it
    # rather than by reading the schema: an unauthenticated request must be refused by
    # the portal's own predicate, which a 404 from some other app would not produce.
    assert httpx.get(f"{live.base}/agents/{NAME}/app", timeout=60).status_code == 401

    deployed = json.loads(_in_control_plane(
        "import httpx,json;"
        "print(json.dumps(sorted(httpx.get('http://127.0.0.1:8000/openapi.json',"
        "timeout=60).json()['paths'])))").strip().splitlines()[-1])
    assert "/admin/agents/usage" not in deployed, (
        "the DEPLOYED control plane already serves the agents surface. This file's whole "
        "premise — that it must not be rolled — needs rechecking, and so does gate -6f3.")
    assert _kubectl("get", "pod", "-l", "app=control-plane",
                    "-o", "jsonpath={.items[0].status.phase}").strip() == "Running"


# ================================================================ 1. resident (-055)


def test_the_agent_is_a_resident_daemon_and_not_something_a_console_spawns(live):
    """Contract 2's precondition, and the two facts every later claim is measured against.

    Resident: `opencode serve` is a direct child of PID 1, so it is what the container
    runs. Guarded: the same port answers 401 without a credential — without which a 200
    through the console would prove nothing about the console.
    """
    ps = _exec(NAME, "ps -eo pid,ppid,args").stdout
    serve = [line for line in ps.splitlines() if "opencode serve" in line]
    assert len(serve) == 1, f"expected exactly one resident opencode serve:\n{ps}"
    assert int(serve[0].split()[1]) == 1, f"opencode serve is not a child of PID 1:\n{ps}"
    assert "ttyd" not in ps, (
        "ttyd is in an agent pod — that is the Code surface's spawn-per-websocket model "
        f"(finding 43), which is exactly what an Agent must not be:\n{ps}")
    assert live.direct("/app").status_code == 401, (
        "the agent's daemon answered an unauthenticated request")


# ================================================================ 2. console (-0e7)


def test_the_owner_attaches_the_console_to_that_same_resident_daemon(live):
    """`/agents/<name>/` returns the RESIDENT daemon's console, and attaching does not move it.

    The composition claim: the pod -055 provisioned is the pod -0e7's route reaches, under
    the identity -627 owns it by. The proof that it ATTACHED rather than spawned is that
    the pid and restart count read off the pod with kubectl are unchanged across the whole
    exchange.
    """
    before = resident()
    assert before[0], "no resident opencode serve process before attaching"

    with live.client() as owner:
        page = owner.get(f"/agents/{NAME}/app")
        assert page.status_code == 200, page.text[:400]
        assert "<title>OpenCode</title>" in page.text, page.text[:400]
        assert f'"/agents/{NAME}"' in page.text and "window.fetch=function" in page.text, (
            "the prefix shim is missing, so the console would resolve its server as the "
            "origin root and leave its own prefix")

        # An asset the daemon really references, fetched back through the console and
        # compared byte-for-byte with the pod's own copy. The expected bytes come from
        # the pod, not from this file.
        import re
        password = _secret_value(f"{OBJ}-key", "OPENCODE_SERVER_PASSWORD")
        assert password, "no OPENCODE_SERVER_PASSWORD in the agent's Secret"
        raw = live.direct("/app", auth=("opencode", password))
        assert raw.status_code == 200
        assets = sorted(set(re.findall(r'(?:src|href)="(/[^"/][^"]*)"', raw.text)))
        assert assets, f"the daemon's entry document has no root-absolute assets:\n{raw.text[:600]}"
        one = assets[0]
        through = owner.get(f"/agents/{NAME}{one}")
        assert through.status_code == 200, through.text[:300]
        assert through.content == live.direct(one, auth=("opencode", password)).content, (
            f"{one} came back different through the console than from the pod")
        assert password not in page.text, "the daemon's credential reached the browser"

    assert resident() == before, (
        f"attaching a console moved the resident daemon: {before} -> {resident()}")


def test_an_unauthenticated_browser_is_refused_by_the_portal(live):
    """The console is not exempt from portal.py's identity rule."""
    import httpx
    with httpx.Client(base_url=live.base, timeout=30.0) as anonymous:
        assert anonymous.get(f"/agents/{NAME}/app").status_code == 401


def test_the_owner_sees_this_agent_in_their_own_portal_listing(live):
    """-627 composed with -055: the agent the script made is the agent the portal owns."""
    with live.client() as owner:
        listing = owner.get("/portal/api/agents")
    assert listing.status_code == 200, listing.text[:300]
    agents = listing.json()["agents"]
    mine = [a for a in agents if a["name"] == NAME]
    assert mine, f"{USER}'s own portal does not list the agent they own: {agents}"
    assert mine[0]["status"] == "running", mine[0]
    assert mine[0].get("console_url") == f"/agents/{NAME}/", mine[0]


# ================================================================ 3. the one bill (-39d)


def test_a_real_inference_call_through_the_gateway_lands_on_the_one_bill(live, bill_before):
    """The money, end to end: the pod's own key, the real gateway, the DEPLOYED bill reader.

    The row is written by LiteLLM into its own Postgres, attributed by the alias
    `baron::agents/e2eede`, and read back by a control-plane image that PREDATES this
    epic. That last part is the composition result worth stating: the pre-agents bill
    reader renders the agents surface with no change at all, because Contract 1 put the
    instance in the alias rather than in a new field.
    """
    before = _deployed_rows_for(SURFACE)
    before_requests = sum(r.get("requests") or 0 for r in before)

    status = _call_model_from_pod(NAME)
    assert status == "200", (
        f"the agent's own model call through the gateway returned {status}, not 200. "
        f"pod env: {_exec(NAME, 'echo $OPENAI_API_BASE').stdout.strip()}")

    deadline = time.time() + LEDGER_TIMEOUT_S
    rows = []
    while time.time() < deadline:
        rows = _deployed_rows_for(SURFACE)
        if sum(r.get("requests") or 0 for r in rows) > before_requests:
            break
        time.sleep(5)
    else:
        pytest.fail(
            f"no new ledger row for {USER} / {SURFACE} within {LEDGER_TIMEOUT_S}s. The "
            "call returned 200, so the money was spent and the bill cannot see it — "
            f"which is finding 4's shape. Rows now: {rows}")

    assert sum(r.get("prompt_tokens") or 0 for r in rows) > 0, rows
    assert all((r.get("spend") or 0) >= 0 for r in rows), rows


# ================================================================ 4. the second dimension (-914)


def test_resident_time_and_compute_are_metered_for_this_agent(live):
    """Contract 3(b), composed onto the same agent: quantities, measured, non-zero.

    Both numbers are bounded against ground truth taken elsewhere — resident time against
    the pod's own `.status.startTime` read with kubectl, compute against an INDEPENDENT
    scrape of the same cAdvisor endpoint through the API server's `nodes/proxy` with the
    operator's kubeconfig. That is a different identity on a different route to the same
    counter, which is what makes it a check rather than a second call.
    """
    from datetime import datetime, timezone

    live.collect()
    time.sleep(16)
    sample = live.collect()
    assert sample["enabled"] is True, sample
    assert sample["running"] >= 1, sample

    row = live.usage_row()
    assert row["resident_seconds"] > 0, row
    assert row["cpu_core_seconds"] > 0, row
    assert row["compute_measured"] is True, row
    assert row["compute_source"] == "kubelet/cadvisor", row
    assert row["running"] is True, row
    assert row["surface"] == SURFACE, (
        f"the usage row's surface is {row['surface']}, which will not line up against the "
        "spend row for the same agent")
    assert row["model_source"] == "integrated", row

    started = datetime.fromisoformat(
        _pod_json(NAME)["status"]["startTime"].replace("Z", "+00:00"))
    wall = (datetime.now(timezone.utc) - started).total_seconds()
    assert row["resident_seconds"] <= wall + 5, (
        f"the meter claims {row['resident_seconds']}s resident for a pod that has existed "
        f"for {wall:.0f}s")

    node = _pod_json(NAME)["status"]["hostIP"]
    node_name = _kubectl("get", "node", "-o",
                         f"jsonpath={{.items[?(@.status.addresses[0].address=='{node}')]"
                         ".metadata.name}").strip()
    scrape = _run("kubectl", "get", "--raw",
                  f"/api/v1/nodes/{node_name}/proxy/metrics/cadvisor",
                  timeout=300).stdout
    pod_name = _pod_json(NAME)["metadata"]["name"]
    independent = 0.0
    for line in scrape.splitlines():
        if line.startswith("container_cpu_usage_seconds_total") and pod_name in line \
                and 'container=""' not in line and 'container="POD"' not in line:
            independent += float(line.rsplit(" ", 1)[-1])
    assert independent > 0, (
        f"an independent cAdvisor scrape sees no CPU for {pod_name}; the ledger's "
        f"{row['cpu_core_seconds']} could not have come from this counter")
    assert row["cpu_core_seconds"] <= independent + 1.0, (
        f"the ledger records {row['cpu_core_seconds']} CPU-core-seconds where the "
        f"independent scrape of the same counter reads {independent}")


def test_the_usage_ledger_carries_no_money_and_the_portal_shows_both_dimensions(live):
    """The composition the operator and the user actually see: usage BESIDE spend.

    Baron's ruling is that owned hardware is metered as usage, not priced — so the check
    is both that the quantities are there and that no dollar figure is anywhere near them.
    Then `/portal/api/spend` is read as the signed-in owner, and the SAME agent must carry
    its resident/compute quantities next to the inference spend proven above. The two
    numbers come from two different databases and are joined in the endpoint; this is the
    only place in the file where both are read at once.
    """
    payload = live.admin(f"/admin/agents/usage?username={USER}")

    # Over the KEYS, not over the serialised blob. A substring scan of the whole document
    # reports `rate` inside `integrated` and `cost` inside any word containing it, and a
    # money check that cries wolf is one somebody deletes. What Baron's ruling actually
    # forbids is a priced FIELD, so the fields are what is examined.
    def keys_of(node) -> list[str]:
        if isinstance(node, dict):
            return [k for k in node] + [x for v in node.values() for x in keys_of(v)]
        if isinstance(node, list):
            return [x for v in node for x in keys_of(v)]
        return []

    for key in keys_of(payload):
        for money in ("cost", "usd", "dollar", "price", "rate", "spend", "currency",
                      "amount", "budget"):
            assert money not in key.lower(), (
                f"the USAGE payload carries a field '{key}', which is money. This ledger "
                "meters quantities only — the hardware is owned and its cost is sunk, so "
                "a figure here would be invented.")
    assert "$" not in json.dumps(payload), (
        f"a currency symbol reached the usage payload: {json.dumps(payload)[:400]}")

    with live.client() as owner:
        spend = owner.get("/portal/api/spend")
    assert spend.status_code == 200, spend.text[:400]
    body = spend.json()
    assert "agents_usage_error" not in body, body["agents_usage_error"]

    mine = [a for a in body["by_agent"] if a["agent"] == NAME]
    assert mine, f"the owner's own page does not carry this agent: {body['by_agent']}"
    agent = mine[0]
    assert agent["usage"]["resident_seconds"] > 0, agent
    assert agent["usage"]["cpu_core_seconds"] > 0, agent
    assert agent["usage"]["compute_measured"] is True, agent
    assert agent["inference"]["on_ledger"] is True, agent
    assert agent["inference"]["requests"] >= 1, (
        f"the agent's inference spend is not lined up beside its usage: {agent}")

    surfaces = {s["surface"] for s in body["by_surface"]}
    assert SURFACE in surfaces, (
        f"{SURFACE} is missing from the owner's own spend breakdown: {surfaces}")


# ================================================================ 5. residency (-055 + -0e7)


def test_closing_the_browser_leaves_the_same_resident_agent_running(live):
    """THE CLAIM THE WHOLE SURFACE EXISTS TO MAKE, measured on the composed agent.

    A session is created THROUGH the console, every connection is then closed — which is
    what closing the browser does — nothing touches the agent for the length of a window,
    and a second, independent client attaches. What must be unchanged is read off the pod:
    the resident pid, its start time, and the container's restart count. What must have
    survived is read through the console: the session created before the disconnect. And
    the meter must not have frozen — this agent kept accruing resident time while nobody
    was looking at it, which is the difference between an Agent and a workspace.

    On the Code surface the equivalent fails by construction: ttyd spawns an opencode per
    websocket and it dies with the connection (finding 43). That is the comparison.
    """
    before = resident()
    assert before[0], "no resident opencode serve process before attaching"
    live.collect()
    usage_before = live.usage_row()["resident_seconds"]

    first = live.client()
    assert first.get(f"/agents/{NAME}/app").status_code == 200
    created = first.post(f"/agents/{NAME}/session", json={})
    assert created.status_code in (200, 201), f"{created.status_code}: {created.text[:300]}"
    session_id = created.json()["id"]
    listed = first.get(f"/agents/{NAME}/session")
    assert session_id in [s["id"] for s in listed.json()], listed.text[:300]

    # THE DISCONNECT. Every socket this client held is closed.
    first.close()
    time.sleep(DISCONNECT_WINDOW_S)

    during = resident()
    assert during[:2] == before[:2], (
        f"the resident daemon changed with nothing attached: {before} -> {during}. The "
        "agent did not survive the console closing, which is the entire difference "
        "between an Agent and the Code surface.")

    # THE RECONNECT.
    second = live.client()
    after = resident()
    assert after[0] == before[0], (
        f"reconnecting reached a different process ({before[0]} -> {after[0]}): the "
        "console SPAWNED an agent instead of attaching to the resident one.")
    assert after[1] == before[1], f"process start time moved: {before[1]} -> {after[1]}"
    assert after[2] == before[2], f"the container restarted: {before} -> {after}"

    survived = second.get(f"/agents/{NAME}/session")
    assert survived.status_code == 200, survived.text[:300]
    assert session_id in [s["id"] for s in survived.json()], (
        f"the session created before the disconnect is gone after reconnecting: "
        f"{survived.text[:400]}")
    second.close()

    live.collect()
    usage_after = live.usage_row()["resident_seconds"]
    assert usage_after > usage_before, (
        f"resident time did not accrue across the disconnect window "
        f"({usage_before} -> {usage_after}) — the meter is measuring attention, not residency")


# ================================================================ 6. the mailbox (-a4e)


@pytest.fixture(scope="module")
def mailbox(live):
    """Configure the mailbox on the LIVE agent with the real provisioner.

    Deliberately a SECOND provisioning run rather than part of the first: it exercises
    -a4e's own claim that adding a mailbox to an existing agent keeps its key
    (`kept`, no rotation) while rolling the pod for the new `checksum/email`. It runs
    after the residency test above for the obvious reason — it restarts the pod.
    """
    cfg = Path(tempfile.mkdtemp(prefix="ede-mail-")) / "mail.env"
    cfg.write_text(live.mail.config_lines())
    cfg.chmod(0o600)
    try:
        proc = _provision(NAME, "--model", MODEL, "--email-config-file", str(cfg))
        assert proc.returncode == 0, (
            f"provisioning the mailbox failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
        assert "(kept;" in proc.stdout, (
            f"adding a mailbox rotated the agent's virtual key:\n{proc.stdout}")
        assert MAIL_PASSWORD not in proc.stdout + proc.stderr, (
            "the mailbox password was printed by the provisioner")
        # The pod rolls on the new checksum/email; wait for the new one to be Ready.
        _kubectl("rollout", "status", f"deployment/{OBJ}", "--timeout=480s", timeout=540)
        _wait_ready(NAME)
        yield live.mail
    finally:
        with contextlib.suppress(OSError):
            cfg.unlink()
            cfg.parent.rmdir()


def test_configuring_email_puts_a_real_mailbox_on_the_live_agent(live, mailbox):
    """The configuration, as the RUNNING pod actually holds it.

    Read out of the pod's own environment and off its own filesystem — not out of the
    Secret this file wrote, and not out of the provisioner's output.
    """
    env = _exec(NAME, "env | grep '^AGENT_EMAIL_' | sort").stdout
    assert f"AGENT_EMAIL_ADDRESS={MAIL_USER}" in env, env
    assert f"AGENT_EMAIL_SMTP_HOST={mailbox.host}" in env, env
    assert f"AGENT_EMAIL_SMTP_PORT={mailbox.smtp}" in env, env
    assert f"AGENT_EMAIL_IMAP_PORT={mailbox.imap}" in env, env
    assert "AGENT_EMAIL_CONFIG_SUM=" in env, env

    # The tool itself: delivered by the ConfigMap, executable, and this checkout's bytes.
    # `test -x` rather than parsing `ls`, because a ConfigMap volume presents every key as
    # a SYMLINK into its own `..data/` snapshot directory — the mode bits on the link say
    # `lrwxrwxrwx` and mean nothing. What matters is whether the pod can execute the path.
    probe = _exec(NAME, f"test -x {AGENT_EMAIL_PATH} && echo EXECUTABLE || echo NO").stdout
    assert "EXECUTABLE" in probe, (
        f"agent-email is not executable at {AGENT_EMAIL_PATH}: "
        f"{_exec(NAME, f'ls -lL {AGENT_EMAIL_PATH}').stdout.strip()}")
    in_pod = _exec(NAME, f"sha256sum {AGENT_EMAIL_PATH}", check=True).stdout.split()[0]
    on_disk = _run("sha256sum", str(ROOT / "deploy/agent/agent-email")).stdout.split()[0]
    assert in_pod == on_disk, (
        "the agent is running a different agent-email from this checkout's")

    # ON PATH FOR THE PROCESS THAT MATTERS, read out of that process rather than out of a
    # shell this test started. `agent-email` is a tool for OPENCODE — the resident daemon
    # runs the shell tool, and it is a child of the entrypoint that exports the PATH. A
    # `kubectl exec` shell is NOT such a child and does NOT get it (finding recorded in
    # docs/design/dogfood-findings.md); asserting on `command -v` from here would measure
    # the wrong process and call a working capability broken.
    pid = resident()[0]
    daemon_env = _exec(NAME, f"tr '\\0' '\\n' < /proc/{pid}/environ", check=True).stdout
    daemon_path = next((l for l in daemon_env.splitlines() if l.startswith("PATH=")), "")
    assert AGENT_EMAIL_DIR in daemon_path, (
        f"{AGENT_EMAIL_DIR} is not on the resident daemon's PATH, so opencode's shell "
        f"tool cannot call agent-email at all: {daemon_path}")
    assert "AGENT_EMAIL_ADDRESS=" in daemon_env, (
        "the resident daemon does not hold the mailbox configuration in its own "
        "environment — the pod was not rolled onto the new checksum/email")

    # The shipped tool's own view of its configuration, computed INSIDE the pod. It needs
    # no network, so it is the one mail command that can run there.
    cfg = json.loads(_exec(NAME, f"{AGENT_EMAIL_PATH} config", check=True).stdout)
    assert cfg["address"] == MAIL_USER, cfg
    assert cfg["smtp"]["host"] == mailbox.host, cfg
    assert MAIL_PASSWORD not in json.dumps(cfg), (
        "agent-email config printed the mailbox password")


def test_the_agent_pod_cannot_reach_the_fixture_because_the_policy_forbids_it(live, mailbox):
    """The isolation receipt, and the reason the send below runs where it runs.

    `63-agent-common.yaml` excludes every private range from an agent's egress. The
    fixture is on this host inside `192.168.0.0/16`, so an agent must not be able to reach
    it — while the gateway, which the policy names, must stay reachable. Both are measured
    from inside the same pod in the same breath, so "blocked" cannot be a fixture that is
    simply down, and the mailbox test that follows proves the fixture is up at the same
    moment. A real mailbox is an external provider on a public address, which this policy
    admits; a private-range one is exactly what it must refuse.
    """
    allowed = _exec(NAME, "timeout 6 bash -c 'exec 3<>/dev/tcp/gateway/4000' "
                          "&& echo REACHED || echo BLOCKED").stdout.strip()
    assert allowed.endswith("REACHED"), (
        f"the agent cannot reach the gateway, which its NetworkPolicy names: {allowed}")

    fixture = _exec(NAME, f"timeout 6 bash -c 'exec 3<>/dev/tcp/{mailbox.host}/{mailbox.smtp}' "
                          "&& echo REACHED || echo BLOCKED").stdout.strip()
    assert fixture.endswith("BLOCKED"), (
        f"the agent pod reached {mailbox.host}:{mailbox.smtp}, a private-range address. "
        "The egress allowlist in deploy/k8s/63-agent-common.yaml is not holding, and an "
        "unattended process with a spendable key can reach this LAN.")


def test_the_agents_own_mail_tool_sends_and_reads_real_mail(live, mailbox):
    """SEND and READ, over real SMTP and real IMAP, with the agent's own tool and config.

    The tool is the byte-identical copy taken out of the running pod, and the environment
    is the pod's own — both read back with kubectl in this test, not supplied by it. Only
    the process's LOCATION differs from the pod, for the reason the test above measures.

    The expected values come out of the fixture mailbox with this file's own IMAP and SMTP
    clients. Nothing stubs a mailer.
    """
    workdir = Path(tempfile.mkdtemp(prefix="ede-mailtool-"))
    try:
        tool = workdir / "agent-email"
        tool.write_bytes(base64.b64decode(_exec(
            NAME, f"base64 -w0 {AGENT_EMAIL_PATH}", check=True).stdout))
        tool.chmod(0o755)
        assert _run("sha256sum", str(tool)).stdout.split()[0] == \
            _run("sha256sum", str(ROOT / "deploy/agent/agent-email")).stdout.split()[0]

        # The pod's OWN environment, read off the pod.
        env = dict(os.environ)
        for line in _exec(NAME, "env | grep '^AGENT_EMAIL_'", check=True).stdout.splitlines():
            k, _, v = line.partition("=")
            env[k] = v
        assert env["AGENT_EMAIL_SMTP_HOST"] == mailbox.host

        # ---- SEND. A real SMTP submission, authenticated as the agent's mailbox.
        subject = f"ede-send-{uuid.uuid4().hex[:10]}"
        sent = _run(sys.executable, str(tool), "send", "--to", MAIL_PEER,
                    "--subject", subject, "--body", "composed on the live cluster",
                    env=env, check=False, timeout=180)
        assert sent.returncode == 0, f"{sent.stdout}\n{sent.stderr}"
        assert MAIL_PASSWORD not in sent.stdout + sent.stderr

        arrived = mailbox.fetch(MAIL_PEER, MAIL_PEER_PASSWORD, subject)
        assert arrived is not None, (
            f"'{subject}' never arrived in {MAIL_PEER}'s real mailbox")
        assert MAIL_USER.encode() in arrived, arrived[:400]

        # ---- READ. The inbound message is injected by this file's OWN SMTP transaction,
        # so what `agent-email read` finds got there the way mail gets there.
        inbound = f"ede-read-{uuid.uuid4().hex[:10]}"
        mailbox.inject(MAIL_PEER, MAIL_USER, inbound, "reply expected")
        assert mailbox.fetch(MAIL_USER, MAIL_PASSWORD, inbound) is not None, (
            "the injected message never landed in the agent's own mailbox")

        deadline = time.time() + 90
        found = None
        while time.time() < deadline and found is None:
            listed = _run(sys.executable, str(tool), "list", env=env, check=False,
                          timeout=180)
            assert listed.returncode == 0, f"{listed.stdout}\n{listed.stderr}"
            for msg in json.loads(listed.stdout):
                if msg.get("subject") == inbound:
                    found = msg
                    break
            if found is None:
                time.sleep(3)
        assert found is not None, (
            f"agent-email list never saw '{inbound}' in the agent's own mailbox: "
            f"{listed.stdout[:500]}")

        body = _run(sys.executable, str(tool), "read", "--uid", str(found["uid"]),
                    env=env, check=False, timeout=180)
        assert body.returncode == 0, f"{body.stdout}\n{body.stderr}"
        read = json.loads(body.stdout)
        assert read["subject"] == inbound, read
        assert "reply expected" in json.dumps(read), read
        assert MAIL_PASSWORD not in body.stdout + body.stderr
    finally:
        with contextlib.suppress(OSError):
            for f in workdir.iterdir():
                f.unlink()
            workdir.rmdir()


def test_the_mailbox_did_not_disturb_the_agents_key_or_its_ledger(live, mailbox):
    """Additivity of -a4e onto -39d: the mailbox is beside the model API, not instead of it."""
    assert _secret_value(f"{OBJ}-key", "OPENAI_API_KEY").startswith("sk-"), (
        "the agent's virtual key was replaced when the mailbox was configured")
    assert _call_model_from_pod(NAME) == "200", (
        "the agent stopped being able to spend after its mailbox was configured")
    assert _deployed_rows_for(SURFACE), "the agent's ledger row vanished"


# ================================================================ 7. BYO, off-ledger (-39d)


def _wait_gone(name: str, timeout_s=300) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        out = _kubectl("get", "pod", "-l",
                       f"agent.enterprise-ai/user={USER},agent.enterprise-ai/name={name}",
                       "-o", "name", check=False).strip()
        if not out:
            return
        time.sleep(3)
    raise AssertionError(f"agent-{USER}-{name}'s pod is still present after {timeout_s}s")


@pytest.fixture(scope="module")
def byo_agent(live, bill_before):
    """A second agent on the user's OWN credential, pointed away from our gateway.

    THE INTEGRATED AGENT IS STOPPED FIRST, and that is a measured property of this cluster
    rather than a convenience: `k3s-worker` is the only schedulable node (`k3s-cp` is
    tainted) and its CPU REQUESTS sit at ~95% with the camp's three workspaces, chat, the
    gateway and the rest of the row on it. A second agent does not fit — the scheduler
    says `Insufficient cpu` and the pod stays Pending forever.

    Stopping it is done through `/portal/api/agents/<name>/stop`, i.e. -627's own
    lifecycle path and Contract 2's scale-to-zero, so the workaround for a capacity limit
    is itself another leg of the surface being exercised rather than a `kubectl scale`
    that goes around the product.
    """
    keyfile = Path(tempfile.mkdtemp(prefix="ede-byo-")) / "key"
    keyfile.write_text("sk-the-users-own-provider-credential-ede\n")
    keyfile.chmod(0o600)
    try:
        with live.client() as owner:
            stopped = owner.post(f"/portal/api/agents/{NAME}/stop")
        assert stopped.status_code == 200, stopped.text[:300]
        _wait_gone(NAME)

        proc = _provision(BYO, "--byo-key-file", str(keyfile),
                          "--byo-api-base", BYO_BASE)
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        _wait_ready(BYO)
        yield
    finally:
        with contextlib.suppress(OSError):
            keyfile.unlink()
            keyfile.parent.rmdir()


def test_stopping_the_integrated_agent_freezes_its_usage_and_keeps_its_bill(live, byo_agent):
    """Contract 2's pause, as the composed surface reports it.

    Stopping is not deleting: the usage ledger keeps the quantities it accrued and stops
    calling the agent running, and the inference spend it already put on the one bill is
    untouched. This runs as a consequence of making room for the BYO agent, which is why
    it is here rather than in -914's file.
    """
    live.collect()
    row = live.usage_row()
    assert row["running"] is False, row
    assert row["pod_phase"] == "Absent", row
    assert row["resident_seconds"] > 0, (
        f"a stopped agent lost the resident time it had accrued: {row}")
    assert _deployed_rows_for(SURFACE), (
        "stopping an agent removed its spend from the one bill")


def test_a_byo_agent_is_declared_and_pointed_away_from_our_gateway(live, byo_agent):
    """Off-ledger by DECLARATION, which is the only basis on which Contract 4 permits it."""
    pod = _pod_json(BYO)
    assert pod["metadata"]["labels"]["agent.enterprise-ai/model-source"] == "byo", (
        "a BYO agent is not labelled as one — its $0 would be indistinguishable from an "
        "idle metered agent, which is finding 4's shape")
    assert _exec(BYO, "echo $OPENAI_API_BASE").stdout.strip() == BYO_BASE
    assert "gateway" not in _exec(BYO, "echo $OPENAI_API_BASE").stdout


def test_a_byo_call_leaves_the_pod_and_lands_no_gateway_ledger_row(live, byo_agent):
    """The negative, measured the only way a negative can be.

    A real call is made from inside the pod to a real non-gateway endpoint, something
    other than our gateway answers it, and then the gateway's own ledger is confirmed to
    have gained nothing at all for this agent.
    """
    status = _call_model_from_pod(BYO)
    assert status != "000", (
        f"the BYO agent's request never left the pod ({status}); a request that never "
        "happened would satisfy the zero-rows assertion below for the wrong reason")

    time.sleep(20)
    rows = _deployed_rows_for(BYO_SURFACE)
    assert rows == [], (
        f"a BYO agent produced gateway ledger rows: {rows}. Its inference is supposed to "
        "route to the user's own provider and never traverse this layer.")

    aliases = _gateway_aliases()
    # The positive control for the negative below. The integrated agent's key IS at the
    # gateway (stopping an agent does not revoke it), so seeing it proves this listing
    # actually reaches the keys it is being asked about — without which "the BYO alias is
    # absent" would be satisfied just as well by a broken or truncated list.
    assert ALIAS in aliases, (
        f"{ALIAS} is missing from the gateway's own key listing, so this listing cannot "
        f"be used to prove anything is absent from it ({len(aliases)} aliases seen)")
    assert BYO_ALIAS not in aliases, (
        f"a virtual key was minted for a BYO agent ({BYO_ALIAS}) — a spendable credential "
        "with nothing using it")


def test_the_byo_agent_is_still_metered_for_the_hardware_it_occupies(live, byo_agent):
    """The two dimensions are independent, and this is where that matters.

    Compute is consumed by the POD, so an agent with no gateway ledger row at all still
    has resident time and CPU — attributed from the pod's labels, not from a virtual key.
    An operator's view must show it as off-ledger, never as a silent zero.
    """
    live.collect()
    time.sleep(16)
    live.collect()
    row = live.usage_row(BYO)
    assert row["resident_seconds"] > 0, row
    assert row["compute_measured"] is True, row
    assert row["model_source"] == "byo", row

    with live.client() as owner:
        body = owner.get("/portal/api/spend").json()
    entry = [a for a in body["by_agent"] if a["agent"] == BYO]
    assert entry, f"the BYO agent is missing from the owner's page: {body['by_agent']}"
    assert entry[0]["inference"]["on_ledger"] is False, entry[0]
    assert entry[0]["inference"]["note"], (
        "a BYO agent renders as a bare $0 with no explanation, which reads as free or "
        "broken rather than as off-ledger by design")
    assert entry[0]["usage"]["resident_seconds"] > 0, entry[0]


# ================================================================ 8. the Code surface


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                          timeout=180)


def test_the_frozen_code_surface_is_byte_identical_to_the_design_merge_baseline():
    """Contract 6, checked independently of the suite that owns it.

    The camp runs on the Code surface. Every item in this epic was built from NEW files
    beside the frozen set, and this is the measurement that makes that a fact rather than
    a promise — including the untracked-file hole, because `deploy/workspace/` is a Docker
    build context and `git diff` never looks at anything untracked.
    """
    present = _git("cat-file", "-e", f"{PRE_AGENTS_BASELINE}^{{commit}}")
    assert present.returncode == 0, (
        f"the pinned baseline {PRE_AGENTS_BASELINE} is not in this checkout "
        f"({present.stderr.strip()}) — a shallow clone cannot prove the Code surface is "
        "unchanged and must not be allowed to look like it did")

    diff = _git("diff", "--exit-code", PRE_AGENTS_BASELINE, "--", *FROZEN)
    assert diff.returncode == 0, (
        "the Code/workspace surface changed since the Agents epic began. The camp runs on "
        "this surface tomorrow. Revert these hunks and put the change in a new file:\n\n"
        f"{diff.stdout}{diff.stderr}")

    stray = [l for l in _git("ls-files", "--others", "--exclude-standard", "--",
                             *FROZEN).stdout.splitlines() if l.strip()]
    assert not stray, (
        f"untracked files inside the frozen Code surface paths: {stray}")


def test_the_code_surfaces_own_suite_still_passes_unmodified():
    """`tests/test_workspace_shell.py`, run as it ships, on the merged stack.

    It is hermetic by construction — no cluster, no docker, no network, no bundle — so
    "the compose bundle is down" is not a reason to skip it, and it is run rather than
    argued about. Byte-identity above says the file and the server it drives are the
    baseline's; this says they still behave.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_workspace_shell.py", "-q",
         "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=900,
    )
    assert proc.returncode == 0, (
        "the Code surface's own suite is not green on the merged Agents stack:\n"
        f"{proc.stdout[-4000:]}\n{proc.stderr[-2000:]}")
    assert " passed" in proc.stdout, proc.stdout[-2000:]


def test_the_camps_own_surfaces_still_bill_exactly_as_they_did_before(live, bill_before):
    """Additivity on the ONE BILL, over the whole run.

    `chat`, `ide` and `terminal` are what the camp spends on tomorrow. Every base-surface
    row that existed before this file provisioned anything must still exist, still be
    attributed to the same person, and still carry at least the spend it had — the agents
    rows are ADDITIONAL, never a replacement. Read through the DEPLOYED bill both times,
    so the comparison is not made by the code that changed.
    """
    base = {"chat", "ide", "terminal"}
    after = {(r["username"], r["surface"]): r for r in _deployed_spend_rows()}
    for row in bill_before:
        if row.get("surface") not in base:
            continue
        key = (row["username"], row["surface"])
        assert key in after, (
            f"{key} was on the bill before this run and is gone now — an agents change "
            "took a camp surface off the one bill")
        assert (after[key].get("spend") or 0) >= (row.get("spend") or 0) - 1e-9, (
            f"{key} lost spend across this run: {row} -> {after[key]}")

    assert (USER, SURFACE) in after, (
        "the agent's own row is missing from the final bill, so it was not additional")


def test_the_camp_surfaces_are_still_running_and_were_never_touched(live):
    """A guard, not a feature: this file provisions infrastructure in a live namespace.

    ws-baron / ws-claire / ws-student, chat, the gateway and the control plane are what
    the camp needs tomorrow. None of them is any of this file's business, and the cheapest
    way to know is to look.
    """
    # Parsed as JSON rather than assembled from a jsonpath template: a pod missing any one
    # of the fields yields a short line, and the split() that follows raises a ValueError
    # that reads as a test bug instead of naming the unhealthy workspace.
    workspaces = json.loads(_kubectl(
        "get", "pod", "-l", "app.kubernetes.io/component=workspace", "-o", "json"))["items"]
    seen = {}
    for pod in workspaces:
        name = pod["metadata"]["name"]
        # A workspace is identified by its owner label — the same `ws-<user>` family
        # provision-workspace.sh names its objects after. There is no `app` label on
        # these pods; keying on one would silently match nothing and assert nothing.
        owner = (pod["metadata"].get("labels") or {}).get("workspace.enterprise-ai/user")
        assert owner, f"workspace pod {name} carries no owner label: {pod['metadata']}"
        phase = pod["status"].get("phase")
        ready = all(c.get("ready") for c in pod["status"].get("containerStatuses", []))
        seen[f"ws-{owner}"] = (phase, ready)
        assert phase == "Running" and ready, f"workspace {name} is {phase}, ready={ready}"
    for expected in ("ws-baron", "ws-claire", "ws-student"):
        assert expected in seen, f"{expected} is not running: {sorted(seen)}"

    for app in ("chat", "gateway", "control-plane"):
        phase = _kubectl("get", "pod", "-l", f"app={app}",
                         "-o", "jsonpath={.items[0].status.phase}").strip()
        assert phase == "Running", f"{app} is {phase}"

    # And the control plane is still the image it was: nothing here rolled it.
    image = _kubectl("get", "deploy", "control-plane", "-o",
                     "jsonpath={.spec.template.spec.containers[0].image}").strip()
    assert image.endswith(":04e98a9"), (
        f"the control-plane Deployment is running {image}. This file must never roll, "
        "restart, scale or patch it — the camp's login is downstream of that pod "
        "(gate -6f3).")
