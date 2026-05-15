# Python Dependency CVE Audit — narve.ai gateway

**Date:** 2026-05-15
**Auditor:** `pip-audit 2.10.0` (PyPI advisory DB + OSV)
**Method:** Synchronous `pip-audit -r gateway/requirements.txt --format=columns`
**Scope:** `gateway/requirements.txt` (direct + transitive deps resolved against Python 3.11)
**Output captured:** `/tmp/pip_audit_out.txt`
**Hard constraint:** No package upgrades performed in this audit. Recommendations only.

---

## 1. Headline

| Metric | Value |
|---|---|
| Direct requirements | 22 |
| Total deps resolved (direct + transitive) | 85 |
| Packages with known CVEs | **1** |
| Total CVEs | **1** |

### Severity rollup

| Severity | Count |
|---|---|
| CRITICAL | 0 |
| HIGH | 0 |
| MEDIUM | 1 |
| LOW | 0 |
| INFO / unrated | 0 |

The gateway dependency tree is in **good shape**. The previous CVE sweep
(audit #3, 2026-04-21) closed the high-impact issues in `starlette`,
`orjson`, `cryptography`, `pillow`, `python-dotenv`, `requests`, and
`filelock`. Only one new MEDIUM-severity CVE has surfaced since: a DoS
in `python-multipart` header parsing, which is reachable from any
unauthenticated multipart upload endpoint.

---

## 2. Top 5 CRITICAL / HIGH

**None.** No CRITICAL- or HIGH-severity advisories were returned for any of
the 85 resolved packages.

The single open finding is MEDIUM severity and listed in full below.

---

## 3. Full CVE list (severity-sorted)

### 3.1 MEDIUM — `python-multipart` 0.0.26 — CVE-2026-42561

| Field | Value |
|---|---|
| Package | `python-multipart` |
| Installed version | `0.0.26` |
| Fix version | **`0.0.27`** |
| Aliases | `GHSA-pp6c-gr5w-3c5g` |
| Severity (CVSS) | MEDIUM — Denial of Service |
| Vector | Network, unauthenticated |
| Reachability | **High** — invoked by Starlette/FastAPI on any `multipart/form-data` upload route |

**Summary.** `MultipartParser` did not bound the number of part headers or
the size of an individual part header. An attacker can craft a
`multipart/form-data` body with (a) a single oversized header value, or
(b) many repeated header lines / an unterminated header block, causing
excessive CPU work in the affected parser states (`HEADER_FIELD_START`,
`HEADER_FIELD`, `HEADER_VALUE_START`, `HEADER_VALUE`,
`HEADER_VALUE_ALMOST_DONE`) before the request is rejected or completed.

**Impact on narve.ai.** Any FastAPI route accepting multipart uploads
(profile avatars, attachments, admin importers) is reachable via this
vector. The blast radius is per-worker CPU exhaustion / event-loop lag,
not RCE or data disclosure. With Cloudflare in front and request body
limits enforced at the edge, the practical risk is reduced but not
eliminated — Cloudflare body limits do not bound per-part header count
or per-header size.

**Fix.** Bump `python-multipart==0.0.26 → 0.0.27` in
`gateway/requirements.txt`. Compatibility risk is **low**: 0.0.27 is a
patch release that adds default header-count and header-size limits and
preserves the public API used by Starlette.

**Interim mitigation (if upgrade is deferred).** Tighten the body-size
limit in the gateway's Starlette middleware and at the Cloudflare WAF
layer. This reduces — but does not eliminate — the attack surface; the
parser still lacks default header limits on affected versions.

---

## 4. Fix recommendations — single-line summary

```text
python-multipart    0.0.26 → 0.0.27    (CVE-2026-42561, MEDIUM, DoS)
```

Single-package patch bump. No transitive churn expected. Recommend
landing in the next dependency-bump cycle alongside the standard
`pip-audit` smoke run.

---

## 5. Out-of-scope notes (informational)

- `orjson==3.11.6` cannot be resolved by the system Python 3.9; the
  audit was run against Python 3.11 to match production. The pinned
  version is correct for runtime; the audit-runner environment is the
  thing that needed bumping, not the requirement.
- The runner emitted a non-fatal `NotOpenSSLWarning` from `urllib3` v2
  on LibreSSL 2.8.3 (macOS system OpenSSL). Cosmetic — does not affect
  the advisory data fetched from PyPI/OSV.
- Eth-account 0.10.0 pulls a chain of `eth-*` and `pycryptodome`
  packages used only by the Polymarket SIWE connect path. No advisories
  were returned for any of them at the resolved versions.

---

## 6. Reproduction

```bash
# Requires Python 3.10+ (orjson 3.11.6 floor).
# On systems with only Python 3.9, install python@3.11:
#   brew install python@3.11
#   /opt/homebrew/bin/python3.11 -m pip install --user pip-audit

cd /Users/shocakarel/Habbig
pip-audit -r gateway/requirements.txt --format=columns 2>&1 \
  | tee /tmp/pip_audit_out.txt | tail -60
```

Expected output (verbatim):

```text
Found 1 known vulnerability in 1 package
Name             Version ID             Fix Versions
---------------- ------- -------------- ------------
python-multipart 0.0.26  CVE-2026-42561 0.0.27
```
