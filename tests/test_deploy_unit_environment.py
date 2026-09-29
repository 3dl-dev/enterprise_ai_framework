"""The deploy watcher's systemd unit must carry the toolchain the suite it gates on needs.

A systemd user manager gives services a bare PATH. Under it the watcher's own suite run saw
`opencode` missing (9 setup ERRORs) and `go` missing (4 SKIPs) on a tree that is 1033/0/0 from
an interactive shell — so the watcher refused to deploy a green commit
(enterpriseaiframework-e7f).

The expected value is independent of the unit: the tests the suite runs look tools up with
`shutil.which` on the host, so the truth is "the unit's PATH, expanded the way systemd
expands it, resolves each tool to an executable on this host".
"""
import os
import re
import shutil
from pathlib import Path

import pytest

UNIT = Path(__file__).resolve().parent.parent / "deploy/systemd/enterprise-ai-deploy.service"

# Tools the suite hard-requires (error or skip when absent), found by reading the failures.
REQUIRED = ["opencode", "go"]


def _unit_path() -> str:
    m = re.search(r"^Environment=\"?PATH=([^\"\n]+)\"?\s*$", UNIT.read_text(), re.M)
    assert m, "the unit sets no Environment=PATH=..., so the suite runs under systemd's bare PATH"
    return m.group(1).replace("%h", str(Path.home()))


@pytest.mark.parametrize("tool", REQUIRED)
def test_unit_path_resolves_the_tool_the_suite_needs(tool):
    found = shutil.which(tool, path=_unit_path())
    assert found, f"{tool} is not reachable on the unit's PATH: {_unit_path()}"
    assert os.access(found, os.X_OK)
