# SEV-1 / SEV-2 — Cloudflare is having an incident

Symptoms: narve.ai unreachable or returning Cloudflare error pages
(1xxx series — "1020 Access denied", "522 Connection timed out",
"526 Invalid SSL certificate"). Origin is fine but the edge is
broken.

## First 2 minutes

**Check Cloudflare status.**

<https://www.cloudflarestatus.com/>

Look for: incidents affecting **LHR** (London) or **CDG** (Paris)
since our tunnel edges there, plus any global "Network
Performance" events.

## If Cloudflare confirms an outage

You can't fix Cloudflare. You can:

1. **Update `/status`** with "Cloudflare incident — cross-CDN
   issue, investigating." Users reading the status page see it.
2. **Post to Twitter/X** from the `@narveai` account:
   > narve.ai is currently affected by a Cloudflare incident.
   > Tracking at cloudflarestatus.com. We'll post here when it
   > clears.
3. **If the incident is > 30 min and critical** (e.g. major
   product launch during the outage) — temporarily flip the
   Cloudflare proxy off: DNS Tab → narve.ai A record → click
   the orange cloud → grey. This exposes origin IP; regions that
   aren't affected by the Cloudflare incident can reach us
   directly. Flip back to orange the moment CF recovers.

**The grey-cloud move is a last resort.** It:
* Exposes the origin IP (attackers can scan once it's public).
* Removes WAF rules (rate limiting, bot protection).
* Disables caching (origin load goes up).

## If Cloudflare says "fine" but we're broken

It's us, not them. Diagnostic order:

### Tunnel

```bash
ssh julianhabbig@100.69.44.108
systemctl status cloudflared
journalctl -u cloudflared --since "30 minutes ago" | tail -80
```

If the tunnel is down:

```bash
sudo systemctl restart cloudflared
sleep 5
systemctl status cloudflared
# Wait 20–30 s for the tunnel to re-advertise before re-testing.
curl -m 5 -sI https://narve.ai | head -1
```

### Certificate

Cloudflare Origin CA certs we issue to the tunnel expire. Check:

```bash
openssl s_client -connect narve.ai:443 -servername narve.ai < /dev/null 2>/dev/null | \
  openssl x509 -noout -dates
```

If the `notAfter` is in the past, the tunnel's origin cert has
expired. Cloudflare dashboard → SSL/TLS → Origin Server → issue
a new one, update `/etc/cloudflared/cert.pem` (or wherever our
tunnel config points), restart `cloudflared`.

### DNS

```bash
dig narve.ai
dig www.narve.ai
dig sports.narve.ai
```

All should return Cloudflare anycast IPs (104.x / 172.x). If any
subdomain returns something else, a recent DNS change regressed
— check the last few entries in Cloudflare's Audit Log.

### WAF rule misfire

A newly-deployed WAF rule can block everything if the scope is
too broad. Cloudflare dashboard → Security → Events → filter by
"last 15 minutes". If a single WAF rule accounts for most blocks,
either loosen the rule or disable it.

## Verify before clearing

```bash
curl -m 5 -sI https://narve.ai | head -1
curl -m 5 -s https://narve.ai/health | python3 -m json.tool
```

Both must return 200. A Cloudflare 5xx status page means the
tunnel or certificate is still broken even if `cloudflared` says
"connected" — the tunnel is only healthy when it's both
connected AND serving.

## Postmortem

Required for any SEV-1 (narve.ai fully unreachable). A SEV-2
(one region affected, global edge flapping) is optional but
recommended — document at least the rough timeline so the next
incident has reference.
