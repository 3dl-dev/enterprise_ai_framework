"""The Agents view must re-poll a still-starting agent until it settles — enterpriseaiframework-7af.

An agent's pod goes `starting` -> `running` with nothing on the portal to trigger a refetch:
the list is reloaded only on a tab switch or an explicit action. So an agent the user just
created — or just wired a connector to — sits at "starting…" forever until they navigate away
and back. A working boot that reads as a stuck one is the finding-43 failure mode, and it is
exactly what a dogfood user hit ("stuck in starting... or the UI isn't updating").

The fix is a bounded poll in `control-plane/app/portal_static/app.js`: while the Agents tab is
on screen and any agent is still transitioning, refetch until nothing is; stop when the tab is
left or the browser tab is backgrounded. These are static checks on that wiring — the behaviour
of the poll predicate is exercised directly in `test_agents_status_poll_logic.py` under Node.
What this file guards is the realistic regression: someone deleting the poll, or unbounding it
so it hammers the API on a hidden tab.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_JS = REPO / "control-plane" / "app" / "portal_static" / "app.js"


def _js() -> str:
    return APP_JS.read_text()


def test_loadagents_schedules_a_repoll():
    js = _js()
    assert "scheduleAgentsPoll(rows)" in js, (
        "loadAgents() must call scheduleAgentsPoll(rows) after rendering, or a starting agent "
        "never refreshes to running without a manual tab switch (finding-43 failure mode)"
    )


def test_the_poll_only_runs_while_something_is_transitioning():
    js = _js()
    m = re.search(r"function scheduleAgentsPoll\([^)]*\)\s*\{(.*?)\n\}", js, re.DOTALL)
    assert m, "scheduleAgentsPoll must exist"
    body = m.group(1)
    assert 'a.status === "starting"' in body, (
        "the poll must key on a transient status (starting) so it STOPS once every agent is "
        "running or stopped — an unconditional poll would hammer the API forever"
    )
    assert "setTimeout(loadAgents" in body, "the poll must re-invoke loadAgents on a timer"


def test_the_poll_is_bounded_to_the_visible_agents_tab():
    js = _js()
    body = re.search(r"function scheduleAgentsPoll\([^)]*\)\s*\{(.*?)\n\}", js, re.DOTALL).group(1)
    assert "agentsTabActive()" in body and "document.hidden" in body, (
        "the poll must not run when the Agents tab is not showing or the browser tab is "
        "backgrounded — otherwise it keeps polling behind the user's back"
    )


def test_leaving_the_agents_tab_stops_the_poll():
    js = _js()
    # In showTab, the non-agents branch must clear the timer.
    m = re.search(r"function showTab\([^)]*\)\s*\{(.*?)\n\}", js, re.DOTALL)
    assert m and "stopAgentsPoll()" in m.group(1), (
        "showTab must stopAgentsPoll() when switching away from the Agents tab"
    )


def test_returning_to_the_browser_tab_refreshes():
    js = _js()
    assert re.search(
        r'addEventListener\(\s*["\']visibilitychange["\'].*?agentsTabActive\(\).*?loadAgents\(\)',
        js,
        re.DOTALL,
    ), (
        "a visibilitychange handler must refetch when the tab becomes visible again, because a "
        "backgrounded tab throttles the poll timer to a stall"
    )
