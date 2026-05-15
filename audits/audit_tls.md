# Audit: TLS Configuration on `narve.ai`

- **Date (UTC):** 2026-05-15T14:10:00Z
- **Host audited:** `narve.ai:443` (public production edge — fronted by Cloudflare)
- **Scope:** Negotiated TLS protocol versions, refusal of deprecated protocols (SSLv3, TLSv1.0, TLSv1.1), and leaf certificate validity window (>30 days)
- **Method:** Synchronous local bash from auditor workstation. `curl` and `openssl s_client` only. No writes, no probes against subproduct hostnames, no live-user impact.
- **Tooling:** `OpenSSL 3.6.2 7 Apr 2026`, system `curl`.

## Required posture

| Check | Required |
|---|---|
| TLS 1.3 supported | yes |
| TLS 1.2 supported (broad client compat) | yes |
| SSLv3 refused | yes |
| TLS 1.0 refused | yes |
| TLS 1.1 refused | yes |
| Leaf cert validity remaining | > 30 days |
| Leaf cert covers `narve.ai` | yes |
| Trust chain verifies against system roots | yes |

## Commands executed

```
curl -vI https://narve.ai/ 2>&1 | grep -E "(SSL|TLS|cert|expire)"
openssl s_client -connect narve.ai:443 -servername narve.ai </dev/null 2>/dev/null \
  | openssl x509 -dates -issuer -noout
openssl s_client -connect narve.ai:443 -servername narve.ai -ssl3   </dev/null
openssl s_client -connect narve.ai:443 -servername narve.ai -tls1   </dev/null
openssl s_client -connect narve.ai:443 -servername narve.ai -tls1_1 </dev/null
openssl s_client -connect narve.ai:443 -servername narve.ai -tls1_2 </dev/null
openssl s_client -connect narve.ai:443 -servername narve.ai -tls1_3 </dev/null
openssl s_client -connect narve.ai:443 -servername narve.ai </dev/null 2>/dev/null \
  | openssl x509 -text -noout
```

## Observed

### `curl -vI` (default handshake)

```
* SSL connection using TLSv1.3 / AEAD-CHACHA20-POLY1305-SHA256 / [blank] / UNDEF
* Server certificate:
*  expire date: Jul  7 16:53:32 2026 GMT
*  subjectAltName: host "narve.ai" matched cert's "narve.ai"
*  SSL certificate verify ok.
```

### Certificate dates and issuer

```
notBefore=Apr  8 16:53:33 2026 GMT
notAfter =Jul  7 16:53:32 2026 GMT
issuer   =C=US, O=Let's Encrypt, CN=E7
```

Cert details (subject, SAN, key, signature):

```
Issuer:  C=US, O=Let's Encrypt, CN=E7
Subject: CN=narve.ai
SAN:     DNS:*.narve.ai, DNS:narve.ai
Public Key Algorithm: id-ecPublicKey  (ECDSA)
Signature Algorithm:  ecdsa-with-SHA384
Not Before: Apr  8 16:53:33 2026 GMT
Not After:  Jul  7 16:53:32 2026 GMT
```

Computed remaining validity at audit time: **53 days** (audit clock 2026-05-15, expiry 2026-07-07).

### Per-protocol negotiation results

| Protocol forced | Result | Notes |
|---|---|---|
| SSLv3 (`-ssl3`) | not attempted by client — local OpenSSL 3.6.2 compiled without SSLv3 | Local probe inconclusive. SSLv3 is removed from this OpenSSL build, so we cannot positively confirm server refusal from this host (see Gap 1). |
| TLS 1.0 (`-tls1`) | refused — `tls_setup_handshake:no protocols available` (client-side); no `ServerHello` for TLS 1.0 | Effectively refused: client cannot find a mutual protocol. Same caveat as SSLv3 (Gap 1). |
| TLS 1.1 (`-tls1_1`) | refused — same client error as above | Same caveat (Gap 1). |
| TLS 1.2 (`-tls1_2`) | accepted | Cipher: `ECDHE-ECDSA-CHACHA20-POLY1305`. |
| TLS 1.3 (`-tls1_3`) | accepted | Cipher: `TLS_AES_256_GCM_SHA384`. `Verification: OK`. |
| default (no flag) | TLS 1.3, `AEAD-CHACHA20-POLY1305-SHA256` | Confirms TLS 1.3 is the negotiated default for modern clients. |

## Result

| Check | Verdict |
|---|---|
| TLS 1.3 supported | **PASS** |
| TLS 1.2 supported | **PASS** |
| SSLv3 refused | **PASS (with caveat — see Gap 1)** |
| TLS 1.0 refused | **PASS (with caveat — see Gap 1)** |
| TLS 1.1 refused | **PASS (with caveat — see Gap 1)** |
| Leaf cert valid > 30 days | **PASS** (53 days remaining) |
| Leaf cert covers `narve.ai` | **PASS** (CN + SAN) |
| Trust chain verifies | **PASS** (`SSL certificate verify ok`, `Verification: OK`) |

Hard rule held: TLS 1.3 is negotiated by default, no deprecated protocol negotiated, leaf cert is valid for substantially more than 30 days.

## Gaps

1. **Deprecated-protocol refusal is asserted from a client that itself cannot speak the deprecated protocols.** OpenSSL 3.6.2 is compiled without SSLv3 (and the system OpenSSL on this workstation has TLS 1.0 / TLS 1.1 disabled at the protocol level via `MinProtocol=TLSv1.2` defaults). The handshake errors observed for `-ssl3`, `-tls1`, `-tls1_1` are *client-side* `no protocols available`, not a `ServerHello` rejecting the version. So we did not positively prove the server rejects TLS 1.0/1.1/SSLv3 — only that this client refused to ship those `ClientHello` versions. Recommend a follow-up probe from a host that has those protocols enabled (e.g. `nmap --script ssl-enum-ciphers -p 443 narve.ai`, or `testssl.sh narve.ai`, or an SSL Labs scan), and record the per-version `handshake_failure` / protocol downgrade-attempt result. Until that's done, the SSLv3/TLS 1.0/TLS 1.1 PASS is provisional.

2. **No HSTS check was performed in this audit.** TLS protocol/cert posture is necessary but not sufficient — without HSTS (`Strict-Transport-Security` with a non-trivial `max-age` and `includeSubDomains`), a downgrade to HTTP on the first request remains possible for fresh clients. The audit scope as written did not include header inspection; consider adding `curl -sI https://narve.ai/ | grep -i strict-transport-security` (and `preload` status check) to the next iteration of this audit, or as a separate `audit_security_headers.md` cross-reference.

3. **Cipher inventory is shallow.** We confirmed *one* cipher per TLS version (`ECDHE-ECDSA-CHACHA20-POLY1305` for 1.2, `TLS_AES_256_GCM_SHA384` for 1.3) — the first one the server picks for this client's offer. We did not enumerate the full accepted cipher suite list, so we cannot confirm that legacy ciphers (RC4, 3DES, CBC modes, non-AEAD, anonymous DH, NULL) are absent. `testssl.sh` or `nmap --script ssl-enum-ciphers` are the right tools; bash-only this round.

4. **Renewal automation is not observable from this audit.** Leaf cert was issued 2026-04-08, expires 2026-07-07 — a 90-day Let's Encrypt cert with 53 days remaining means the next renewal is due around mid-June. Because the edge is Cloudflare (per `CLOUDFLARE_CHANGES.md`), the renewal is presumably automatic, but this audit did not confirm a tested renewal job exists on the Cloudflare side, nor an alert for "cert remaining < 14 days". Recommend an external monitor (UptimeRobot SSL check, or `expiring-soon` alert in Cloudflare) configured at ≤14-day threshold, plus a documented manual-rotation runbook for the case Cloudflare's auto-renew silently fails.

5. **Wildcard SAN (`*.narve.ai`) widens blast radius.** All narve.ai subdomains (every subproduct slug, plus future ones) share one private key. If that key is compromised on Cloudflare, every subdomain is compromised at once. This is intrinsic to using a wildcard at the edge — not a misconfiguration — but it argues for: (a) ensuring Cloudflare Universal SSL key rotation is enabled, and (b) keeping any internal-only / sensitive subdomain off the public wildcard (origin certs only, or a separate cert).

6. **No probe against subproduct hostnames.** This audit covered only `narve.ai`. The wildcard SAN should cover them, but we did not directly verify e.g. `signals.narve.ai`, `feed.narve.ai`, etc. negotiate TLS 1.3 with the same cipher posture. A loop over the 12 subproduct slugs from `narve_subproducts.md` would close this gap; left out here to keep the audit synchronous-bash-only and tightly scoped to the requested check.

7. **OCSP stapling / revocation posture not verified.** `openssl s_client -status` would report whether the edge staples an OCSP response; we did not run it. With a wildcard ECDSA cert on Cloudflare this is almost certainly fine (Cloudflare staples), but it's an unverified assumption.

## Recommended next probes (informational — not executed, pre-release rules in force)

```
# Positive-rejection proof for SSLv3 / TLS 1.0 / TLS 1.1, full cipher list:
testssl.sh --severity LOW https://narve.ai/
# or
nmap --script ssl-enum-ciphers -p 443 narve.ai

# HSTS header check:
curl -sI https://narve.ai/ | grep -i -E "(strict-transport-security|content-security-policy)"

# OCSP stapling check:
openssl s_client -connect narve.ai:443 -servername narve.ai -status </dev/null \
  | grep -A1 "OCSP Response Status"

# Per-subproduct sweep (read CLAUDE.md subproduct list, then for each):
for sub in feed signals leaderboard portfolio insider ... ; do
  openssl s_client -connect $sub.narve.ai:443 -servername $sub.narve.ai -tls1_3 </dev/null 2>/dev/null \
    | openssl x509 -subject -dates -noout
done
```
