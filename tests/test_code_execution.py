"""enterpriseaiframework-082: chat runs code in a real sandbox and shows real output.

DONE condition, verbatim from the item: ask the chat to compute something the model
CANNOT produce by prediction and get the correct answer back; the effect must be
observable AFTERWARD (a file in the session store, a log entry, a sandbox artifact), not
merely plausible in the reply.

Two independent proofs, deliberately not one:

1. CORRECTNESS THE MODEL CANNOT FAKE. fakeprovider computes nothing (see
   tests/test_fakeprovider_execute_code.py and fakeprovider/app.py) — it can only relay
   what a real sandbox actually ran. A sha256 of a nonce generated fresh by THIS test run
   is not in any training data and not derivable from the prompt alone; the only way it
   can come back correct is that codeapi-sandbox really executed the command.

2. AN ARTIFACT OBSERVABLE AFTER THE FACT, INDEPENDENT OF THE REPLY. Every real /exec call
   registers `session:<id>` in codeapi's own Valkey (service/src/service/router.ts) before
   the job runs — this test counts that key set before and after the turn and asserts it
   grew, straight from the session store rather than from anything the model said.

The no-path-to-the-master-key invariant (Finding 27; "no path to ... the gateway master
key") is checked structurally: codeapi-sandbox's actual container environment is
inspected and asserted to carry none of CODEAPI_*/REDIS_*/AWS_*/S3_*/MINIO_* or anything
matching SECRET|TOKEN|PASSWORD|PRIVATE_KEY — the same set api/src/secure-startup.ts
refuses to boot the sandbox over. If the container is running and answering, that check
already passed once at boot; this asserts it again, directly, rather than trusting that
inference.
"""

import hashlib
import re
import uuid

import pytest

import chat_turn
from conftest import compose

pytestmark = pytest.mark.usefixtures("stack_up")

TIMEOUT = 180.0
MODEL = "fake-large"
ENDPOINT_NAME = "Enterprise AI"


def _redis_session_key_count(env) -> int:
    """How many `session:*` keys codeapi's own Valkey currently holds.

    Every real /exec call registers one (service/src/service/router.ts:
    `connection.set('session:'+session_id, sessionKey, 'EX', ...)`) BEFORE the sandbox
    job runs — this is the session store the item's done condition asks for, read
    directly rather than through anything the chat reply claims.
    """
    result = compose(
        "exec", "-T", "codeapi-redis", "valkey-cli", "--no-auth-warning",
        "-a", env["CODEAPI_REDIS_PASSWORD"], "--scan", "--pattern", "session:*",
        check=True,
    )
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    return len(lines)


def _codeapi_sandbox_env(env) -> list[str]:
    """The ACTUAL environment codeapi-sandbox's container process sees, via `docker
    compose exec ... env` — not the compose file's declared config, which could drift
    from what a real container inherits (e.g. from a base image's own ENV lines)."""
    result = compose("exec", "-T", "codeapi-sandbox", "env", check=True)
    return result.stdout.splitlines()


class TestChatRunsRealCode:
    def test_correct_answer_the_model_cannot_predict_and_an_afterward_artifact(
        self, chat_session, chat_url, env
    ):
        models = chat_session.get(
            f"{chat_url}/api/models", timeout=TIMEOUT
        ).json().get(ENDPOINT_NAME, [])
        assert MODEL in models, (
            f"{MODEL!r} not offered on {ENDPOINT_NAME!r}: {models} — codeapi wiring "
            "cannot be exercised without it"
        )

        before = _redis_session_key_count(env)

        nonce = uuid.uuid4().hex
        expected_hash = hashlib.sha256(nonce.encode()).hexdigest()
        command = (
            "python3 -c \"import hashlib; "
            f"print(hashlib.sha256(b'{nonce}').hexdigest())\""
        )
        text = f"EXECUTE_BASH:{command}"

        reply = chat_turn.send_turn(
            chat_session, chat_url, text, model=MODEL, endpoint=ENDPOINT_NAME,
            execute_code=True, timeout=TIMEOUT,
        )
        assert reply, "no assistant message was persisted for this turn"
        reply_text = chat_turn.reply_text(reply)

        assert expected_hash in reply_text, (
            f"expected the sandbox-computed sha256 of a nonce THIS TEST RUN generated "
            f"({nonce!r} -> {expected_hash!r}) to appear in the reply verbatim; got: "
            f"{reply_text!r}. fakeprovider cannot compute this itself (see "
            f"fakeprovider/app.py's find_exec_command/find_tool_result — it only ever "
            f"relays a real tool result), so its absence means either the tool call "
            f"never reached codeapi-sandbox, or the sandbox produced a different answer."
        )

        # Not merely plausible in the reply: a fresh session was actually registered in
        # codeapi's own session store while this call was in flight.
        after = _redis_session_key_count(env)
        assert after > before, (
            f"codeapi-redis's session:* key count did not grow ({before} -> {after}) — "
            "the reply contained the right answer but no new /exec session was ever "
            "registered in the session store, which is the artifact the item's done "
            "condition requires be observable AFTERWARD, independent of the reply"
        )

    def test_the_sandbox_container_cannot_see_any_credential(self, env):
        """Finding 27 / the item's own invariant, checked structurally: codeapi-sandbox
        must have no path to another user's workspace, the cluster API, or the gateway
        master key. api/src/secure-startup.ts refuses to BOOT the sandbox if it can see
        anything matching this pattern — a running, answering container already passed
        that check once; this asserts it directly rather than trusting the inference."""
        forbidden = re.compile(
            r"^(CODEAPI_|REDIS_|AWS_|S3_|MINIO_)|SECRET|TOKEN|PASSWORD|PRIVATE_KEY",
            re.IGNORECASE,
        )
        # The one CODEAPI_*-named exception, by design: a PUBLIC key the sandbox needs
        # to verify the worker's execution manifest. Not a credential — nothing signs
        # with it, nothing authenticates with it, and the pattern above would otherwise
        # also (correctly, but redundantly with this test's own purpose) flag it.
        allowed_exception = "SANDBOX_EXECUTION_MANIFEST_PUBLIC_KEY"

        offenders = []
        for line in _codeapi_sandbox_env(env):
            name = line.partition("=")[0]
            if not name or name == allowed_exception:
                continue
            if forbidden.search(name):
                offenders.append(name)

        assert not offenders, (
            f"codeapi-sandbox's actual container environment carries variables the "
            f"hardened-sandbox-mode boot check exists to keep out of a pod running "
            f"arbitrary user code: {offenders}"
        )
