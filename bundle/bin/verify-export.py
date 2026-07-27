#!/usr/bin/env python3
"""Verify an exported ledger. Standalone.

Deliberately depends on nothing but the Python standard library, and talks to no running
service. After the exit path is taken this whole platform is gone — an archive that needs
the vendor's software to check is not an exit, it is a hostage.

    verify-export.py <export-dir>

Exit status is 0 only if every check passes.

The chain digest is reimplemented here rather than imported from the control plane. That
duplication is intentional — the verifier has to survive the control plane's deletion —
and a test asserts the two implementations agree, so drift is caught rather than assumed
away.
"""

import csv
import hashlib
import json
import sys
from pathlib import Path

GENESIS_HASH = "0" * 64


def digest(prev_hash, ts, actor, action, target, detail) -> str:
    payload = "\x00".join(
        [
            prev_hash,
            ts,
            actor,
            action,
            target or "",
            json.dumps(detail, sort_keys=True, separators=(",", ":")),
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def fail(msg):
    print(f"FAIL  {msg}")
    return False


def ok(msg):
    print(f"ok    {msg}")
    return True


def verify_audit(path: Path, expected_count, expected_head):
    if not path.exists():
        return fail(f"{path.name} is missing")

    prev = GENESIS_HASH
    count = 0
    last_seq = None

    with path.open() as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError as exc:
                return fail(f"{path.name}:{lineno} is not valid JSON: {exc}")

            if last_seq is not None and e["seq"] <= last_seq:
                return fail(
                    f"{path.name}:{lineno} out of order (seq {e['seq']} after {last_seq}); "
                    "the chain is only verifiable in write order"
                )
            last_seq = e["seq"]

            if e["prev_hash"] != prev:
                return fail(
                    f"{path.name}:{lineno} breaks the chain: prev_hash does not match the "
                    f"previous event's hash (seq {e['seq']})"
                )

            recomputed = digest(
                e["prev_hash"], e["ts"], e["actor"], e["action"], e["target"], e["detail"]
            )
            if recomputed != e["hash"]:
                return fail(
                    f"{path.name}:{lineno} has been altered: recomputed hash does not "
                    f"match the recorded one (seq {e['seq']})"
                )

            prev = e["hash"]
            count += 1

    if expected_count is not None and count != expected_count:
        return fail(
            f"{path.name} has {count} events but the manifest declares {expected_count} "
            "— the export is truncated"
        )
    if expected_head is not None and count and prev != expected_head:
        return fail(f"{path.name} ends at {prev[:12]}… but the manifest declares "
                    f"{expected_head[:12]}…")

    return ok(f"audit chain verified: {count} events, head {prev[:12]}…")


def verify_csv(path: Path, expected_rows, label):
    if not path.exists():
        return fail(f"{path.name} is missing")
    with path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    if expected_rows is not None and len(rows) != expected_rows:
        return fail(
            f"{path.name} has {len(rows)} rows but the manifest declares {expected_rows} "
            "— the export is truncated"
        )
    return ok(f"{label}: {len(rows)} rows")


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2

    d = Path(argv[1])
    if not d.is_dir():
        print(f"FAIL  {d} is not a directory")
        return 1

    manifest_path = d / "manifest.json"
    if not manifest_path.exists():
        print("FAIL  manifest.json is missing; completeness cannot be checked")
        return 1
    manifest = json.loads(manifest_path.read_text())

    results = [
        verify_audit(
            d / "audit.jsonl",
            manifest.get("audit_events"),
            manifest.get("audit_chain_head"),
        ),
        verify_csv(d / "spend.csv", manifest.get("spend_rows"), "spend ledger"),
        verify_csv(d / "keys.csv", manifest.get("virtual_keys"), "key inventory"),
    ]

    print()
    if all(results):
        print(f"EXPORT VERIFIED — {d}")
        print("This archive stands on its own. Nothing from the platform is needed to read it.")
        return 0
    print(f"EXPORT FAILED VERIFICATION — {d}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
