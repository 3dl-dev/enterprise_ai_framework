#!/usr/bin/env python3
"""Generate and fill in the codeapi Ed25519 keypairs .env needs, in place.

Two independent keypairs, never confused (enterpriseaiframework-082):

- CODEAPI_JWT_PRIVATE_KEY / CODEAPI_JWT_PUBLIC_KEY — PEM. LibreChat signs a short-lived
  per-request JWT with the private half; codeapi's api service verifies it with the
  public half (CODEAPI_AUTH_PROVIDER=librechat-jwt). This is the identity LibreChat
  asserts about the calling user — Finding 27 applies, so this is the ONLY credential
  that gets to say who the caller is; the request body's own user_id is not trusted for
  that by the service (service/src/session-key.ts#resolveSessionKey), and this bundle
  must not add a second path that pretends otherwise.
- CODEAPI_EXECUTION_MANIFEST_PRIVATE_KEY / SANDBOX_EXECUTION_MANIFEST_PUBLIC_KEY —
  base64 PKCS8/SPKI DER (execution-manifest.ts base64-decodes before createPrivateKey/
  createPublicKey; PEM would also parse, DER is what the upstream compose already uses
  by convention). codeapi's own worker signs a manifest for each job with the private
  half; codeapi-sandbox verifies it with the public half. This one never leaves the
  codeapi services and is unrelated to LibreChat's identity.

Written as a standalone python script, not inline sed, for one reason
(enterpriseaiframework-082 wave 18's defect #1): bin/render-env.sh's ensure() filled an
empty var with `sed "s|^VAR=$|VAR=${value}|"`, and GNU sed's replacement string
interprets `\n` in the replacement as a literal newline — so a one-line PEM with escaped
newlines came out as a multi-line value, `--env-file` silently kept only the first line,
and the surface booted fine and only failed once something tried to sign with the
(truncated) key. Doing the substitution here, in python, with no shell string
interpolation of the generated value at all, removes that failure mode rather than
working around it.

PEM values are written SINGLE-quoted (enterpriseaiframework-082 wave 18's defect #2):
unquoted, the six bundle/bin scripts that do `set -a; . ./.env` die on the PEM's first
line with "PRIVATE: command not found" (bash tries to run the unquoted words); double-
quoted, compose's own `.env` interpolation expands the PEM's literal `\n` sequences into
real newlines before the JS side ever sees them, which breaks the multi-line-collapsed-
to-one-line encoding this file deliberately produces. Single quotes are the one form
bash, compose, and both the LibreChat and codeapi JWT parsers agree on: bash and compose
leave a single-quoted value untouched, and both parsers un-escape the literal `\n`
themselves.
"""

import re
import subprocess
import sys
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _openssl(*args: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["openssl", *args],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout


def _gen_ed25519_pem_pair() -> tuple[str, str]:
    priv_pem = _openssl("genpkey", "-algorithm", "ed25519").decode()
    pub_pem = _openssl("pkey", "-pubout", input_bytes=priv_pem.encode()).decode()
    return priv_pem, pub_pem


def _gen_ed25519_der_b64_pair() -> tuple[str, str]:
    priv_pem, _ = _gen_ed25519_pem_pair()
    priv_der = _openssl("pkey", "-outform", "DER", input_bytes=priv_pem.encode())
    pub_der = _openssl("pkey", "-pubout", "-outform", "DER", input_bytes=priv_pem.encode())
    import base64

    return base64.b64encode(priv_der).decode(), base64.b64encode(pub_der).decode()


def _pem_to_env_literal(pem: str) -> str:
    """A PEM, collapsed to one line with literal backslash-n, matching what
    normalizePem() (LibreChat) and publicKeyFromValue() (codeapi) both expect:
    `value.replace(/\\n/g, '\\n')` before parsing."""
    return pem.strip().replace("\n", "\\n")


def _read_env() -> list[str]:
    if not ENV_PATH.exists():
        return []
    return ENV_PATH.read_text().splitlines(keepends=True)


def _var_is_set(lines: list[str], var: str) -> bool:
    pattern = re.compile(rf"^{re.escape(var)}=.+$")
    return any(pattern.match(line.rstrip("\n")) for line in lines)


def _fill_or_append(lines: list[str], var: str, value: str, quoted: bool) -> list[str]:
    rendered = f"{var}='{value}'\n" if quoted else f"{var}={value}\n"
    empty_pattern = re.compile(rf"^{re.escape(var)}=\s*$")
    for i, line in enumerate(lines):
        if empty_pattern.match(line.rstrip("\n")):
            lines[i] = rendered
            return lines
    lines.append(rendered)
    return lines


def main() -> int:
    lines = _read_env()
    if not lines:
        print(f"error: {ENV_PATH} does not exist yet", file=sys.stderr)
        return 1

    changed = False

    if not _var_is_set(lines, "CODEAPI_JWT_PRIVATE_KEY"):
        priv_pem, pub_pem = _gen_ed25519_pem_pair()
        lines = _fill_or_append(lines, "CODEAPI_JWT_PRIVATE_KEY", _pem_to_env_literal(priv_pem), quoted=True)
        lines = _fill_or_append(lines, "CODEAPI_JWT_PUBLIC_KEY", _pem_to_env_literal(pub_pem), quoted=True)
        print("  generated CODEAPI_JWT_PRIVATE_KEY / CODEAPI_JWT_PUBLIC_KEY (Ed25519 PEM pair)")
        changed = True

    if not _var_is_set(lines, "CODEAPI_EXECUTION_MANIFEST_PRIVATE_KEY"):
        priv_b64, pub_b64 = _gen_ed25519_der_b64_pair()
        lines = _fill_or_append(lines, "CODEAPI_EXECUTION_MANIFEST_PRIVATE_KEY", priv_b64, quoted=False)
        lines = _fill_or_append(lines, "SANDBOX_EXECUTION_MANIFEST_PUBLIC_KEY", pub_b64, quoted=False)
        print("  generated CODEAPI_EXECUTION_MANIFEST_PRIVATE_KEY / SANDBOX_EXECUTION_MANIFEST_PUBLIC_KEY (Ed25519 DER pair)")
        changed = True

    if changed:
        ENV_PATH.write_text("".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
