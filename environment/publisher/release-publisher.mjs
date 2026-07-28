#!/usr/bin/env node
//
// Firmware release publisher.
//
// Pipeline:
//   1. load fixtures/build_manifest.csv into releases.duckdb
//   2. reconcile it in SQL (collapse exact duplicates, apply withdrawals) to get
//      the publishable bundles and their surviving artifact count / total bytes
//   3. build a canonical descriptor per bundle and sign it with the CURRENT
//      code-signing key via detached OpenSSL CMS
//   4. POST each signed descriptor to the distribution gateway
//   5. persist receipts + idempotency tokens so a re-run replays instead of
//      re-publishing, and print deterministic status lines
//
// The gateway is touched only over HTTP; its private ledger is never read.

import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const duckdb = require('duckdb');

// ---------------------------------------------------------------------------
// Paths and configuration
// ---------------------------------------------------------------------------

// Anchored to this file's location (publisher/ -> app root) so the program works
// regardless of the directory it is invoked from.
const APP_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

const MANIFEST_CSV = path.join(APP_ROOT, 'fixtures', 'build_manifest.csv');
const DATABASE_FILE = path.join(APP_ROOT, 'releases.duckdb');

// keys/ is installed into the image at the app root. Overridable so the
// publisher can also be exercised outside the container, the same way the
// gateway allows CURRENT_CERT_PATH to be redirected.
const KEYS_DIR = process.env.KEYS_DIR || path.join(APP_ROOT, 'keys');
const CURRENT_KEY_PEM = path.join(KEYS_DIR, 'current', 'current.key.pem');
const CURRENT_CERT_PEM = path.join(KEYS_DIR, 'current', 'current.cert.pem');

const GATEWAY_URL = (process.env.GATEWAY_URL || 'http://127.0.0.1:7070').replace(/\/+$/, '');

// ---------------------------------------------------------------------------
// DuckDB helpers (the driver is callback-based)
// ---------------------------------------------------------------------------

function exec(connection, sql) {
  return new Promise((resolve, reject) => {
    connection.exec(sql, (err) => (err ? reject(err) : resolve()));
  });
}

function all(connection, sql, ...params) {
  return new Promise((resolve, reject) => {
    connection.all(sql, ...params, (err, rows) => (err ? reject(err) : resolve(rows)));
  });
}

function run(connection, sql, ...params) {
  return new Promise((resolve, reject) => {
    connection.run(sql, ...params, (err) => (err ? reject(err) : resolve()));
  });
}

// DuckDB returns BIGINT/HUGEINT aggregates as JS BigInt; descriptors need plain
// numbers so the JSON encodes as an integer literal rather than throwing.
function toNumber(value) {
  return typeof value === 'bigint' ? Number(value) : Number(value);
}

function sqlString(value) {
  return `'${String(value).replace(/'/g, "''")}'`;
}

// ---------------------------------------------------------------------------
// Step 1 + 2 — ingest and reconcile
// ---------------------------------------------------------------------------

async function ingestManifest(connection) {
  // Forward slashes so the same literal works on every platform DuckDB runs on.
  const csvPath = MANIFEST_CSV.split(path.sep).join('/');

  await exec(
    connection,
    `CREATE OR REPLACE TABLE manifest_raw AS
     SELECT
       CAST(entry_id      AS VARCHAR) AS entry_id,
       CAST(bundle_id     AS VARCHAR) AS bundle_id,
       CAST(component_id  AS VARCHAR) AS component_id,
       CAST(version       AS VARCHAR) AS version,
       CAST(size_bytes    AS BIGINT)  AS size_bytes,
       CAST(record_type   AS VARCHAR) AS record_type,
       CAST(supersedes_id AS VARCHAR) AS supersedes_id,
       CAST(recorded_at   AS VARCHAR) AS recorded_at
     FROM read_csv_auto(${sqlString(csvPath)}, header = true);`
  );
}

// The publishable set, derived entirely from the manifest:
//
//   deduped   - rows identical across EVERY column are one record, not two
//   withdrawn - entry_ids cancelled by a WITHDRAWAL row via supersedes_id
//   surviving - BUILD rows that were not withdrawn
//
// A bundle is publishable when at least one build survives; a bundle whose every
// build was withdrawn aggregates to nothing and drops out on its own.
const RECONCILE_SQL = `
  WITH deduped AS (
    SELECT DISTINCT * FROM manifest_raw
  ),
  withdrawn AS (
    SELECT DISTINCT supersedes_id AS entry_id
    FROM deduped
    WHERE record_type = 'WITHDRAWAL'
      AND supersedes_id IS NOT NULL
      AND supersedes_id <> ''
  ),
  surviving AS (
    SELECT d.*
    FROM deduped AS d
    WHERE d.record_type = 'BUILD'
      AND d.entry_id NOT IN (SELECT entry_id FROM withdrawn)
  )
  SELECT
    bundle_id,
    CAST(COUNT(*)         AS INTEGER) AS artifact_count,
    CAST(SUM(size_bytes)  AS BIGINT)  AS total_bytes
  FROM surviving
  GROUP BY bundle_id
  HAVING COUNT(*) > 0
  ORDER BY bundle_id;
`;

async function publishableBundles(connection) {
  const rows = await all(connection, RECONCILE_SQL);
  return rows.map((row) => ({
    bundle_id: row.bundle_id,
    artifact_count: toNumber(row.artifact_count),
    total_bytes: toNumber(row.total_bytes),
  }));
}

// ---------------------------------------------------------------------------
// Step 3 — canonical descriptor and detached CMS signature
// ---------------------------------------------------------------------------

// UTF-8 JSON, object keys sorted lexicographically, no insignificant
// whitespace. Must reproduce the gateway's own encoding byte for byte.
function canonicalEncode(value) {
  if (Array.isArray(value)) {
    return '[' + value.map(canonicalEncode).join(',') + ']';
  }
  if (value !== null && typeof value === 'object') {
    const entries = Object.keys(value)
      .sort()
      .map((key) => JSON.stringify(key) + ':' + canonicalEncode(value[key]));
    return '{' + entries.join(',') + '}';
  }
  return JSON.stringify(value);
}

function buildDescriptor(bundle) {
  return canonicalEncode({
    artifact_count: bundle.artifact_count,
    bundle_id: bundle.bundle_id,
    total_bytes: bundle.total_bytes,
  });
}

// "sha256WithRSAEncryption" -> "sha256". The gateway advertises the algorithm it
// expects; the digest is taken from there rather than assumed.
function digestFromAlgorithm(algorithm) {
  const match = /^(sha3-\d+|sha\d+|md5)/i.exec(String(algorithm || ''));
  if (!match) {
    throw new Error(`Cannot derive a digest from signing algorithm "${algorithm}".`);
  }
  return match[1].toLowerCase();
}

// Detached CMS signature (PEM) over the exact descriptor bytes, made with the key
// currently in force. `openssl cms -sign` is detached unless -nodetach is given.
function signDescriptor(descriptorBytes, digest) {
  const result = spawnSync(
    'openssl',
    [
      'cms',
      '-sign',
      '-signer', CURRENT_CERT_PEM,
      '-inkey', CURRENT_KEY_PEM,
      '-md', digest,
      '-outform', 'PEM',
      '-binary',
    ],
    { input: descriptorBytes, maxBuffer: 32 * 1024 * 1024 }
  );

  if (result.error) {
    throw new Error(`Failed to run openssl: ${result.error.message}`);
  }
  if (result.status !== 0) {
    const detail = result.stderr ? result.stderr.toString().trim() : `exit ${result.status}`;
    throw new Error(`openssl cms -sign failed: ${detail}`);
  }
  return result.stdout.toString('utf8');
}

// ---------------------------------------------------------------------------
// Step 4 — gateway calls
// ---------------------------------------------------------------------------

async function fetchSigningKey() {
  const response = await fetch(`${GATEWAY_URL}/v1/signing-key/current`);
  if (!response.ok) {
    throw new Error(`GET /v1/signing-key/current returned HTTP ${response.status}.`);
  }
  const metadata = await response.json();
  if (!metadata.key_id) {
    throw new Error('Gateway did not report a current key_id.');
  }
  return metadata;
}

async function submitPublication({ descriptor, signature, requestToken }) {
  const response = await fetch(`${GATEWAY_URL}/v1/publications`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ descriptor, signature, request_token: requestToken }),
  });

  const receipt = await response.json().catch(() => ({}));

  if (!response.ok || receipt.error) {
    // UNTRUSTED_SIGNATURE lands here: surface it instead of reporting a
    // publication that never happened.
    const reason = receipt.error || `HTTP ${response.status}`;
    const detail = receipt.message ? ` (${receipt.message})` : '';
    throw new Error(`Publication rejected: ${reason}${detail}`);
  }
  return receipt;
}

// ---------------------------------------------------------------------------
// Step 5 — local persistence / idempotency
// ---------------------------------------------------------------------------

async function ensurePublicationsTable(connection) {
  await exec(
    connection,
    `CREATE TABLE IF NOT EXISTS publications (
       bundle_id       VARCHAR PRIMARY KEY,
       request_token   VARCHAR NOT NULL,
       publication_id  VARCHAR NOT NULL,
       status          VARCHAR NOT NULL,
       descriptor      VARCHAR NOT NULL,
       signature       VARCHAR,
       key_id          VARCHAR,
       attempts        INTEGER NOT NULL DEFAULT 0,
       last_error      VARCHAR,
       published_at    TIMESTAMP
     );`
  );
}

async function loadReceipts(connection) {
  const rows = await all(
    connection,
    'SELECT bundle_id, request_token, publication_id, status, descriptor FROM publications;'
  );
  return new Map(rows.map((row) => [row.bundle_id, row]));
}

async function saveReceipt(connection, record) {
  await run(connection, 'DELETE FROM publications WHERE bundle_id = ?;', record.bundleId);
  await run(
    connection,
    `INSERT INTO publications
       (bundle_id, request_token, publication_id, status, descriptor, signature,
        key_id, attempts, last_error, published_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, now());`,
    record.bundleId,
    record.requestToken,
    record.publicationId,
    record.status,
    record.descriptor,
    record.signature,
    record.keyId,
    record.attempts
  );
}

async function recordFailure(connection, bundleId, attempts, message) {
  await run(
    connection,
    `INSERT INTO publications
       (bundle_id, request_token, publication_id, status, descriptor, signature,
        key_id, attempts, last_error, published_at)
     VALUES (?, '', '', 'FAILED', '', NULL, NULL, ?, ?, NULL)
     ON CONFLICT (bundle_id) DO UPDATE SET
       status     = 'FAILED',
       attempts   = excluded.attempts,
       last_error = excluded.last_error;`,
    bundleId,
    attempts,
    message
  );
}

// The idempotency token is derived from the bundle, so the same bundle always
// submits under the same token and the gateway replays rather than duplicating.
function requestTokenFor(bundleId) {
  return `token-${bundleId}`;
}

// ---------------------------------------------------------------------------
// Driver
// ---------------------------------------------------------------------------

async function report() {
  const signingKey = await fetchSigningKey();
  const digest = digestFromAlgorithm(signingKey.algorithm);

  const database = new duckdb.Database(DATABASE_FILE);
  const connection = database.connect();

  const lines = [];
  try {
    await ingestManifest(connection);
    await ensurePublicationsTable(connection);

    const bundles = await publishableBundles(connection);
    const stored = await loadReceipts(connection);

    for (const bundle of bundles) {
      const descriptor = buildDescriptor(bundle);
      const requestToken = requestTokenFor(bundle.bundle_id);
      const cached = stored.get(bundle.bundle_id);

      // A stored receipt for this exact descriptor is replayed locally: no
      // re-signing and no second submission.
      let receipt;
      let signature = null;
      if (cached && cached.status === 'PUBLISHED' && cached.descriptor === descriptor) {
        receipt = {
          publication_id: cached.publication_id,
          request_token: cached.request_token,
          status: cached.status,
        };
      } else {
        const attempts = (cached ? Number(cached.attempts) || 0 : 0) + 1;
        try {
          signature = signDescriptor(Buffer.from(descriptor, 'utf8'), digest);
          receipt = await submitPublication({ descriptor, signature, requestToken });
        } catch (err) {
          await recordFailure(connection, bundle.bundle_id, attempts, err.message);
          throw err;
        }
        await saveReceipt(connection, {
          bundleId: bundle.bundle_id,
          requestToken: receipt.request_token || requestToken,
          publicationId: receipt.publication_id,
          status: receipt.status,
          descriptor,
          signature,
          keyId: signingKey.key_id,
          attempts,
        });
      }

      lines.push(`BUNDLE ${bundle.bundle_id} SIGNED KEY=${signingKey.key_id}`);
      lines.push(
        `BUNDLE ${bundle.bundle_id} PUBLISHED ` +
          `RECEIPT=${receipt.publication_id} ` +
          `TOKEN=${receipt.request_token} ` +
          `STATUS=${receipt.status}`
      );
    }
  } finally {
    connection.close();
    database.close();
  }

  process.stdout.write(lines.join('\n') + (lines.length ? '\n' : ''));
}

report().catch((err) => {
  process.stderr.write(`release-publisher: ${err.message}\n`);
  process.exitCode = 1;
});
