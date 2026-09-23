# Jev demo

A local page that sends a query to the TypeSafe API and shows, step by step,
what was judged and what was computed. The point of the demo is the split:
**the model answers questions, code makes the decisions.**

Two files, no dependencies beyond the standard library.

## Run

```bash
echo 'JEV_API_KEY=sk-...' > .env      # or export it
python3 jevdemo.py                     # opens http://localhost:8000
python3 jevdemo.py --port 9000         # different port
python3 jevdemo.py --no-open           # don't launch a browser
```

## The shape of it

Two model calls, not one, and most of what looks sequential is not.

```
call 1   query + catalog only
         should_split · use_<source> per catalog entry ·
         capability_needed · needs_live_data
   ↓
gate     ordinary Python. classification = max() over the classifications of
         the sources call 1 selected, compared against the user's access level
   ↓     blocked or split → stop here, no second call
call 2   latency_sensitive · sovereignty_constrained
   ↓
routing  ordinary Python. filter endpoints by capability and sovereignty,
         then rank by RTT or by cost
```

### What is not asked

**Classification.** The old version asked the model "what is the highest data
classification this query would need to touch?" — which is a table lookup
wearing a judgment's clothes. The catalog already carries each source's
classification. Once the model says which sources a query needs, the
classification it needs is `max()` over those. Computed, not judged, and
therefore always right instead of usually right.

**Which endpoint to use.** Picking the nearest endpoint is arithmetic on RTT.
The model supplies the semantic constraints — is this interactive, must this
data stay in region — and code applies them.

## The page

Two columns. **Left: the request** — the `state` JSON, with each call's
questions underneath in an expander. **Right: the decision trace**, in the
order things actually happened: call 1, the gate, call 2, routing. A blocked or
split query shows call 1 and the gate, then says plainly why there was no
second call.

Picking a scenario loads it but does not send it, so you can read and edit the
request first. Press **Send** (or Enter in the query box).

Each judgment row carries two different numbers:

- the **inline** figures are the probability of each option, summing to 1. A
  `score` question shows its criteria in declaration order, so a ladder reads
  as a ladder. A `choice` question is sorted by probability.
- the **right-hand** figure is `conf` — how sure the model is about that one
  answer. Reported by the API for `score` and `choice`; for `noul` there is no
  such field, so the page derives it as the distance from 0.5, doubled. Rows
  under 0.5 are dimmed.

The API does not report a model-side duration, so the timing line says **round
trip** and nothing else. Inventing a model/network split would be a guess.

## The network selector

Next to the access level. Switching it and re-running the same query is the
demo:

| Network | Silicon Valley user, latency-sensitive | Why |
|---|---|---|
| Baseline | `sonnet-sjc`, 2ms | local endpoint, nothing queued |
| SV congested | `sonnet-lon`, 138ms | SV endpoints queue ~260ms, so London is genuinely closer in time |
| APAC degraded | `sonnet-sjc`, 47ms | SV mildly queued, still the best available |

Congestion is modelled as queueing **at the destination**, added to
propagation, rather than a multiplier along the path. Distance does not change
when a metro is busy; the wait to be served does. That is also what makes a
congested local endpoint lose to a healthy remote one, which is the whole point
of the selector.

Rejected endpoints are listed under the chosen one with a one-line reason each:
`opus-sjc rejected: RTT 262ms, congestion high`.

## The scenarios

Pick one from the list under the query box; press Send to run it.

| Scenario | What it shows |
|---|---|
| Restricted financials | internal user selects `eu-finance-db` → computed `restricted` → **block**, naming the source |
| Allowed for restricted | same query, restricted user → **pass**. The gate blocks on access, not on wording. |
| Incident summary | internal user, internal source → **pass** |
| Two requests in one | `should_split` fires → **split**, no second call |
| Public lookup | `capability_needed` tier 0 → no model call at all, return the record |
| Cross-region analysis | sources in two regions; sovereignty against the nearest endpoint |

## Adding a scenario

Edit `scenarios.py`. Nothing in `jevdemo.py` needs to change.

```python
SCENARIOS.append({
    "name": "Cross-region request",
    "note": "region mismatch",          # shown dim next to the query in the list
    "state": {
        "user": {"id": "analyst@corp", "region": "Silicon Valley",
                 "access_level": "internal", "role": "capacity planner"},
        "query": "What is Frankfurt cabinet utilisation this month?",
        "catalog": CATALOG,
    },
})
```

The loop at the bottom of `scenarios.py` attaches both question sets, so a new
scenario needs no `questions` key.

Adding a catalog entry automatically adds its `use_<id>` question — the call 1
set is generated from `CATALOG` at import time, and
`SOURCE_BY_QUESTION_KEY` maps the answer back to the source.

Question types:

- `score` — pick one of an ordered list of `criteria`
- `choice` — pick one of a named set, each with a `what` and optional `not_for`
- `noul` — a single probability, read as yes above 0.5

`ACCESS_RANK`: `public` 0, `internal` 1, `restricted` 2. The clearance dropdown
spells the ladder out — *public — anyone*, *internal — employees*, *restricted
— approved only* — while still sending the bare value, so `state` and the
catalog stay on the same vocabulary.

`SOURCE_THRESHOLD` (0.5) is the line above which a `use_*` answer counts as
"we need this source".

## Scaling the source questions

One `noul` per source works to roughly 20 sources. Past that, switch to a single
`choice` over sources and take everything above a probability threshold; past
255, go hierarchical — pick a domain first, then a source within it. The full
reasoning is in the SCALE NOTE comment in `scenarios.py`.

## API contract

Request, once per call:

```json
{ "model": "jev-latest", "state": { ... }, "questions": { ... } }
```

Response, per question in `answers`:

- `score` → `{ "type": "score", "score": <index>, "confidence": ..., "probabilities": {"0": ..., "1": ...} }`
- `choice` → `{ "type": "choice", "choice": ..., "confidence": ..., "probabilities": {...} }`
- `noul` → `{ "type": "noul", "noul": 0.0–1.0 }`

Score probabilities come back keyed by criteria **index**, not by criteria
text. The page maps them back through `questions[key].criteria` before
rendering, so you see `restricted 0.97` rather than `2 0.97`.

`usage` is optional and `evaluation_time_ms` is not returned at all.

## Troubleshooting

**`HTTP 403 — blocked by Cloudflare (error 1010)`**
The default urllib User-Agent trips Cloudflare's bot check. `jevdemo.py` sends
a browser User-Agent to get around it. If it still fails, override it:

```bash
echo 'JEV_USER_AGENT=...' >> .env
```

Confirm the key itself works with curl, which is not affected:

```bash
curl -sS https://api.typesafe.ai/v1/models -H "Authorization: Bearer $JEV_API_KEY"
```

**`the API rejected the key`**
`JEV_API_KEY` is wrong or expired. Environment variable wins over `.env`.

**`JEV_API_KEY not found`**
`.env` has to sit next to `jevdemo.py`, or the variable has to be exported.

**A judgment row shows `–` or `no answer`**
The API returned that answer without a numeric value. The row renders rather
than breaking the page. A missing `use_*` answer is treated as "not needed" and
the gate says so in its steps.

**`no endpoint satisfies every constraint`**
Capability and sovereignty together ruled everything out — for example tier 3
work on data that must stay in a region with no tier 3 endpoint. The steps list
every rejection and its reason.

## Files

| File | |
|---|---|
| `jevdemo.py` | server, API calls, gate, routing, and the page (HTML/CSS/JS inline) |
| `scenarios.py` | catalog, both question sets, endpoints, network scenarios, access ranks, scenarios — edit this |
| `.env` | `JEV_API_KEY`, optionally `JEV_USER_AGENT`. Not tracked. |

`parse_manuals.py` in this directory is unrelated to the demo. It parses
Mitsubishi/CMC service manuals into a routing dataset.

## Notes

- The server binds to `127.0.0.1` only.
- The API key stays server-side. The browser talks to `/run`, never to the API.
- Call 2 costs money and time, so it only goes out when the gate passes. A
  blocked query pays for one call, not two.
- `gate_after_call1` returns `block` both for "access too low" and, implicitly,
  for a query whose sources could not be determined. The steps distinguish
  them; the verdict does not. If this gets wired into a real flow, those need
  different downstream handling.
- The split verdict stops at the trace line. A real system would have an LLM
  rewrite the query into parts, each part re-entering call 1. That rewrite is
  deliberately not implemented.
