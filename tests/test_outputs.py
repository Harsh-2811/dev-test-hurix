"""Verifier tests for the firmware release-publisher task.

The candidate delivers ``/app/publisher/release-publisher.mjs``, run via
``npm run report``. These tests drive only the candidate-visible surface:

* the program's stdout, compared against the golden file with the random
  receipt id masked;
* the reconciled bundle set, recomputed here directly from the raw CSV so
  grading never trusts the candidate's own SQL;
* the real OpenSSL verification path, exercised with verifier-minted signatures
  from both the current and the revoked keypair;
* ``releases.duckdb``, read to confirm receipts and request tokens persist;
* a second run, to confirm idempotent replay and no duplicate publications on
  the gateway.

The gateway is already running (tests/test.sh launched it). Grading is binary:
every test must pass for reward 1.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path

import duckdb
import pytest
import requests

APP_ROOT = Path(os.environ.get("APP_ROOT", "/app"))
PUBLISHER = APP_ROOT / "publisher" / "release-publisher.mjs"
MANIFEST_CSV = APP_ROOT / "fixtures" / "build_manifest.csv"
EXPECTED_REPORT = APP_ROOT / "reports" / "publications.expected.txt"
DATABASE_FILE = APP_ROOT / "releases.duckdb"
KEYS_DIR = Path(os.environ.get("KEYS_DIR", str(APP_ROOT / "keys")))
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:7070")

# The receipt (publication_id) is minted randomly by the gateway. It is the one
# field the golden file cannot pin, so both sides are masked before diffing.
RECEIPT_MASK = re.compile(r"RECEIPT=\S+")


def mask_receipts(text: str) -> str:
    return RECEIPT_MASK.sub("RECEIPT=<id>", text.strip())


# ---------------------------------------------------------------------------
# Running the candidate
# ---------------------------------------------------------------------------


def run_report() -> subprocess.CompletedProcess:
    """Invoke `npm run report` the way the task documents it."""
    return subprocess.run(
        ["npm", "run", "--silent", "report"],
        cwd=str(APP_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.fixture(scope="session")
def first_run() -> subprocess.CompletedProcess:
    """The publisher's first run. Session-scoped: it is the shared subject of
    most assertions, and re-running it per test would defeat the point."""
    if not PUBLISHER.is_file():
        pytest.fail(
            f"{PUBLISHER} does not exist — the publisher was never implemented."
        )
    result = run_report()
    if result.returncode != 0:
        pytest.fail(
            "`npm run report` exited "
            f"{result.returncode}.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


# ---------------------------------------------------------------------------
# Independent reconciliation — recomputed here from the raw CSV
# ---------------------------------------------------------------------------


def expected_bundles() -> dict[str, tuple[int, int]]:
    """Recompute the publishable set without touching the candidate's SQL.

    Rules: collapse rows identical across every column; a WITHDRAWAL cancels the
    BUILD its supersedes_id names; a bundle survives only if at least one build
    remains.
    """
    with MANIFEST_CSV.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    deduped = list({tuple(sorted(row.items())): row for row in rows}.values())

    withdrawn = {
        row["supersedes_id"]
        for row in deduped
        if row["record_type"] == "WITHDRAWAL" and row["supersedes_id"]
    }

    counts: dict[str, int] = defaultdict(int)
    totals: dict[str, int] = defaultdict(int)
    for row in deduped:
        if row["record_type"] != "BUILD" or row["entry_id"] in withdrawn:
            continue
        counts[row["bundle_id"]] += 1
        totals[row["bundle_id"]] += int(row["size_bytes"])

    return {bundle: (counts[bundle], totals[bundle]) for bundle in sorted(counts)}


# ---------------------------------------------------------------------------
# functional_criteria: output reproduces the golden file
# ---------------------------------------------------------------------------


def test_report_matches_golden_output(first_run):
    """functional_criteria[id=report_matches_golden_output]: `npm run report`
    reproduces reports/publications.expected.txt, in order, with only the random
    receipt id masked."""
    expected = mask_receipts(EXPECTED_REPORT.read_text(encoding="utf-8"))
    actual = mask_receipts(first_run.stdout)
    assert actual == expected, (
        "publisher stdout did not match the golden report.\n"
        f"--- expected ---\n{expected}\n--- actual ---\n{actual}"
    )


def test_report_lines_are_ordered_by_bundle_id(first_run):
    """functional_criteria[id=deterministic_ordering]: status lines are emitted in
    ascending bundle_id order, two lines per publishable bundle."""
    bundles = [
        line.split()[1] for line in first_run.stdout.strip().splitlines() if line.strip()
    ]
    pairs = [bundles[i : i + 2] for i in range(0, len(bundles), 2)]
    assert all(pair[0] == pair[1] for pair in pairs), (
        "expected exactly two consecutive lines per bundle (SIGNED then PUBLISHED)"
    )
    ordered = [pair[0] for pair in pairs]
    assert ordered == sorted(ordered), f"bundles not in ascending order: {ordered}"


# ---------------------------------------------------------------------------
# functional_criteria: reconciliation is correct
# ---------------------------------------------------------------------------


def test_publishable_bundle_set_is_correct(first_run):
    """functional_criteria[id=reconciliation_bundle_membership]: the published
    bundles are exactly those that survive dedup + withdrawal, recomputed here
    from the raw CSV. A bundle whose every build was withdrawn must not appear."""
    published = {
        line.split()[1]
        for line in first_run.stdout.strip().splitlines()
        if " SIGNED " in line
    }
    assert published == set(expected_bundles()), (
        "publishable bundle set is wrong — check duplicate collapsing and "
        "withdrawal handling"
    )


def test_fully_withdrawn_bundle_is_excluded(first_run):
    """functional_criteria[id=fully_withdrawn_bundle_excluded]: the trap case —
    a bundle whose builds are all withdrawn nets to nothing and is skipped."""
    all_bundles = {
        row["bundle_id"]
        for row in csv.DictReader(MANIFEST_CSV.open(newline="", encoding="utf-8"))
    }
    fully_withdrawn = all_bundles - set(expected_bundles())
    assert fully_withdrawn, (
        "fixture no longer contains a fully-withdrawn bundle; this test would be "
        "tautological"
    )
    for bundle in fully_withdrawn:
        assert bundle not in first_run.stdout, (
            f"{bundle} has no surviving builds but was published anyway"
        )


def test_descriptor_totals_match_reconciliation(first_run):
    """functional_criteria[id=descriptor_totals_correct]: the signed descriptor
    carries the surviving artifact count and summed size_bytes. Read back from
    the gateway's own receipt via the candidate's stored descriptor."""
    con = duckdb.connect(str(DATABASE_FILE), read_only=True)
    try:
        rows = con.execute(
            "SELECT bundle_id, descriptor FROM publications WHERE status = 'PUBLISHED'"
        ).fetchall()
    finally:
        con.close()

    assert rows, "no PUBLISHED rows persisted in releases.duckdb"
    expected = expected_bundles()
    for bundle_id, descriptor in rows:
        payload = json.loads(descriptor)
        count, total = expected[bundle_id]
        assert payload["artifact_count"] == count, f"{bundle_id} artifact_count"
        assert payload["total_bytes"] == total, f"{bundle_id} total_bytes"


# ---------------------------------------------------------------------------
# functional_criteria: signing, key rotation, and the real verification path
# ---------------------------------------------------------------------------


def sign_with(key_dir: str, name: str, payload: bytes) -> str:
    """Mint a detached CMS signature with one of the two keypairs."""
    scratch = Path(tempfile.mkdtemp(prefix="verifier-sign-"))
    try:
        content = scratch / "descriptor.bin"
        content.write_bytes(payload)
        result = subprocess.run(
            [
                "openssl", "cms", "-sign",
                "-in", str(content),
                "-signer", str(KEYS_DIR / key_dir / f"{name}.cert.pem"),
                "-inkey", str(KEYS_DIR / key_dir / f"{name}.key.pem"),
                "-md", "sha256",
                "-outform", "PEM",
                "-binary",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"openssl cms -sign failed: {result.stderr}"
        return result.stdout
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_current_key_signature_is_accepted():
    """functional_criteria[id=current_key_accepted]: a descriptor signed with the
    key in force passes the gateway's real verification path. Verifier-owned, so
    it holds independently of the candidate's own output."""
    descriptor = '{"artifact_count":1,"bundle_id":"BND-VERIFIER-OK","total_bytes":1}'
    response = requests.post(
        f"{GATEWAY_URL}/v1/publications",
        json={
            "descriptor": descriptor,
            "signature": sign_with("current", "current", descriptor.encode("utf-8")),
            "request_token": "token-verifier-current",
        },
        timeout=60,
    )
    assert response.status_code == 200, response.text
    assert response.json().get("status") == "PUBLISHED"


def test_revoked_key_signature_is_rejected():
    """functional_criteria[id=revoked_key_rejected]: the rotation trap — a
    descriptor signed with the retired key does not chain to the current
    certificate and is rejected with UNTRUSTED_SIGNATURE, with nothing recorded."""
    descriptor = '{"artifact_count":1,"bundle_id":"BND-VERIFIER-BAD","total_bytes":1}'
    response = requests.post(
        f"{GATEWAY_URL}/v1/publications",
        json={
            "descriptor": descriptor,
            "signature": sign_with("revoked", "revoked", descriptor.encode("utf-8")),
            "request_token": "token-verifier-revoked",
        },
        timeout=60,
    )
    assert response.status_code == 400, response.text
    assert response.json().get("error") == "UNTRUSTED_SIGNATURE"


def test_publisher_reported_the_current_key_id(first_run):
    """functional_criteria[id=reports_current_key_id]: the key id on every SIGNED
    line is the one the gateway advertises, not a hardcoded string."""
    key_id = requests.get(f"{GATEWAY_URL}/v1/signing-key/current", timeout=60).json()[
        "key_id"
    ]
    signed = [
        line for line in first_run.stdout.strip().splitlines() if " SIGNED " in line
    ]
    assert signed, "no SIGNED lines emitted"
    for line in signed:
        assert line.endswith(f"KEY={key_id}"), f"wrong key id on: {line}"


def test_no_submission_was_untrusted(first_run):
    """functional_criteria[id=no_untrusted_submissions]: every submission the
    publisher made was PUBLISHED — nothing signed with the revoked key."""
    combined = first_run.stdout + first_run.stderr
    assert "UNTRUSTED_SIGNATURE" not in combined, (
        "a submission was rejected as UNTRUSTED_SIGNATURE — the publisher signed "
        "with the revoked key"
    )
    for line in first_run.stdout.strip().splitlines():
        if " PUBLISHED " in line:
            assert "STATUS=PUBLISHED" in line, f"non-published status: {line}"


# ---------------------------------------------------------------------------
# functional_criteria: persistence and idempotency
# ---------------------------------------------------------------------------


def test_receipts_and_tokens_are_persisted(first_run):
    """functional_criteria[id=receipts_persisted]: releases.duckdb holds the
    request token and publication id for every published bundle."""
    assert DATABASE_FILE.is_file(), f"{DATABASE_FILE} was never created"

    con = duckdb.connect(str(DATABASE_FILE), read_only=True)
    try:
        rows = con.execute(
            "SELECT bundle_id, request_token, publication_id, status "
            "FROM publications WHERE status = 'PUBLISHED' ORDER BY bundle_id"
        ).fetchall()
    finally:
        con.close()

    persisted = {row[0] for row in rows}
    assert persisted == set(expected_bundles()), (
        f"persisted bundles {sorted(persisted)} != publishable "
        f"{sorted(expected_bundles())}"
    )
    for bundle_id, token, publication_id, _ in rows:
        assert token, f"{bundle_id} has an empty request_token"
        assert publication_id, f"{bundle_id} has an empty publication_id"


def test_rerun_is_idempotent(first_run):
    """functional_criteria[id=rerun_idempotent]: a second run reproduces the first
    byte-for-byte (receipts replayed, not re-minted)."""
    second = run_report()
    assert second.returncode == 0, (
        f"second run exited {second.returncode}.\nstderr:\n{second.stderr}"
    )
    assert second.stdout == first_run.stdout, (
        "re-running produced different output — receipts were not replayed.\n"
        f"--- first ---\n{first_run.stdout}\n--- second ---\n{second.stdout}"
    )


def test_rerun_created_no_duplicate_publications(first_run):
    """functional_criteria[id=no_duplicate_publications]: after the re-run the
    gateway still holds exactly one publication per bundle. Ground truth is the
    gateway's replay behaviour, probed over HTTP only."""
    con = duckdb.connect(str(DATABASE_FILE), read_only=True)
    try:
        rows = con.execute(
            "SELECT bundle_id, request_token, publication_id "
            "FROM publications WHERE status = 'PUBLISHED'"
        ).fetchall()
    finally:
        con.close()

    tokens = [row[1] for row in rows]
    assert len(tokens) == len(set(tokens)), "duplicate request tokens persisted"

    # Re-posting a stored token must replay the original receipt rather than
    # mint a second publication. A signature is required to reach that path, but
    # replay is resolved before verification, so any well-formed PEM suffices.
    for bundle_id, token, publication_id in rows:
        replay = requests.post(
            f"{GATEWAY_URL}/v1/publications",
            json={
                "descriptor": "{}",
                "signature": "-----BEGIN CMS-----\nreplay-probe\n-----END CMS-----\n",
                "request_token": token,
            },
            timeout=60,
        )
        assert replay.status_code == 200, (
            f"token {token} for {bundle_id} did not replay: {replay.text}"
        )
        assert replay.json().get("publication_id") == publication_id, (
            f"{bundle_id} replayed a different publication id — a duplicate was created"
        )


# ---------------------------------------------------------------------------
# Sensitivity control — grading must not be tautologically satisfied
# ---------------------------------------------------------------------------


def test_revoked_key_publisher_would_fail():
    """functional_criteria[id=grader_is_sensitive]: a publisher that signs with
    the revoked key does NOT get a publication, so passing the suite genuinely
    requires using the key in force."""
    descriptor = '{"artifact_count":1,"bundle_id":"BND-SENSITIVITY","total_bytes":1}'
    response = requests.post(
        f"{GATEWAY_URL}/v1/publications",
        json={
            "descriptor": descriptor,
            "signature": sign_with("revoked", "revoked", descriptor.encode("utf-8")),
            "request_token": "token-sensitivity-probe",
        },
        timeout=60,
    )
    assert response.status_code != 200, (
        "the revoked key was accepted — the grader is not sensitive to key rotation"
    )
