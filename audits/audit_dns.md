# DNS Audit — narve.ai

**Date:** 2026-05-15
**Scope:** DNSSEC, CAA, MX validation for `narve.ai`
**Resolvers used:** system default + `1.1.1.1` (Cloudflare) for cross-check

## Commands run

```
dig +short DNSKEY narve.ai
dig CAA narve.ai +noall +answer
dig MX narve.ai +noall +answer
dig DS narve.ai +noall +answer
dig +short SOA narve.ai
dig +short NS narve.ai
```

## Raw output

### DNSKEY (DNSSEC public keys at the zone)

```
$ dig +short DNSKEY narve.ai
(empty)

$ dig +short DNSKEY narve.ai @1.1.1.1
(empty)
```

### DS (DNSSEC delegation chain at parent `.ai` registry)

```
$ dig DS narve.ai +noall +answer
(empty)

$ dig DS narve.ai @1.1.1.1 +noall +answer
(empty)
```

### CAA (Certificate Authority Authorization)

```
$ dig CAA narve.ai +noall +answer
(empty)

$ dig CAA narve.ai @1.1.1.1 +noall +answer
(empty)
```

### MX (Mail Exchange)

```
$ dig MX narve.ai +noall +answer
narve.ai.   300   IN   MX   33 route1.mx.cloudflare.net.
narve.ai.   300   IN   MX   69 route3.mx.cloudflare.net.
narve.ai.   300   IN   MX   93 route2.mx.cloudflare.net.
```

### Zone metadata (context)

```
$ dig +short SOA narve.ai
dylan.ns.cloudflare.com. dns.cloudflare.com. 2402256201 10000 2400 604800 1800

$ dig +short NS narve.ai
dylan.ns.cloudflare.com.
jean.ns.cloudflare.com.
```

## Verification matrix

| Control                              | Expected                                              | Actual                                | Status |
| ------------------------------------ | ----------------------------------------------------- | ------------------------------------- | ------ |
| DNSSEC enabled at zone (DNSKEY)      | DNSKEY records present                                | none                                  | FAIL   |
| DNSSEC chain at parent (DS)          | DS record at `.ai` registry                           | none                                  | FAIL   |
| CAA pinned to Let's Encrypt          | `0 issue "letsencrypt.org"`                           | none                                  | FAIL   |
| CAA pinned to Cloudflare             | `0 issue "pki.goog"` / Cloudflare CA                  | none                                  | FAIL   |
| CAA `iodef` reporting                | `0 iodef "mailto:..."`                                | none                                  | FAIL   |
| MX routing (email-receiving)         | Cloudflare Email Routing (or other valid MX)          | 3 × `*.mx.cloudflare.net` (33/69/93)  | PASS   |
| MX priority sanity                   | distinct priorities, lowest is primary                | 33 < 69 < 93, distinct                | PASS   |
| Authoritative NS                     | Cloudflare nameservers                                | `dylan`, `jean` @ ns.cloudflare.com   | PASS   |

## Gaps

1. **DNSSEC is OFF.** No DNSKEY at the zone and no DS at the `.ai` parent.
   Domain is vulnerable to cache poisoning / response spoofing. Enable
   DNSSEC in Cloudflare dashboard (`DNS → Settings → DNSSEC → Enable`),
   then add the resulting DS record at the `.ai` registrar.
2. **No CAA records.** Any CA in the public WebPKI can issue a cert for
   `narve.ai`. Mis-issuance risk is unmitigated. Add at minimum:
   ```
   narve.ai. CAA 0 issue "letsencrypt.org"
   narve.ai. CAA 0 issue "pki.goog"            ; Cloudflare uses Google Trust Services + Let's Encrypt
   narve.ai. CAA 0 issuewild ";"               ; or pin wildcards explicitly
   narve.ai. CAA 0 iodef "mailto:security@narve.ai"
   ```
   Confirm with Cloudflare which CA(s) currently issue the edge cert
   before pinning — pinning the wrong CA breaks renewals.
3. **MX is correct and email-receiving via Cloudflare Email Routing.**
   No gap, but note there is no SPF/DKIM/DMARC check in this audit —
   those are required for outbound mail and should be verified in a
   separate pass (`dig TXT narve.ai`, `dig TXT _dmarc.narve.ai`,
   `dig TXT <selector>._domainkey.narve.ai`).
4. **TTL = 300s on MX** is fine for Cloudflare-managed records;
   not a gap, flagged only because some controls (e.g. DNSSEC rollover)
   benefit from explicit TTL planning.

## Pre-release status

**Pre-release: off-limits per instruction.** No DNS changes were made.
This audit is read-only.
