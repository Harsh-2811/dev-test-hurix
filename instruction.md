# Signing certificates — how to create and check them

The publisher signs each release with a certificate. There are two certificates:

- **current** — the good one. Use this to sign.
- **revoked** — the old one. The gateway rejects anything signed with it.

Neither one is in this repository. Private keys must never be committed to git.
You create them yourself. This page shows how.

There are two places they can live:

- **Part 1** — inside Docker. Made automatically when you build the image.
- **Part 2** — on your computer, if you want to run without Docker.

Run one command at a time. Read the result before moving to the next one.

---

# Part 1 — Inside Docker

## Step 1.1 — Check if the image exists

```
docker images fw-publisher-test
```

If you see a line with `fw-publisher-test`, the image is there. Go to step 1.3.

If you see only column headings, the image is missing. Do step 1.2 first.

## Step 1.2 — Build the image

Go into the environment folder:

```
cd environment
```

Now build:

```
docker build -f Dockerfile -t fw-publisher-test .
```

This takes a few minutes the first time.

The certificates are made during this build. You do not run any openssl command
yourself.

Go back up when it finishes:

```
cd ..
```

## Step 1.3 — Check the certificates are inside the image

```
docker run --rm fw-publisher-test ls /app/keys/current
```

You should see:

```
current.cert.pem
current.key.pem
```

Now the old one:

```
docker run --rm fw-publisher-test ls /app/keys/revoked
```

You should see:

```
revoked.cert.pem
revoked.key.pem
```

If you see `No such file or directory`, the build did not finish. Do step 1.2 again.

## Step 1.4 — Check the certificate is readable

```
docker run --rm fw-publisher-test openssl x509 -in /app/keys/current/current.cert.pem -noout -subject
```

You should see:

```
subject=CN = fw-signing-2026-current, O = ReleaseEng, C = US
```

That is all you need for Docker. Part 1 is done.

---

# Part 2 — On your computer (no Docker)

Only do this if you want to run the publisher outside Docker.

You need `openssl` installed. On Windows use **Git Bash**, not `cmd`.

## Step 2.1 — Pick a folder outside the repository

Do not put keys inside the project folder. They could get committed by accident.

This page uses `~/fw-rig`. Change it if you like.

## Step 2.2 — Check if you already made them

```
ls ~/fw-rig/keys/current
```

If you see `current.cert.pem` and `current.key.pem`, they already exist.
Skip ahead to step 2.7.

If you see `No such file or directory`, keep going.

## Step 2.3 — Make the folders

Make the folder for the good key:

```
mkdir -p ~/fw-rig/keys/current
```

Make the folder for the old key:

```
mkdir -p ~/fw-rig/keys/revoked
```

Make the folder the gateway writes into:

```
mkdir -p ~/fw-rig/gateway-data
```

## Step 2.4 — Turn off path rewriting (Git Bash only)

Git Bash rewrites text starting with `/`. That breaks the `-subj` value below.

```
export MSYS_NO_PATHCONV=1
```

Skip this on Mac or Linux.

## Step 2.5 — Create the current certificate

```
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 3650 -keyout ~/fw-rig/keys/current/current.key.pem -out ~/fw-rig/keys/current/current.cert.pem -subj "/CN=fw-signing-2026-current/O=ReleaseEng/C=US"
```

What the parts mean:

| Part | Meaning |
| --- | --- |
| `-x509` | make a certificate, not a request |
| `-newkey rsa:2048` | make a new 2048-bit RSA key |
| `-nodes` | do not put a password on the key |
| `-sha256` | use SHA-256 |
| `-days 3650` | valid for 10 years |
| `-keyout` | where to write the private key |
| `-out` | where to write the certificate |
| `-subj` | the name inside the certificate |

## Step 2.6 — Create the revoked certificate

```
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 3650 -keyout ~/fw-rig/keys/revoked/revoked.key.pem -out ~/fw-rig/keys/revoked/revoked.cert.pem -subj "/CN=fw-signing-2025-revoked/O=ReleaseEng/C=US"
```

This is the "wrong" key. You need it to prove the gateway rejects it.

---

# Part 3 — Check the certificates you just made

Run these one at a time.

## Step 3.1 — Are all four files there?

```
ls ~/fw-rig/keys/current
```

Expect `current.cert.pem` and `current.key.pem`.

```
ls ~/fw-rig/keys/revoked
```

Expect `revoked.cert.pem` and `revoked.key.pem`.

## Step 3.2 — Is the name correct?

```
openssl x509 -in ~/fw-rig/keys/current/current.cert.pem -noout -subject
```

Expect:

```
subject=CN = fw-signing-2026-current, O = ReleaseEng, C = US
```

## Step 3.3 — Has it expired?

```
openssl x509 -in ~/fw-rig/keys/current/current.cert.pem -noout -dates
```

Expect two lines. `notAfter` should be about 10 years from today.

## Step 3.4 — Is the private key valid?

```
openssl rsa -in ~/fw-rig/keys/current/current.key.pem -check -noout
```

Expect:

```
RSA key ok
```

## Step 3.5 — Do the key and certificate belong together?

This is the one people get wrong. A key and certificate from different runs will
not work together.

Get the number from the certificate:

```
openssl x509 -in ~/fw-rig/keys/current/current.cert.pem -noout -modulus | openssl md5
```

Get the number from the key:

```
openssl rsa -in ~/fw-rig/keys/current/current.key.pem -noout -modulus | openssl md5
```

**The two results must be identical.** For example both showing:

```
(stdin)= c266b7174a784c06198a68ccc5654050
```

If they differ, delete the folder and do steps 2.3 to 2.6 again.

## Step 3.6 — Does signing actually work?

Make a small test file:

```
printf '%s' 'hello' > ~/fw-rig/test.txt
```

Sign it with the current key:

```
openssl cms -sign -in ~/fw-rig/test.txt -signer ~/fw-rig/keys/current/current.cert.pem -inkey ~/fw-rig/keys/current/current.key.pem -outform PEM -binary -out ~/fw-rig/good.sig
```

No message means it worked.

Now check the signature:

```
openssl cms -verify -inform PEM -in ~/fw-rig/good.sig -content ~/fw-rig/test.txt -certfile ~/fw-rig/keys/current/current.cert.pem -CAfile ~/fw-rig/keys/current/current.cert.pem -purpose any -no_check_time -binary -out ~/fw-rig/out.txt
```

Expect:

```
Verification successful
```

Do not use `/dev/null` for `-out` on Windows. OpenSSL cannot write to it.

## Step 3.7 — Is the revoked key really rejected?

Sign the same file with the old key:

```
openssl cms -sign -in ~/fw-rig/test.txt -signer ~/fw-rig/keys/revoked/revoked.cert.pem -inkey ~/fw-rig/keys/revoked/revoked.key.pem -outform PEM -binary -out ~/fw-rig/bad.sig
```

Now check it against the **current** certificate:

```
openssl cms -verify -inform PEM -in ~/fw-rig/bad.sig -content ~/fw-rig/test.txt -certfile ~/fw-rig/keys/current/current.cert.pem -CAfile ~/fw-rig/keys/current/current.cert.pem -purpose any -no_check_time -binary -out ~/fw-rig/out.txt
```

Expect:

```
Verification failure
```

**Failure here is the correct result.** It proves the old key no longer works.
If this one succeeds, something is wrong — the two certificates are the same.

## Step 3.8 — Clean up the test files

```
rm -f ~/fw-rig/test.txt
```

```
rm -f ~/fw-rig/good.sig
```

```
rm -f ~/fw-rig/bad.sig
```

```
rm -f ~/fw-rig/out.txt
```

---

# Part 4 — Using them

The gateway and the publisher must use the **same** certificate. If they do not,
every submission is rejected with `UNTRUSTED_SIGNATURE`.

Tell the gateway which certificate to trust:

```
export CURRENT_CERT_PATH=~/fw-rig/keys/current/current.cert.pem
```

Tell the gateway where to save its records:

```
export GATEWAY_DATA_DIR=~/fw-rig/gateway-data
```

Start the gateway:

```
node environment/distribution-gateway/server.js
```

Leave it running. In a **second** terminal, tell the publisher where the keys are:

```
export KEYS_DIR=~/fw-rig/keys
```

Go to the environment folder:

```
cd environment
```

Run it:

```
npm run report
```

---

# Common problems

| Message | Cause | Fix |
| --- | --- | --- |
| `Can't open ...current.cert.pem for reading` | No certificates | Do Part 2 |
| `UNTRUSTED_SIGNATURE` | Gateway trusts a different certificate | Check `CURRENT_CERT_PATH` and `KEYS_DIR` point at the same pair |
| `Unable to find image 'fw-publisher-test'` | Image not built | Do step 1.2 |
| Modulus values differ in step 3.5 | Key and certificate are from different runs | Delete the folder, redo steps 2.3 to 2.6 |
| `-subj` name looks mangled | Git Bash rewrote the path | Do step 2.4 first |

# Two things to remember

The certificates inside Docker and the ones on your computer are **different**.
That is fine. Each side only has to match itself.

Rebuilding the Docker image makes brand-new certificates every time. The output of
`npm run report` does not change, because the key name it prints comes from
`environment/distribution-gateway/fixtures/current-key.json`, not from the
certificate.

**Never commit a `.pem` file to git.**
