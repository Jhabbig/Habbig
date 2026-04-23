# Incident Postmortem — <brief description>

<!-- File this at postmortems/YYYY-MM-DD-<slug>.md within 48 h of any
     SEV-1 or SEV-2. Keep it honest. -->

## Summary

- **Date:** <YYYY-MM-DD>
- **Duration:** <HH:MM UTC> → <HH:MM UTC> (<total minutes>)
- **Severity:** SEV-<n>
- **Impact:** <user-facing description>
- **Users affected:** <count or %, or "all" / "Pro tier only" / etc.>
- **Revenue impact:** <$0 if unknown — err toward over-reporting>
- **Detection source:** <uptime alert / user report / self-discovery>

## Timeline

All times in UTC. Lead the list with "Alert" so the gap between
root cause and detection is visible.

- `<HH:MM>`: Root cause introduced (commit sha: `<>`, deploy time
  `<>`).
- `<HH:MM>`: Alert triggered — `<alert name>`, `<channel>`.
- `<HH:MM>`: On-call ack'd.
- `<HH:MM>`: Investigation began — ran `<first diagnostic command>`.
- `<HH:MM>`: Hypothesis 1 — `<>`. Confirmed/ruled out via `<>`.
- `<HH:MM>`: Root cause identified.
- `<HH:MM>`: Mitigation deployed (`<short description>`).
- `<HH:MM>`: Verified resolved — `<smoke test that confirmed>`.
- `<HH:MM>`: Alert cleared.

## Root cause

<2–3 paragraphs. What specifically failed, and why. Link to the
code / commit / config that introduced it. If the cause was an
external dependency (Stripe, Cloudflare, Polymarket API), note
our dependency shape and what signal would have told us earlier.>

## Blast radius

<What state was wrong, for how long. Any cleanup required beyond
the code fix? Billing reconcile needed? Data corruption? User
trust?>

## Mitigation

<What we did to stop the bleeding, in the order we did it.
Include commands + the time each took. This is the "next on-call
runs this exactly" section.>

## Permanent fix

<What's in the code / config / infrastructure NOW that prevents
this exact failure from recurring. Include the commit sha(s).>

## Detection gap

<Time from root-cause introduction to alert. If > 30 min, why
didn't we see it faster? What signal should exist that doesn't?>

## What went well

- <>
- <>

## What went poorly

- <>
- <>

## Where we got lucky

<Honest list of things that could have been worse but weren't, by
accident. "The backup had run 20 min before the corruption"
counts.>

## Action items

Every action item has an owner and a due date. No "team to
investigate" — assign one person. Copy the list below verbatim
into the issue tracker; this doc is the source of truth until
they're closed.

- [ ] **<action>** — owner: `<name>`, due: `<date>`, severity:
  (blocker / important / nice-to-have)
- [ ] **<action>** — owner: `<name>`, due: `<date>`
- [ ] **<action>** — owner: `<name>`, due: `<date>`

## Review

- [ ] Shared with all contributors in Slack `#incidents`.
- [ ] Reviewed in next weekly sync (date: `<>`).
- [ ] Action items linked to GitHub issues.
- [ ] Customer-facing disclosure sent (if SEV-1 and user data
  affected): `<link>`.
- [ ] Postmortem closed after all action items complete: date
  `<>`.

## Appendix — supporting evidence

- Log excerpts: `<path or paste>`
- Graphs: `<screenshots attached>`
- DB queries used during diagnosis: `<>`
- External status pages checked: `<>`
