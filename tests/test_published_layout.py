"""What the published server actually SERVES at each path — not what shape its URLs have.

THE DEFECT THIS EXISTS FOR (enterpriseaiframework-7bc). publish(1) writes to
/live/<user>/<project>/. The publisher before it wrote every project to
/live/<user>/index.html. Those old files are still sitting on the published volume, and
nginx's default `index index.html` served them: GET /live/baron/ answered 200 text/html
with a game published under a layout the code no longer has. Observed live on the cluster
at dispatch time — 5164 bytes of "Pink Unicorn", Last-Modified Tue 28 Jul — at BOTH
/live/baron/ and /listing/baron/.

Nothing caught it because the only coverage asserted the SHAPE of returned URLs
(tests-live/test_portal.py::test_published_list_is_scoped checks that "/live/<user>/" is a
substring of the link). A URL of the right shape that serves the wrong decade of content
passes that check forever. So these tests assert the BYTES coming back, and the negative
case explicitly: content published under the OLD layout must not be reachable.

WHY THIS RUNS REAL NGINX. The whole defect lives in nginx's own resolution order —
`index` beating `autoindex`, the index module's internal redirect re-entering location
matching, how `$request_filename` resolves under `alias` versus `root`. Reimplementing
that in Python would be asserting the thing under test. So the pinned image from
62-published.yaml is started against the config text parsed out of 62-published.yaml, over
a fixture tree, and the tests speak HTTP to it. No cluster, no bundle, no publish volume.

THE FIXTURE TREE deliberately includes a user with BOTH residue and a live project
("mixed"), because the failure mode of a careless fix is refusing the whole user
directory and taking that user's real work down with the stale file.
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "deploy/k8s/62-published.yaml"

# Markers written into the fixture tree. A test that greps for these is asserting which
# bytes came back, which is the point — a status code alone would not have caught the
# original defect, since the original defect was a 200.
STALE = "STALE-OLD-LAYOUT-MUST-NOT-BE-SERVED"
CURRENT = "CURRENT-PER-PROJECT-CONTENT"
MIXED_STALE = "STALE-RESIDUE-BESIDE-A-LIVE-PROJECT"
MIXED_CURRENT = "LIVE-PROJECT-OF-A-USER-WHO-ALSO-HAS-RESIDUE"


def _docs() -> list[dict]:
    return [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]


def _nginx_conf() -> str:
    """The served config, read from the manifest rather than restated here.

    Restating it would let the manifest drift while these tests kept passing against a
    copy — the exact failure shape the file is about.
    """
    for doc in _docs():
        if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "published-nginx":
            return doc["data"]["default.conf"]
    raise AssertionError("published-nginx ConfigMap not found in 62-published.yaml")


def _nginx_image() -> str:
    for doc in _docs():
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "published":
            for c in doc["spec"]["template"]["spec"]["containers"]:
                if c["name"] == "web":
                    return c["image"]
    raise AssertionError("published Deployment web container not found")


def _mount_path() -> str:
    """Where the published volume lands in the pod.

    Read from the manifest because the config's `root` is only correct relative to it;
    if someone moves the mount, these tests must move with it rather than silently
    testing a path the deployment does not use.
    """
    for doc in _docs():
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "published":
            for c in doc["spec"]["template"]["spec"]["containers"]:
                if c["name"] == "web":
                    for m in c["volumeMounts"]:
                        if m["name"] == "published":
                            return m["mountPath"]
    raise AssertionError("published volumeMount not found")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _build_tree(root: Path) -> None:
    """A published volume holding both layouts at once — which is the real cluster state.

    OLD layout: a file directly under <user>/. publish(1) cannot create this any more.
    NEW layout: <user>/<project>/index.html. publish(1) creates only this.
    """
    # A user with nothing but residue — the observed cluster state for `baron`.
    (root / "baron").mkdir(parents=True)
    (root / "baron/index.html").write_text(STALE)

    # A clean user on the current layout — the observed cluster state for `student`.
    (root / "student/my-first-project").mkdir(parents=True)
    (root / "student/my-first-project/index.html").write_text(CURRENT)

    # A user with BOTH. The one that catches an over-broad fix.
    (root / "mixed/real-project").mkdir(parents=True)
    (root / "mixed/index.html").write_text(MIXED_STALE)
    # Residue with no extension: the refusal must be a filesystem test, not a guess at
    # what a stale filename looks like.
    (root / "mixed/leftover-no-extension").write_text(MIXED_STALE)
    (root / "mixed/real-project/index.html").write_text(MIXED_CURRENT)

    # A project whose directory name contains dots — the mirror-image trap. A fix that
    # refused "anything that looks like a filename" would take this live project down.
    (root / "dotty/v1.2.release").mkdir(parents=True)
    (root / "dotty/v1.2.release/index.html").write_text(CURRENT)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Report redirects instead of following them.

    Two reasons. Practically, nginx builds its directory redirect from the port it is
    listening on inside the container (8080), which is not the port the test reached it
    on, so following it dials a closed port. But mainly: every assertion here is about
    what a given path answers with, and a client that quietly follows a hop is a client
    that can report 200-and-the-right-bytes for a path that served neither.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Server:
    def __init__(self, port: int):
        self.port = port
        self._opener = urllib.request.build_opener(_NoRedirect)

    def get(self, path: str) -> tuple[int, str, str]:
        """(status, content-type, body). Errors are responses here, not exceptions."""
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        try:
            with self._opener.open(req, timeout=10) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read().decode("utf-8", "replace")


def _start(conf_text: str, tree: Path, name: str) -> Server:
    image = _nginx_image()
    have = subprocess.run(["docker", "image", "inspect", image],
                          capture_output=True, text=True)
    if have.returncode != 0:
        pull = subprocess.run(["docker", "pull", image], capture_output=True, text=True)
        if pull.returncode != 0:
            pytest.fail(
                f"cannot obtain {image}, which these tests must run the real binary of:\n"
                f"{pull.stderr}\n"
                "Not skipping: the whole point is that nginx's own resolution order is "
                "what is under test, so a skip here would report success for an "
                "unverified serving path."
            )

    conf = tree.parent / "default.conf"
    conf.write_text(conf_text)
    port = _free_port()
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    run = subprocess.run(
        ["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:8080",
         "-v", f"{tree}:{_mount_path()}:ro",
         "-v", f"{conf}:/etc/nginx/conf.d/default.conf:ro",
         image],
        capture_output=True, text=True)
    assert run.returncode == 0, f"docker run failed: {run.stderr}"

    srv = Server(port)
    for _ in range(100):
        try:
            srv.get("/live/")
            return srv
        except Exception:
            time.sleep(0.1)
    logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
    pytest.fail(f"nginx never became ready:\n{logs.stdout}\n{logs.stderr}")


@pytest.fixture(scope="module")
def tree(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("published") / "live"
    root.mkdir()
    _build_tree(root)
    # nginx runs as uid 101 and must traverse the tmp dirs pytest created.
    for p in [root, *root.rglob("*")]:
        p.chmod(0o755 if p.is_dir() else 0o644)
    root.parent.chmod(0o755)
    return root


@pytest.fixture(scope="module")
def served(tree) -> Server:
    name = "eaf-test-published"
    srv = _start(_nginx_conf(), tree, name)
    yield srv
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


# --------------------------------------------------------------- the negative case
#
# Done-condition (2) of enterpriseaiframework-7bc: content published under the OLD layout
# is not reachable after the move.

def test_old_layout_page_is_not_served_at_the_user_path(served):
    """GET /live/<user>/ must not hand back the pre-move index.html.

    This is the exact request the wave-2 reviewer made against the cluster. Before the
    fix it returned 200 text/html carrying the stale page.
    """
    status, ctype, body = served.get("/live/baron/")
    assert STALE not in body, (
        f"/live/baron/ served old-layout content ({status} {ctype}). A viewer sees work "
        "the running publisher can no longer regenerate, with nothing marking it stale."
    )


def test_old_layout_file_is_explicitly_gone(served):
    """Requested directly, old-layout content is refused — and refused visibly.

    Not 200-with-something-else and not a redirect to somewhere plausible: the
    done-condition asks for an explicit 404/410, so that a viewer holding an old link is
    told the thing is gone rather than shown a substitute.
    """
    status, _, body = served.get("/live/baron/index.html")
    assert status == 410, f"expected 410 Gone, got {status}"
    assert STALE not in body


def test_old_layout_residue_without_an_extension_is_also_gone(served):
    """The refusal is a filesystem test, not a filename heuristic.

    A fix keyed on "looks like a file" (an extension regex) passes the index.html case
    and lets this one through.
    """
    status, _, body = served.get("/live/mixed/leftover-no-extension")
    assert status == 410, f"expected 410 Gone, got {status}"
    assert MIXED_STALE not in body


def test_no_path_anywhere_returns_the_stale_bytes(served):
    """Sweep every route into the user level rather than trusting the two above.

    The original defect reached the same file through two different locations
    (/live/ and /listing/), which is exactly why a per-path assertion is not enough.
    """
    for path in ("/live/baron/", "/live/baron/index.html", "/listing/baron/",
                 "/listing/baron/index.html", "/live/mixed/", "/live/mixed/index.html",
                 "/listing/mixed/", "/live/", "/listing/"):
        _, _, body = served.get(path)
        assert STALE not in body and MIXED_STALE not in body, \
            f"{path} leaked old-layout content"


# ----------------------------------------------------------- the listing API's contract

def test_listing_returns_json_for_a_user_with_residue(served):
    """/listing/<user>/ is a listing API and must stay one even when residue is present.

    Before the fix this returned the stale HTML page, resp.json() raised inside
    portal.my_published, and its except-branch reported the user as having published
    nothing. Silent, and wrong in the direction that looks like "no data yet".
    """
    status, ctype, body = served.get("/listing/baron/")
    assert status == 200, f"expected 200, got {status}"
    assert "application/json" in ctype, f"expected JSON, got {ctype!r}: {body[:120]}"
    json.loads(body)


def test_listing_still_enumerates_projects(served):
    """The case that was already working must keep working.

    `student` has no residue, so this path was never broken — which is precisely why it
    is asserted: the fix must not buy the stale-content case by breaking the clean one.
    """
    status, ctype, body = served.get("/listing/student/")
    assert status == 200 and "application/json" in ctype
    entries = json.loads(body)
    dirs = [e["name"] for e in entries if e.get("type") == "directory"]
    assert dirs == ["my-first-project"], f"expected the project listed, got {entries}"


def test_listing_of_a_mixed_user_still_reports_the_live_project(served):
    """Residue must not hide a user's real work from the portal.

    portal.my_published keeps only type == "directory", so the live project has to be
    present and typed correctly for that user's "your work" panel to be right.
    """
    status, _, body = served.get("/listing/mixed/")
    assert status == 200
    entries = json.loads(body)
    dirs = [e["name"] for e in entries if e.get("type") == "directory"]
    assert "real-project" in dirs, f"live project missing from listing: {entries}"


# ------------------------------------------------- the paths that were NOT changed
#
# Everything below was working before this fix. It is asserted because the cheap way to
# satisfy the done-condition is to refuse more than old-layout content.

def test_current_layout_project_is_still_served(served):
    status, _, body = served.get("/live/student/my-first-project/")
    assert status == 200, f"published project no longer served: {status}"
    assert CURRENT in body


def test_live_project_of_a_user_who_also_has_residue_is_still_served(served):
    """The over-broad-fix detector.

    Refusing all of /live/mixed/ would satisfy every negative test above and quietly
    take a real published link offline.
    """
    status, _, body = served.get("/live/mixed/real-project/")
    assert status == 200, f"a live project was taken down with the residue: {status}"
    assert MIXED_CURRENT in body


def test_project_directory_with_dots_in_its_name_is_still_served(served):
    status, _, body = served.get("/live/dotty/v1.2.release/")
    assert status == 200, f"dotted project name refused as if it were a file: {status}"
    assert CURRENT in body


def test_user_directory_lists_projects(served):
    """A clean user's /live/<user>/ still shows their projects."""
    status, _, body = served.get("/live/student/")
    assert status == 200
    assert "my-first-project" in body


def test_user_with_residue_still_gets_a_project_list(served):
    """Residue must not blank out the user-level index for their real work.

    This is why the fix is not a blanket 410 on /live/<user>/.
    """
    status, _, body = served.get("/live/mixed/")
    assert status == 200, f"user-level listing lost: {status}"
    assert "real-project" in body


def test_project_path_without_a_trailing_slash_still_redirects(served):
    status, _, _ = served.get("/live/student/my-first-project")
    assert status == 301, f"expected the usual directory redirect, got {status}"


def test_root_listing_of_users_still_works(served):
    """Also the deployment's readinessProbe target — if this breaks, the pod never starts."""
    status, _, body = served.get("/live/")
    assert status == 200
    assert "baron" in body and "student" in body


def test_unknown_user_is_a_plain_404(served):
    status, _, _ = served.get("/live/nobody/")
    assert status == 404


def test_server_does_not_serve_the_image_welcome_page(served):
    """Scoping `root` to the user-level location, not the server, is load-bearing.

    A server-level `root /usr/share/nginx/html` makes the nginx image's own index.html
    reachable, and this server would answer /index.html with "Welcome to nginx!" — the
    published server advertising itself with content nobody published. It 404s before
    this change and must keep 404ing.
    """
    status, _, body = served.get("/index.html")
    assert status == 404, f"expected 404, got {status}"
    assert "Welcome to nginx" not in body
