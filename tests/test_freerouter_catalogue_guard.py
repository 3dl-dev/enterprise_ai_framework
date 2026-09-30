"""The deploy path is off Forge (enterpriseaiframework-e7f): the guard asks freerouter.

Executable, not string-presence. Inputs:
  * tests/fixtures/freerouter-models.prod.json — a real GET /v1/models body captured from the
    production freerouter (138 models). Independent source of truth for the pass case.
  * fakes-only / too-short catalogues derived from it by relabelling or truncating ids.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GUARD = REPO / "deploy" / "bin" / "check-freerouter-catalogue.sh"
GOLDEN = REPO / "tests" / "fixtures" / "freerouter-models.prod.json"


def run_guard(tmp_path, ids):
    f = tmp_path / "models.json"
    f.write_text(json.dumps({"data": [{"id": i} for i in ids]}))
    return subprocess.run([str(GUARD), "--from-file", str(f)], capture_output=True, text=True)


def golden_ids():
    return [m["id"] for m in json.loads(GOLDEN.read_text())["data"]]


def test_real_production_catalogue_passes():
    r = subprocess.run([str(GUARD), "--from-file", str(GOLDEN)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "138 models" in r.stdout


def test_fakes_only_catalogue_is_refused(tmp_path):
    r = run_guard(tmp_path, ["fake-large", "fake-embed", "fake-provider/fake-gpt-small"])
    assert r.returncode == 1 and "REFUSING" in r.stderr


def test_relabelled_real_catalogue_is_refused(tmp_path):
    # second mutation: same 138 entries, every id relabelled to a fake name
    r = run_guard(tmp_path, [f"fake-{i}" for i in golden_ids()])
    assert r.returncode == 1 and "0 real models" in r.stderr


def test_a_short_real_catalogue_is_refused(tmp_path):
    r = run_guard(tmp_path, golden_ids()[:10])  # exactly 10: the bound is strictly > 10
    assert r.returncode == 1
    assert run_guard(tmp_path, golden_ids()[:11]).returncode == 0


def test_empty_or_garbage_catalogue_is_refused(tmp_path):
    assert run_guard(tmp_path, []).returncode == 1
    f = tmp_path / "bad.json"
    f.write_text("<html>502</html>")
    assert subprocess.run([str(GUARD), "--from-file", str(f)], capture_output=True).returncode == 1


def test_no_upstream_render_ignores_forge_credentials(tmp_path):
    """Poison the real inputs, env var AND bundle/.env, and the render stays fakes-only."""
    bundle = tmp_path / "bundle"
    (bundle / "bin").mkdir(parents=True)
    (bundle / "litellm").mkdir()
    shutil.copy(REPO / "bundle/bin/render-gateway-config.py", bundle / "bin")
    shutil.copy(REPO / "bundle/litellm/config.base.yaml", bundle / "litellm")
    (bundle / ".env").write_text("FORGE_API_KEY=sk-poison\nFORGE_BASE_URL=http://127.0.0.1:9\n")
    env = {**os.environ, "FORGE_API_KEY": "sk-poison", "FORGE_BASE_URL": "http://127.0.0.1:9"}
    r = subprocess.run(["python3", str(bundle / "bin/render-gateway-config.py"), "--no-upstream"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = (bundle / "litellm/config.generated.yaml").read_text()
    assert "model_name: fake-large" in out
    assert "os.environ/FORGE_" not in out and "@GENERATED_UPSTREAMS@" not in out


def test_gateway_manifest_and_deploy_need_no_forge_secret():
    assert "FORGE_API_KEY" not in (REPO / "deploy/k8s/30-gateway.yaml").read_text()
    deploy = (REPO / "deploy/bin/deploy.sh").read_text().replace("os.environ/FORGE_", "")
    assert "FORGE_API_KEY" not in deploy
