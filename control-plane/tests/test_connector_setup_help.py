"""The connector setup form must tell a first-timer how to mint the tokens it asks for.

"App-level token (starts xapp-)" is not something someone can produce without being told
where it lives, so the wizard renders per-connector guidance. These tests read the real
app.js and pin that the guidance exists, points at the right console, and names the two
traps that otherwise cost an afternoon (Slack's separate app-level token; Discord's
MESSAGE CONTENT intent). JS can't execute here, so this is a source check — enough to catch
the guidance being dropped or a connector shipping without any.
"""

from pathlib import Path

import pytest

APP_JS = (
    Path(__file__).resolve().parent.parent / "app" / "portal_static" / "app.js"
).read_text()


def test_the_wizard_renders_help_before_the_fields():
    # The fields renderer must actually call the help renderer, or the data is dead.
    assert "function connectorHelp(" in APP_JS
    assert "connectorHelp(container, kind)" in APP_JS, (
        "connectorFields must render the help block, not just define it"
    )


def test_every_configurable_connector_has_setup_help():
    # Whatever the wizard lets you configure, it must also explain.
    for kind in ("slack", "discord", "email"):
        assert f"{kind}: {{" in APP_JS.replace("  ", " ") or f"{kind}:" in APP_JS, (
            f"CONNECTOR_HELP has no entry for {kind}"
        )


@pytest.mark.parametrize(
    "must_contain",
    [
        "api.slack.com/apps",          # the console that mints it
        "xoxb-",                        # bot token
        "xapp-",                        # the app-level token nobody finds on their own
        "connections:write",            # the scope that trips people up
        "Socket Mode",
    ],
)
def test_slack_help_names_the_real_steps(must_contain):
    assert must_contain in APP_JS, f"Slack setup help is missing: {must_contain}"


@pytest.mark.parametrize(
    "must_contain",
    [
        "discord.com/developers",
        "MESSAGE CONTENT INTENT",       # without it every message arrives empty, no error
        "Reset Token",
    ],
)
def test_discord_help_names_the_real_steps(must_contain):
    assert must_contain in APP_JS, f"Discord setup help is missing: {must_contain}"


def test_help_links_open_safely_in_a_new_tab():
    # An external link from an authed page must not leak the opener.
    assert 'rel = "noopener noreferrer"' in APP_JS or 'noopener' in APP_JS
