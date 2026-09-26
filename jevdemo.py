#!/usr/bin/env python3
"""
Jev demo — pick a pre-written scenario, hit the real API, see the answers.

    python3 jevdemo.py            # http://localhost:8000
    python3 jevdemo.py --port 9000

Needs JEV_API_KEY in .env (same directory) or in the environment.
No dependencies beyond the standard library.

Scenarios live in scenarios.py next to this file. Edit that; nothing here
needs to change.
"""

import argparse
import json
import os
import re
import sys
import time
import http.client
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from scenarios import (ACCESS_RANK, ANSWER_STYLES, CALL1_QUESTIONS,
                           CALL2_QUESTIONS, CATALOG_BY_ID, DEFAULT_NETWORK,
                           IDEAS, IDEAS_BY_ID, MODELS, NETWORK, ROLES, SCENARIOS,
                           SOURCE_BY_QUESTION_KEY, SOURCE_THRESHOLD, TASK_TYPES,
                           idea_is_restricted)
except ImportError:
    sys.exit("scenarios.py not found. It should sit next to this file.")

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


def env(name):
    """Environment first, then .env next to this file."""
    val = os.environ.get(name)
    if val:
        return val.strip()
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, ".env"), ".env"):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    m = re.match(r"\s*(?:export\s+)?" + name + r"\s*=\s*(.+)", line)
                    if m:
                        return m.group(1).strip().strip("\"'")
    return None


API_KEY = None


_NET_RTT = None


def network_rtt_ms():
    """One TCP handshake to the API host, which is one round trip.

    urllib opens a fresh connection per call, so every wall time carries
    roughly four of these: TCP (1), TLS (2), and the request itself (1).
    Measured once and cached. Best effort — a failure just means the UI shows
    no estimate rather than a wrong one.
    """
    global _NET_RTT
    if _NET_RTT is not None:
        return _NET_RTT or None
    host = urllib.parse.urlparse(API_URL).hostname
    samples = []
    for _ in range(3):
        try:
            t0 = time.perf_counter()
            sock = socket.create_connection((host, 443), timeout=4)
            samples.append((time.perf_counter() - t0) * 1000)
            sock.close()
        except Exception:
            break
    _NET_RTT = sorted(samples)[len(samples) // 2] if samples else 0
    return _NET_RTT or None


# One connection, reused across calls. urllib opened a fresh one every time,
# which cost TCP + TLS on every request: about four round trips instead of one.
# Holding the connection also lets the two phases be timed apart — the
# handshake is measurably transport, and what is left is one round trip plus
# whatever the model spent.
_CONN = None
_CONN_LOCK = threading.Lock()


def _connect():
    """Open a connection and return it with the handshake cost in ms."""
    parts = urllib.parse.urlparse(API_URL)
    t0 = time.perf_counter()
    conn = http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=30)
    conn.connect()
    return conn, (time.perf_counter() - t0) * 1000


def call_api(state, questions):
    """POST to the TypeSafe API.

    Returns (response_dict, timing) where timing separates what can honestly
    be separated:

      connect_ms   TCP + TLS. Zero when the connection was reused.
      exchange_ms  request sent, response received. One round trip of
                   transport plus the model's own time.
      wall_ms      the two together.
    """
    global _CONN
    body = json.dumps({"model": MODEL, "state": state, "questions": questions}).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + API_KEY,
        "Accept": "application/json",
        # urllib's default User-Agent trips Cloudflare's bot check (403, 1010).
        # http.client sends none at all, which trips it just as reliably.
        "User-Agent": USER_AGENT,
        "Connection": "keep-alive",
    }
    path = urllib.parse.urlparse(API_URL).path or "/"

    with _CONN_LOCK:
        connect_ms = 0.0
        # A pooled connection can be closed at the far end between calls. One
        # retry on a fresh connection covers that without hiding real failures.
        for attempt in (1, 2):
            try:
                if _CONN is None:
                    _CONN, connect_ms = _connect()
                t0 = time.perf_counter()
                _CONN.request("POST", path, body=body, headers=headers)
                resp = _CONN.getresponse()
                status = resp.status
                raw = resp.read()
                exchange_ms = (time.perf_counter() - t0) * 1000
                break
            except (http.client.HTTPException, OSError) as e:
                try:
                    if _CONN:
                        _CONN.close()
                except Exception:
                    pass
                _CONN = None
                if attempt == 2:
                    raise RuntimeError("Could not reach the API: %s" % e)

        if status >= 400:
            detail = raw.decode(errors="replace")[:800]
            if "1010" in detail or "Cloudflare" in detail:
                raise RuntimeError(
                    "HTTP %s — blocked by Cloudflare (error 1010), not by the API.\n"
                    "The User-Agent was rejected. Try a different one via "
                    "JEV_USER_AGENT in .env, or run the request through curl to "
                    "confirm the key works:\n\n"
                    "  curl -sS https://api.typesafe.ai/v1/models "
                    "-H \"Authorization: Bearer $JEV_API_KEY\"" % status)
            if "authentication_error" in detail or "API key" in detail:
                raise RuntimeError(
                    "HTTP %s — the API rejected the key. Check JEV_API_KEY in .env.\n%s"
                    % (status, detail))
            raise RuntimeError("HTTP %s from the API: %s" % (status, detail))

        data = json.loads(raw.decode())

    return data, {
        "connect_ms": connect_ms,
        "exchange_ms": exchange_ms,
        "wall_ms": connect_ms + exchange_ms,
        "reused": connect_ms == 0.0,
    }


def _noul(answers, key):
    """A noul answer as a float, or None when the model did not give one."""
    a = answers.get(key)
    if isinstance(a, dict) and isinstance(a.get("noul"), (int, float)):
        return float(a["noul"])
    return None


def _score_index(answers, key, criteria):
    """A score answer as an index into `criteria`, or None."""
    a = answers.get(key)
    if not isinstance(a, dict):
        return None
    raw = a.get("score")
    if isinstance(raw, str):
        if raw in criteria:
            return criteria.index(raw)
        if raw.isdigit():
            raw = int(raw)
        else:
            return None
    if isinstance(raw, (int, float)):
        return max(0, min(int(round(raw)), len(criteria) - 1))
    return None


def _choice(answers, key):
    """A choice answer as (pick, probabilities), or (None, {})."""
    a = answers.get(key)
    if not isinstance(a, dict):
        return None, {}
    return a.get("choice"), (a.get("probabilities") or {})


def fit_models(task_type, style):
    """Narrow MODELS by what the task is and how the answer should read.

    Both filters come from the model's own answers to call 1, and neither is a
    role. A role reaches this only through answer_style_needed, which the model
    answered after reading role and skill — so the same query from two people
    can land on two different models without either being looked up by title.

    Returns (candidates, notes, rejected).
    """
    notes, rejected = [], []

    styled = [m for m in MODELS if m["answer_style"] == style] if style else list(MODELS)
    if style:
        for m in MODELS:
            if m["answer_style"] != style:
                rejected.append("%s rejected: answers as %s, not %s"
                                % (m["id"], m["answer_style"], style))
        notes.append("answer style %r -> %d of %d endpoints"
                     % (style, len(styled), len(MODELS)))
    else:
        notes.append("no answer style answered -> no style filter")

    if not task_type:
        notes.append("no task type answered -> no task filter")
        return styled, notes, rejected

    fitted = [m for m in styled if task_type in m["task_fit"]]
    if fitted:
        for m in styled:
            if task_type not in m["task_fit"]:
                rejected.append("%s rejected: fits %s, not %s"
                                % (m["id"], "/".join(m["task_fit"]), task_type))
        notes.append("task %r -> %d remain" % (task_type, len(fitted)))
        return fitted, notes, rejected

    # Style is the part the person will notice, so it is the part that is kept.
    notes.append("no exact task fit for %r; kept the answer style" % task_type)
    return styled, notes, rejected


def gate_after_call1(state, answers):
    """Everything call 1 makes decidable. No model judgment past this point.

    The classification a query needs is not asked for. It is max() over the
    classifications of the sources the model selected, which is a lookup the
    catalog can always answer correctly.
    """
    steps = []
    user = state.get("user", {}) or {}
    access = user.get("access_level", "public")

    selected, missing = [], []
    for qkey, source_id in SOURCE_BY_QUESTION_KEY.items():
        value = _noul(answers, qkey)
        if value is None:
            missing.append(source_id)
        elif value > SOURCE_THRESHOLD:
            selected.append((source_id, value))
    selected.sort(key=lambda pair: -pair[1])
    selected_ids = [sid for sid, _ in selected]

    if missing:
        steps.append("no answer for: %s  ->  treated as not needed" % ", ".join(missing))

    if selected:
        steps.append("sources above %.2f: %s" % (
            SOURCE_THRESHOLD,
            ", ".join("%s %.2f" % (sid, v) for sid, v in selected)))
    else:
        steps.append("no source above %.2f  ->  nothing to retrieve" % SOURCE_THRESHOLD)

    needed = "public"
    for sid in selected_ids:
        entry = CATALOG_BY_ID.get(sid)
        if entry and ACCESS_RANK.get(entry["classification"], 0) > ACCESS_RANK.get(needed, 0):
            needed = entry["classification"]
    if selected_ids:
        steps.append("classification = max(%s) = %s  (computed, not judged)" % (
            ", ".join(CATALOG_BY_ID[s]["classification"] for s in selected_ids if s in CATALOG_BY_ID),
            needed))

    capability = _score_index(answers, "capability_needed",
                              CALL1_QUESTIONS["capability_needed"]["criteria"])
    intent, _ = _choice(answers, "intent")
    task_type, _ = _choice(answers, "task_type")
    style, _ = _choice(answers, "answer_style_needed")

    source_regions = sorted({CATALOG_BY_ID[s]["region"]
                             for s in selected_ids if s in CATALOG_BY_ID})

    # Nothing is being retrieved, so the retrieval access gate has nothing to
    # act on. A tool that will one day handle restricted data is a review
    # requirement, not an access denial — that lands in the idea branch.
    if intent == "build":
        steps.append("intent = build  ->  no retrieval, so no access gate; "
                     "goes to the idea branch")
        return {
            "verdict": "build", "selected": selected_ids, "classification": needed,
            "capability": capability, "intent": intent, "task_type": task_type,
            "answer_style": style, "user_region": user.get("region"),
            "user_role": user.get("role"), "source_regions": source_regions,
            "steps": steps,
        }

    split = _noul(answers, "should_split")
    if split is not None and split > 0.5:
        steps.append("should_split %.2f  ->  split: an LLM rewrites this into "
                     "separate queries, each re-entering call 1" % split)
        verdict = "split"
    elif ACCESS_RANK.get(access, 0) < ACCESS_RANK.get(needed, 2):
        blockers = [s for s in selected_ids
                    if s in CATALOG_BY_ID
                    and CATALOG_BY_ID[s]["classification"] == needed]
        steps.append("rank(%s) < rank(%s) via %s  ->  block, notify data owner" % (
            access, needed, ", ".join(blockers) or "an unknown source"))
        verdict = "block"
    else:
        steps.append("rank(%s) >= rank(%s)  ->  allowed" % (access, needed))
        verdict = "pass"

    live = _noul(answers, "needs_live_data")
    if live is not None and live > 0.5:
        steps.append("live data %.2f  ->  call the feed before answering" % live)

    return {
        "verdict": verdict,
        "selected": selected_ids,
        "classification": needed,
        "capability": capability,
        "intent": intent,
        "task_type": task_type,
        "answer_style": style,
        "user_region": user.get("region"),
        "user_role": user.get("role"),
        "source_regions": source_regions,
        "steps": steps,
    }


def converge(state, answers, gate):
    """The idea branch. Everything here is a lookup or a comparison.

    The model said which idea this is closest to and whether to join, fork or
    start new. What that *requires* — a security review, a residency note — is
    read off the matched idea, not judged.
    """
    steps = []
    user = state.get("user", {}) or {}
    match, probs = _choice(answers, "idea_match")
    action, action_probs = _choice(answers, "idea_action")

    ranked = sorted(((k, v) for k, v in probs.items() if isinstance(v, (int, float))),
                    key=lambda kv: -kv[1])
    top = ranked[0] if ranked else (match, None)
    runner = ranked[1] if len(ranked) > 1 else None

    idea = IDEAS_BY_ID.get(match) if match and match != "none" else None
    if idea:
        steps.append("closest idea: %s (%s, %s, owner %s)"
                     % (idea["title"], idea["phase"], idea["region"], idea["owner"]))
    elif match == "none":
        steps.append("no existing idea is close  ->  nothing to join or fork")
    else:
        steps.append("no idea match answered")

    reviews = []
    if idea and idea_is_restricted(idea):
        reviews.append("Validate: security review required (%s)"
                       % ", ".join(idea["data_classes"]))
        steps.append("data classes %s  ->  security review"
                     % ", ".join(idea["data_classes"]))

    # Residency only matters when the person is somewhere the data may not go.
    network_note = None
    if idea and idea_is_restricted(idea):
        home, where = user.get("region"), idea["region"]
        if home and where and home != where:
            network_note = (
                "%s stays in %s; run inference in-region rather than moving the "
                "material to %s." % (", ".join(idea["data_classes"]), where, home))
            steps.append("user in %s, data must stay in %s  ->  residency note"
                         % (home, where))

    reason = {
        "join": "the same need — contribute rather than start a second effort",
        "fork": "close, but serves a need the existing idea does not",
        "new":  "not close to anything already proposed",
    }.get(action, "no recommendation answered")
    if action:
        steps.append("recommendation: %s — %s" % (action, reason))

    return {
        "match": match,
        "match_title": idea["title"] if idea else ("none" if match == "none" else None),
        "match_p": top[1] if top else None,
        "runner_up": ({"id": runner[0],
                       "title": (IDEAS_BY_ID.get(runner[0], {}) or {}).get("title", runner[0]),
                       "p": runner[1]} if runner else None),
        "idea": idea,
        "action": action,
        "action_p": (action_probs or {}).get(action),
        "reason": reason,
        "reviews": reviews,
        "network_note": network_note,
        "steps": steps,
    }


def pick_endpoint(call1, call2, network_scenario):
    """Choose where to run. Arithmetic, not judgment.

    The model supplied two constraints in call 2; capability came from call 1.
    Everything else here is RTT and cost.
    """
    steps, rejected = [], []
    net = NETWORK.get(network_scenario) or NETWORK[DEFAULT_NETWORK]
    rtt_table = net["rtt_ms"]
    congestion = net["congestion"]
    home = call1.get("user_region")
    criteria = CALL1_QUESTIONS["capability_needed"]["criteria"]

    def rtt(region):
        return (rtt_table.get(home, {}) or {}).get(region)

    def describe(model):
        ms, load = rtt(model["region"]), congestion.get(model["region"], "none")
        return "RTT %s, congestion %s" % ("%dms" % ms if ms is not None else "unknown", load)

    capability = call1.get("capability")
    if capability == 0:
        steps.append("capability tier 0 (%s)  ->  no model call, return the record"
                     % criteria[0])
        return {"chosen": None, "rejected": [], "steps": steps,
                "network": network_scenario, "skipped": True}
    if capability is None:
        capability = len(criteria) - 1
        steps.append("no capability answer  ->  assuming the top tier")

    steps.append("need capability >= %d (%s)" % (capability, criteria[capability]))

    fitted, fit_notes, fit_rejected = fit_models(call1.get("task_type"),
                                                 call1.get("answer_style"))
    steps.extend(fit_notes)
    rejected.extend(fit_rejected)

    pool = []
    for model in fitted:
        if model["capability"] < capability:
            rejected.append("%s rejected: capability %d < %d needed"
                            % (model["id"], model["capability"], capability))
        else:
            pool.append(model)

    sovereignty = _noul(call2, "sovereignty_constrained")
    if sovereignty is not None and sovereignty > 0.5:
        allowed = call1.get("source_regions") or []
        if len(allowed) > 1:
            steps.append("sovereignty %.2f but sources span %s  ->  no single region "
                         "satisfies it; a real system splits the retrieval"
                         % (sovereignty, " + ".join(allowed)))
        elif allowed:
            steps.append("sovereignty %.2f  ->  must run in %s" % (sovereignty, allowed[0]))
        keep = []
        for model in pool:
            if allowed and model["region"] not in allowed:
                rejected.append("%s rejected: in %s, data must stay in %s"
                                % (model["id"], model["region"], " or ".join(allowed)))
            else:
                keep.append(model)
        pool = keep
    elif sovereignty is not None:
        steps.append("sovereignty %.2f  ->  any region" % sovereignty)

    if not pool:
        steps.append("no endpoint satisfies every constraint  ->  escalate")
        return {"chosen": None, "rejected": rejected, "steps": steps,
                "network": network_scenario, "skipped": False}

    latency = _noul(call2, "latency_sensitive")
    by_latency = latency is not None and latency > 0.5
    if by_latency:
        steps.append("latency_sensitive %.2f  ->  rank by RTT from %s" % (latency, home))
        pool.sort(key=lambda m: (rtt(m["region"]) if rtt(m["region"]) is not None else 10 ** 6,
                                 m["cost"]))
    else:
        steps.append("not latency sensitive  ->  rank by cost")
        pool.sort(key=lambda m: (m["cost"],
                                 rtt(m["region"]) if rtt(m["region"]) is not None else 10 ** 6))

    chosen = pool[0]
    for model in pool[1:]:
        if by_latency:
            rejected.append("%s rejected: %s" % (model["id"], describe(model)))
        else:
            rejected.append("%s rejected: $%.2f vs $%.2f" % (model["id"], model["cost"], chosen["cost"]))

    steps.append("chosen: %s  (%s, %s, $%.2f%s)" % (
        chosen["id"], chosen["region"], describe(chosen), chosen["cost"],
        ", on-prem" if chosen["on_prem"] else ""))

    return {
        "chosen": dict(chosen, rtt_ms=rtt(chosen["region"]),
                       congestion=congestion.get(chosen["region"], "none")),
        "rejected": rejected,
        "steps": steps,
        "network": network_scenario,
        "skipped": False,
    }


PAGE = r"""<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Decision trace</title>
<style>
:root{--bg:#EDEFF1;--panel:#FAFBFB;--ink:#141A1E;--soft:#66727A;--faint:#A9B4BA;
--rule:#D2DADE;--sig:#1B4FD8;--sigbg:#E2E9FB;--gate:#AE5D08;--gatebg:#F8E8D2;
--pass:#0A6E5E;--passbg:#D9EDE8;--warn:#B3320A;
/* stage language, from dai_hub_two_call_decision_flow.svg:
   purple = model call, stone = ordinary code, red = stop */
--mcall:#534AB7;--mcallbg:#EEEDFE;
--ocode:#5F5E5A;--ocodebg:#F1EFE8;
--stop:#A32D2D;--stopbg:#FCEBEB;}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#111A1F;--panel:#18222A;
--ink:#E6ECEF;--soft:#93A2AB;--faint:#5A6A73;--rule:#2A3841;--sig:#7398FF;--sigbg:#1C2B4C;
--gate:#E19343;--gatebg:#382914;--pass:#4DC1A9;--passbg:#11322D;--warn:#F07A4C;
--mcall:#9D95F0;--mcallbg:#1E1B3D;
--ocode:#A09C92;--ocodebg:#232220;
--stop:#E08A8A;--stopbg:#331A1A;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 "IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif}
.mono{font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums}
.wrap{max-width:1180px;margin:0 auto;padding:22px 18px 70px}

/* command bar */
.cmd{display:flex;gap:9px;margin-bottom:12px;align-items:flex-end;flex-wrap:wrap}
.cmd .f{display:flex;flex-direction:column;gap:5px;min-width:0}
.cmd .f-q{flex:1;min-width:210px}
.cmd .fld{margin:0}
.cmd input,.cmd select,.cmd button{height:44px}
.cmd input{width:100%;background:var(--panel);color:var(--ink);
border:1px solid var(--rule);padding:0 13px;font:inherit;font-size:15px;
border-radius:3px}
.cmd input:focus{outline:2px solid var(--sig);outline-offset:-1px}
.cmd select{background:var(--panel);color:var(--ink);border:1px solid var(--rule);
padding:0 9px;font:inherit;font-size:13.5px;border-radius:3px}
.cmd select:focus{outline:2px solid var(--sig);outline-offset:-1px}
.cmd button{background:var(--ink);color:var(--bg);border:0;padding:0 22px;
font:inherit;font-weight:500;border-radius:3px;cursor:pointer}
.cmd button[disabled]{opacity:.45;cursor:default}
@media(max-width:560px){.cmd .f-q{flex:1 0 100%}.cmd button{flex:1}}

/* scenario picker — folded by default so the list can grow */
details.picker{margin-bottom:20px;padding-bottom:12px;
border-bottom:1px solid var(--rule)}
details.picker summary{cursor:pointer;list-style:none;display:flex;
align-items:baseline;gap:9px;padding:3px 0;
font-size:12px;letter-spacing:.06em;text-transform:uppercase;
color:var(--soft);font-weight:500}
details.picker summary::-webkit-details-marker{display:none}
details.picker summary::before{content:"+";font-family:"IBM Plex Mono",Menlo,monospace;
color:var(--sig);font-weight:600}
details.picker[open] summary::before{content:"\2212"}
details.picker summary:hover{color:var(--ink)}
details.picker summary:focus-visible{outline:2px solid var(--sig);outline-offset:2px}
details.picker summary .sel{text-transform:none;letter-spacing:0;color:var(--ink);
font-weight:400;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
min-width:0;flex:1}
details.picker summary .n{flex:none;text-transform:none;letter-spacing:0;
color:var(--faint);font-family:"IBM Plex Mono",Menlo,monospace}
details.picker[open] summary .sel{color:var(--faint)}

/* suggestion chips */
.chips{display:flex;flex-direction:column;align-items:stretch;gap:0;
margin-top:8px}
.chips button{display:flex;align-items:baseline;gap:10px;width:100%;
background:none;border:0;border-left:2px solid transparent;padding:5px 9px;
font:inherit;font-size:13.5px;color:var(--soft);cursor:pointer;text-align:left;
border-radius:0 3px 3px 0}
.chips button:hover{color:var(--ink);background:var(--panel)}
.chips button:focus-visible{outline:2px solid var(--sig);outline-offset:-2px}
.chips button[aria-pressed=true]{color:var(--ink);border-left-color:var(--sig);
background:var(--panel)}
.chips .cq{flex:1;min-width:0}
.chips .cn{flex:none;color:var(--faint);font-size:12px;white-space:nowrap}
.chips button[aria-pressed=true] .cn{color:var(--soft)}
@media(max-width:640px){.chips button{flex-direction:column;gap:2px}
.chips .cn{white-space:normal}}

/* trace panel */
.trace{background:var(--panel);border:1px solid var(--rule);border-radius:3px}
.thead{padding:15px 18px 13px;border-bottom:1px solid var(--rule)}
.thead h2{font-size:15px;font-weight:600;margin:0 0 5px}
.tq{color:var(--soft);font-size:14px;margin:0 0 9px}
.chain{font-size:14.5px;display:flex;flex-wrap:wrap;gap:7px;align-items:baseline}
.chain .seg{font-weight:600}
.chain .seg.ts{color:var(--sig)}
.chain .seg.code{color:var(--soft)}
.chain .arrow{color:var(--faint)}
.chain .ms{font-family:"IBM Plex Mono",Menlo,monospace}

.rhead{display:grid;grid-template-columns:62px 1fr 48px;gap:12px;
padding:8px 18px;border-bottom:1px solid var(--rule);
font-size:11px;letter-spacing:.06em;text-transform:uppercase;
color:var(--faint);font-weight:500}
.rhead span:last-child{text-align:right}
.rhead .hint{cursor:help;border-bottom:1px dotted var(--faint)}
/* the use_<source> questions are one decision, so they render as one block
   instead of five near-identical rows */
.srcs{padding:11px 18px 13px;border-bottom:1px solid var(--rule)}
.srch{display:flex;align-items:baseline;gap:9px;margin-bottom:9px}
.srch .t{font-size:11px;letter-spacing:.06em;text-transform:uppercase;
color:var(--soft);font-weight:600}
.srch .q{font-size:12px;color:var(--faint)}
.srch .n{margin-left:auto;font-family:"IBM Plex Mono",Menlo,monospace;
font-size:11.5px;color:var(--faint)}
.src{display:grid;grid-template-columns:11px minmax(0,1fr) 74px 78px 40px;
gap:11px;align-items:center;padding:3.5px 0;
font-family:"IBM Plex Mono",Menlo,monospace;font-size:12.5px}
.src .dot{width:7px;height:7px;border-radius:50%;border:1.5px solid var(--faint);
justify-self:center}
.src.on .dot{background:var(--warn);border-color:var(--warn)}
.src .id{color:var(--soft);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.src.on .id{color:var(--ink);font-weight:500}
.src .cls{font-size:10px;letter-spacing:.04em;text-align:center;padding:1.5px 0;
border-radius:2px;background:var(--bg);color:var(--faint);border:1px solid var(--rule)}
.src.on .cls{background:var(--gatebg);color:var(--gate);border-color:transparent}
.src .bar{height:5px;border-radius:3px;background:var(--rule);overflow:hidden}
.src .bar i{display:block;height:100%;background:var(--faint);border-radius:3px}
.src.on .bar i{background:var(--warn)}
.src .p{text-align:right;color:var(--soft)}
.src.on .p{color:var(--warn);font-weight:500}
.srcf{margin-top:8px;font-size:11.5px;color:var(--faint);
font-family:"IBM Plex Mono",Menlo,monospace}
@media(max-width:620px){.src{grid-template-columns:11px minmax(0,1fr) 62px 40px}
.src .bar{display:none}}

.rows{padding:0 0 5px}
.stage .rows{border-top:1px solid var(--rule)}
.r{display:grid;grid-template-columns:62px 1fr 48px;gap:12px;align-items:start;
padding:9px 18px;border-bottom:1px solid var(--rule)}
.r:last-child{border-bottom:0}
.r.low{background:var(--gatebg)}
.badge{font-size:11px;font-family:"IBM Plex Mono",Menlo,monospace;text-align:center;
padding:2px 0;border-radius:9px;background:var(--sigbg);color:var(--sig);margin-top:2px}
.badge.noul{background:var(--passbg);color:var(--pass)}
.badge.score{background:var(--gatebg);color:var(--gate)}
.qt{font-size:14.5px;margin:0 0 3px}
.dist{font-family:"IBM Plex Mono",Menlo,monospace;font-size:13px;line-height:1.75;
color:var(--faint);word-spacing:.1em}
.dist b{font-weight:500;color:var(--ink)}
.dist .v{color:var(--soft)}
.dist .hot b,.dist .hot .v{color:var(--warn)}
.conf{font-family:"IBM Plex Mono",Menlo,monospace;font-size:13px;text-align:right;
color:var(--soft);margin-top:2px}
.conf.low{color:var(--gate);font-weight:500}

/* where the time went */
.timing{padding:12px 18px 14px;border-top:1px solid var(--rule);
border-bottom:1px solid var(--rule);background:var(--bg)}
.timing .th{display:flex;align-items:baseline;gap:9px;margin-bottom:9px}
.timing .th .t{font-size:11px;letter-spacing:.06em;text-transform:uppercase;
color:var(--soft);font-weight:600}
.timing .th .tot{margin-left:auto;font-family:"IBM Plex Mono",Menlo,monospace;
font-size:14px;color:var(--ink);font-weight:500}
.tbar{display:flex;height:16px;border-radius:3px;overflow:hidden;
background:var(--rule);margin-bottom:8px}
.tbar span{display:block;min-width:2px}
.tbar .c1,.tbar .c2{background:var(--mcall)}
.tbar .c2{opacity:.62}
.tkeys{display:flex;flex-wrap:wrap;gap:6px 18px;
font-family:"IBM Plex Mono",Menlo,monospace;font-size:12px}
.tkeys b{font-weight:500}
.tkeys .k{display:inline-flex;align-items:baseline;gap:6px;color:var(--soft)}
.tkeys .k::before{content:"";width:8px;height:8px;border-radius:2px;
background:currentColor;align-self:center}
.tkeys .k.m{color:var(--mcall)}
.tkeys .k.m2{color:var(--mcall);opacity:.62}
.tkeys .k.o{color:var(--ocode)}
.tbar .hs{background:var(--ocode);opacity:.55}
.tbar .net{background:var(--mcall);opacity:.35}
.tbar .inf{background:var(--mcall)}
.tkeys .k.hs{color:var(--ocode);opacity:.75}
.tkeys .k.net{color:var(--mcall);opacity:.55}
.tsplit{margin:10px 0 0;border-top:1px dashed var(--rule);padding-top:9px}
.tsplit .row{display:grid;grid-template-columns:56px 1fr;gap:10px;
align-items:baseline;padding:2px 0;
font-family:"IBM Plex Mono",Menlo,monospace;font-size:12px}
.tsplit .row .lbl{color:var(--soft);text-align:right}
.tsplit .row .seg{display:flex;gap:14px;flex-wrap:wrap;color:var(--faint)}
.tsplit .row .seg b{font-weight:500;color:var(--ink)}
.tnote{margin-top:9px;font-size:11.5px;color:var(--faint);line-height:1.6}
.tnote code{font-family:"IBM Plex Mono",Menlo,monospace}

/* stages — purple is a model call, stone is ordinary code, red is a stop */
.stage{border-top:1px solid var(--rule)}
.shead{padding:11px 18px 10px;display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;
border-left:3px solid var(--mcall);background:var(--mcallbg)}
.shead h3{font-size:12px;letter-spacing:.06em;text-transform:uppercase;
color:var(--mcall);font-weight:600;margin:0}
.shead .kind{font-size:10.5px;letter-spacing:.05em;text-transform:uppercase;
color:var(--mcall);opacity:.72}
.legend{padding:2px 18px 12px;display:flex;gap:15px;flex-wrap:wrap;
font-size:11px;color:var(--faint)}
.legend i{font-style:normal;display:inline-flex;align-items:center;gap:5px}
.legend i::before{content:"";width:9px;height:9px;border-radius:2px;
border:1.5px solid currentColor}
.legend .m{color:var(--mcall)}.legend .c{color:var(--ocode)}.legend .s{color:var(--stop)}
.skip{border-top:1px solid var(--rule);padding:13px 18px;color:var(--stop);
font-size:13px;background:var(--stopbg);border-left:3px solid var(--stop);
font-family:"IBM Plex Mono",Menlo,monospace}

/* question list */
/* The whole flow should be readable in one look, so the list is tight: the
   rows carry one value each and do not need air between them. */
.qlist{padding:2px 0 6px}
.qsec{padding:7px 16px 3px}
.qsec + .qsec{border-top:1px solid var(--rule)}
.qsh{font-size:10.5px;letter-spacing:.08em;color:var(--soft);font-weight:700;
margin-bottom:3px;display:flex;gap:7px;align-items:baseline}
.qsh .hint{font-weight:400;letter-spacing:0;color:var(--faint);
text-transform:none}
.qr{display:grid;grid-template-columns:44px 124px minmax(0,1fr);gap:0 10px;
align-items:baseline;padding:1px 0;line-height:1.4;
font-family:"IBM Plex Mono",Menlo,monospace;font-size:12px}
.qr .qn{color:var(--faint)}
.qr .ql{color:var(--soft)}
.qr .qv{color:var(--ink)}
.qr .qv b{font-weight:600}
.qr.unused{opacity:.45}
.qr.unused .qv b{font-weight:500}
.qr .tag{margin-left:8px;font-size:10px;letter-spacing:.05em;
color:var(--faint);border:1px solid var(--rule);border-radius:2px;padding:0 4px}
.qr.skipped .qv{color:var(--faint);font-style:normal}
.qr.pending .qn,.qr.pending .ql{opacity:.62}
.qr.pending .qv::after{content:"";display:inline-block;width:48px;height:5px;
border-radius:3px;background:var(--rule)}
.qr.pending .qv.chips::after{display:none}
/* The source chips are a fixed grid, not wrapped text. Flowing them inline
   needed a break opportunity between chips and there was none, so the row ran
   off the panel; and wrapping would put a different number on each line
   depending on the window. Three columns over two rows is the same shape every
   time. The chips drop to their own line so they get the label column's width
   as well. */
/* Every row opens. The marker sits in the Q column so the three text columns
   stay aligned whether a row is open or not. */
.qr{cursor:pointer;position:relative}
.qr:hover{background:var(--panel)}
.qr:focus-visible{outline:2px solid var(--sig);outline-offset:-2px}
.qr .qn::before{content:"\203a";display:inline-block;width:9px;
color:var(--rule);transition:transform .12s}
.qr.open .qn::before{transform:rotate(90deg);color:var(--sig)}
.qr .qd{display:none}
.qr.open .qd{display:block;grid-column:1 / -1;
padding:6px 0 8px 9px;margin-top:2px;cursor:default}
.qr.dbrow .qv{grid-column:1 / -1;grid-row:2;margin-top:2px}
.qr.dbrow.open .qd{grid-row:3}

.qd .dq{margin:0 0 5px;font-size:11px;line-height:1.5;color:var(--soft);
font-family:"IBM Plex Sans",system-ui,sans-serif;max-width:62ch}
.qd .dnote{margin:0;font-size:11px;color:var(--faint)}
.qd .dsrc + .dsrc{margin-top:8px}
.dbars{display:grid;gap:2px}
.dbar{display:grid;grid-template-columns:minmax(0,150px) 1fr 40px;gap:8px;
align-items:center;font-size:10.5px;color:var(--faint)}
.dbar .dk{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dbar .dt{height:5px;border-radius:3px;background:var(--rule);overflow:hidden}
.dbar .dt i{display:block;height:100%;background:var(--faint);border-radius:3px}
.dbar .dv{text-align:right}
.dbar.hot{color:var(--ink)}
.dbar.hot .dt i{background:var(--warn)}
.dbar.hot .dv{color:var(--warn);font-weight:600}
@media(max-width:700px){.dbar{grid-template-columns:minmax(0,1fr) 34px}
.dbar .dt{display:none}}
.qv.chips{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));
gap:1px 10px;font-size:10.5px;line-height:1.5}
.qv .chip{color:var(--faint);overflow:hidden;text-overflow:ellipsis;
white-space:nowrap}
.qv .chip b{color:var(--soft);font-weight:500}
.qv .chip.on{color:var(--ink)}
.qv .chip.on b{color:var(--warn);font-weight:600}
@media(max-width:820px){.qv.chips{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:700px){.qr{grid-template-columns:44px 1fr;gap:5px}
.qr .qv{grid-column:2}}

/* converge — the idea branch, also ordinary code */
.converge{border-top:1px solid var(--rule);padding:13px 18px;
border-left:3px solid var(--ocode);background:var(--ocodebg)}
.converge h3{font-size:12px;letter-spacing:.06em;text-transform:uppercase;
color:var(--ocode);font-weight:600;margin:0 0 10px}
.cm{border:1px solid var(--sig);background:var(--sigbg);border-radius:3px;
padding:10px 13px;margin-bottom:10px}
.cm .title{font-size:14.5px;font-weight:600;color:var(--sig)}
.cm .meta,.cm .up{display:block;margin-top:3px;font-size:12.5px;
font-family:"IBM Plex Mono",Menlo,monospace;color:var(--soft)}
.cm .up{color:var(--faint)}
.cm.none{border-color:var(--rule);background:var(--bg)}
.cm.none .title{color:var(--soft)}
.act{display:inline-block;font-family:"IBM Plex Mono",Menlo,monospace;
font-size:11px;letter-spacing:.06em;padding:2px 8px;border-radius:2px;
background:var(--sig);color:var(--panel);margin-right:8px}
.req{border-left:3px solid var(--gate);background:var(--gatebg);color:var(--gate);
padding:8px 12px;margin-bottom:6px;font-size:13px;border-radius:0 3px 3px 0}
.req.net{border-left-color:var(--mcall);background:var(--mcallbg);color:var(--mcall)}
.converge pre{margin:8px 0 0;font-family:"IBM Plex Mono",Menlo,monospace;
font-size:13px;line-height:1.7;white-space:pre-wrap}

/* routing — ordinary code */
.routing{border-top:1px solid var(--rule);padding:13px 18px;
border-left:3px solid var(--ocode);background:var(--ocodebg)}
.routing h3{font-size:12px;letter-spacing:.06em;text-transform:uppercase;
color:var(--ocode);font-weight:600;margin:0 0 10px}
.pick{border:1px solid var(--pass);background:var(--passbg);border-radius:3px;
padding:10px 13px;margin-bottom:11px}
.pick b{font-family:"IBM Plex Mono",Menlo,monospace;font-size:14.5px;color:var(--pass)}
.pick .meta{display:block;margin-top:3px;font-size:12.5px;color:var(--soft);
font-family:"IBM Plex Mono",Menlo,monospace}
.pick.none{border-color:var(--rule);background:var(--bg);color:var(--soft);
font-family:"IBM Plex Mono",Menlo,monospace;font-size:13px}
.routing pre{margin:0 0 11px;font-family:"IBM Plex Mono",Menlo,monospace;
font-size:13px;line-height:1.7;white-space:pre-wrap}
.rejh{font-size:11px;letter-spacing:.06em;text-transform:uppercase;
color:var(--faint);margin:0 0 5px;font-weight:500}
.rej{font-family:"IBM Plex Mono",Menlo,monospace;font-size:12.5px;
color:var(--faint);line-height:1.75}

/* gate — ordinary code, until it stops something */
.gate{border-top:1px solid var(--rule);padding:13px 18px;
border-left:3px solid var(--ocode);background:var(--ocodebg)}
.gate h3{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--ocode);
font-weight:600;margin:0 0 9px}
.gate pre{margin:0;font-family:"IBM Plex Mono",Menlo,monospace;font-size:13px;
line-height:1.7;white-space:pre-wrap}
.gate.block{border-left-color:var(--stop);background:var(--stopbg)}
.gate.block h3{color:var(--stop)}
.gate .verdict{display:inline-block;font-family:"IBM Plex Mono",Menlo,monospace;
font-size:10.5px;letter-spacing:.06em;padding:2px 8px;border-radius:2px;margin-left:9px}
.gate.pass .verdict{background:var(--passbg);color:var(--pass)}
.gate.split .verdict{background:var(--sigbg);color:var(--sig)}
.gate.block .verdict{background:var(--stop);color:var(--panel)}
.gate.build .verdict{background:var(--mcall);color:var(--panel)}

.err{border:1px solid var(--gate);background:var(--gatebg);padding:12px 14px;
margin-bottom:14px;white-space:pre-wrap;font-size:13.5px;border-radius:3px}
.empty{padding:40px 18px;color:var(--faint);text-align:center;font-size:14px}
/* two columns: request on the left, what came back on the right */
.split{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.15fr);
gap:14px;align-items:start}

.panel{background:var(--panel);border:1px solid var(--rule);border-radius:3px}
.phead{padding:15px 18px 13px;border-bottom:1px solid var(--rule)}
.phead h2{font-size:15px;font-weight:600;margin:0 0 5px}
.phead p{color:var(--soft);font-size:13.5px;margin:0}
.pbody{padding:14px 16px 16px}
.fld{margin:0 0 6px;font-size:12px;letter-spacing:.06em;text-transform:uppercase;
color:var(--soft);font-weight:500}
.panel textarea,.raw textarea{min-height:240px}
.panel textarea{width:100%;background:var(--bg);color:var(--ink);
border:1px solid var(--rule);border-radius:3px;padding:10px;display:block;
font-family:"IBM Plex Mono",Menlo,monospace;font-size:12.5px;line-height:1.55;
resize:vertical}
.panel textarea:focus{outline:2px solid var(--sig);outline-offset:-1px}
#state{min-height:260px}
#questions1{min-height:340px}
#questions2{min-height:200px}

/* the questions block opens to full height */
details.qs{margin-top:14px;border-top:1px solid var(--rule);padding-top:12px}
details.qs summary{cursor:pointer;list-style:none;display:flex;align-items:baseline;
gap:8px;font-size:12px;letter-spacing:.06em;text-transform:uppercase;
color:var(--soft);font-weight:500}
details.qs summary::-webkit-details-marker{display:none}
details.qs summary::before{content:"+";font-family:"IBM Plex Mono",Menlo,monospace;
color:var(--sig);font-weight:600}
details.qs[open] summary::before{content:"\2212"}
details.qs summary:hover{color:var(--ink)}
details.qs summary:focus-visible{outline:2px solid var(--sig);outline-offset:2px}
details.qs summary .n{margin-left:auto;text-transform:none;letter-spacing:0;
color:var(--faint);font-family:"IBM Plex Mono",Menlo,monospace}
details.qs .qbody{margin-top:10px}
details.raw{padding:0 0 2px}
.raw textarea{width:100%;display:block;background:var(--bg);color:var(--soft);
border:1px solid var(--rule);border-radius:3px;padding:10px;
font-family:"IBM Plex Mono",Menlo,monospace;font-size:12px;line-height:1.55;
resize:vertical;min-height:300px}
.raw textarea:focus{outline:2px solid var(--sig);outline-offset:-1px}
.raw summary .n{max-width:52%;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap}

@media(max-width:900px){.split{grid-template-columns:1fr}
#state{min-height:180px}#questions1{min-height:240px}
#questions2{min-height:160px}#rawjson{min-height:200px}}
@media(max-width:760px){
.r,.rhead{grid-template-columns:52px 1fr 42px;gap:8px;padding:9px 12px}}
</style>
</head>
<body>
<div class="wrap">

<div class="cmd">
  <div class="f f-q">
    <label class="fld" for="q">query</label>
    <input id="q" type="text" spellcheck="false" placeholder="Ask anything">
  </div>
  <div class="f">
    <label class="fld" for="clr">access level</label>
    <select id="clr">
      <option value="public">public &mdash; anyone</option>
      <option value="internal">internal &mdash; employees</option>
      <option value="restricted">restricted &mdash; approved only</option>
    </select>
  </div>
  <div class="f">
    <label class="fld" for="role">role</label>
    <select id="role">
      <option>seller</option><option>solution engineer</option>
      <option>network architect</option>
    </select>
  </div>
  <div class="f">
    <label class="fld" for="reg">region</label>
    <select id="reg">
      <option>Frankfurt</option><option>Silicon Valley</option>
      <option>Singapore</option><option>London</option>
    </select>
  </div>
  <div class="f">
    <label class="fld" for="net">network</label>
    <select id="net"></select>
  </div>
  <button id="run" type="button">Send</button>
</div>

<details class="picker" id="picker">
  <summary>scenarios<span class="sel" id="picksel"></span><span class="n" id="pickn"></span></summary>
  <div class="chips" id="chips"></div>
</details>

<div id="err"></div>

<div class="split">

  <div class="panel">
    <div class="phead">
      <h2>Request</h2>
      <p>Sent to the API exactly as written. Call 2 only goes out if the gate passes.</p>
    </div>
    <div class="pbody">
      <p class="fld">state</p>
      <textarea id="state" spellcheck="false"></textarea>
      <details class="qs" id="qs1box">
        <summary>call 1 questions <span class="n" id="qn1"></span></summary>
        <div class="qbody"><textarea id="questions1" spellcheck="false"></textarea></div>
      </details>
      <details class="qs" id="qs2box">
        <summary>call 2 questions <span class="n" id="qn2"></span></summary>
        <div class="qbody"><textarea id="questions2" spellcheck="false"></textarea></div>
      </details>
    </div>
  </div>

  <div class="trace">
    <div class="thead">
      <h2>Decision Trace</h2>
      <p class="tq" id="tq">Nothing run yet.</p>
      <div class="legend">
        <i class="m">model call</i><i class="c">ordinary code</i><i class="s">stop</i>
      </div>
    </div>

    <div class="stage qpanel" id="questions" hidden>
      <div class="shead"><h3>Questions</h3><span class="kind">model</span>
        <div class="chain" id="qhead"></div></div>
      <div class="qlist" id="qlist"></div>
    </div>
    <div class="rows" id="rows1"><div class="empty">Pick a query on the left, then press Send.</div></div>

    <div class="gate" id="gate" hidden>
      <h3>Gate &mdash; computed, not judged<span class="verdict" id="gv"></span></h3>
      <pre id="gp"></pre>
    </div>

    <div class="skip" id="skip" hidden></div>

    <div class="converge" id="converge" hidden>
      <h3>Converge &mdash; computed, not judged</h3>
      <div id="cmatch"></div>
      <div id="creq"></div>
      <pre id="csteps"></pre>
    </div>

    <div class="routing" id="routing" hidden>
      <h3>Routing &mdash; arithmetic, not judged</h3>
      <div id="chosen"></div>
      <pre id="rsteps"></pre>
      <div id="rejected"></div>
    </div>

    <div class="timing" id="timing" hidden>
      <div class="th"><span class="t">where the time went</span>
        <span class="tot" id="ttot"></span></div>
      <div class="tbar" id="tbar"></div>
      <div class="tkeys" id="tkeys"></div>
      <div class="tsplit" id="tsplit" hidden></div>
      <div class="tnote" id="tnote"></div>
      <details class="qs raw"><summary>raw API response <span class="n" id="rawn"></span></summary>
        <div class="qbody"><textarea id="rawjson" spellcheck="false" readonly></textarea></div>
      </details>
    </div>
  </div>

</div>

</div>
<script>
const SCENARIOS = __SCENARIOS__;
const NETWORKS = __NETWORKS__;
const IDEA_TITLES = __IDEA_TITLES__;
const DEFAULT_NETWORK = __DEFAULT_NETWORK__;
let cur = 0;
const $ = i => document.getElementById(i);

function esc(t){ return String(t == null ? "" : t)
  .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }

$("net").innerHTML = NETWORKS.map(n =>
  '<option value="' + esc(n.id) + '"' + (n.id === DEFAULT_NETWORK ? ' selected' : '') +
  '>' + esc(n.label) + '</option>').join("");
$("net").title = "switching this and re-running the same query is the demo";

$("chips").innerHTML = SCENARIOS.map((s,i)=>
  '<button type="button" data-i="'+i+'" aria-pressed="'+(i===0)+'">' +
  '<span class="cq">' + esc(s.state.query) + '</span>' +
  (s.note ? '<span class="cn">' + esc(s.note) + '</span>' : '') +
  '</button>').join("");
// The summary carries the selection, so a folded picker still says what is
// loaded. It stays open once opened — picking and re-picking during a demo
// should not cost an extra click each time.
function syncPicker(){
  const s = SCENARIOS[cur];
  $("picksel").textContent = s ? s.state.query : "";
  $("pickn").textContent = SCENARIOS.length + " saved";
}

$("chips").addEventListener("click", e => {
  const b = e.target.closest("button"); if(!b) return;
  cur = +b.dataset.i;
  [...$("chips").children].forEach((x,i)=>x.setAttribute("aria-pressed", i===cur));
  syncPicker();
  // Fold again once a choice is made. The summary line carries the selection,
  // so leaving the list open just pushes the request panel down.
  $("picker").open = false;
  // Load only. Sending is the user's call, so the request can be read first.
  fill();
  reset('Loaded. Press Send to run it.');
  $("tq").textContent = '"' + SCENARIOS[cur].state.query + '"';
});

function reset(msg){
  $("err").innerHTML = "";
  $("rows1").innerHTML = '<div class="empty">' + esc(msg) + '</div>';
  $("rows1").hidden = false;
  drawSkeleton();
  $("gate").hidden = true;
  $("skip").hidden = true; $("routing").hidden = true;
  $("converge").hidden = true; $("timing").hidden = true;
}

function fill(){
  $("state").value = JSON.stringify(SCENARIOS[cur].state, null, 2);
  $("questions1").value = JSON.stringify(SCENARIOS[cur].questions1, null, 2);
  $("questions2").value = JSON.stringify(SCENARIOS[cur].questions2, null, 2);
  syncFromState();
  countQuestions();
  syncPicker();
  drawSkeleton();
}

// The question list is a property of DISPLAY, not of any response, so it is
// drawn at rest too: you can see what will be asked before asking it.
function drawSkeleton(){
  let q1 = null, q2 = null;
  try{ q1 = JSON.parse($("questions1").value); }catch(e){}
  try{ q2 = JSON.parse($("questions2").value); }catch(e){}
  renderQuestions({}, q1 || {}, q2 || {});
}
function countQuestions(){
  [["questions1","qn1"],["questions2","qn2"]].forEach(([src,out])=>{
    let n = null;
    try{ n = Object.keys(JSON.parse($(src).value)).length; }catch(e){}
    $(out).textContent = n === null ? "unparsed" : n + (n === 1 ? " question" : " questions");
  });
}
function setRole(v){
  const sel = $("role");
  if(![...sel.options].some(o => o.value === v)) sel.add(new Option(v, v));
  sel.value = v;
}
function setRegion(v){
  const sel = $("reg");
  if(![...sel.options].some(o => o.value === v)) sel.add(new Option(v, v));
  sel.value = v;
}
function syncFromState(){
  try{ const s = JSON.parse($("state").value);
    $("q").value = s.query || "";
    if(s.user){ if(s.user.access_level) $("clr").value = s.user.access_level;
                if(s.user.region) setRegion(s.user.region);
                if(s.user.role) setRole(s.user.role); }
  }catch(e){}
}
function syncToState(){
  let s; try{ s = JSON.parse($("state").value); }catch(e){ return; }
  s.query = $("q").value;
  s.user = s.user || {};
  s.user.access_level = $("clr").value;
  s.user.region = $("reg").value;
  s.user.role = $("role").value;
  $("state").value = JSON.stringify(s, null, 2);
}
["q","clr","reg","role"].forEach(id=>{
  $(id).addEventListener("input", syncToState);
  $(id).addEventListener("change", syncToState);
});
$("state").addEventListener("input", syncFromState);
$("questions1").addEventListener("input", countQuestions);
$("questions2").addEventListener("input", countQuestions);
$("q").addEventListener("keydown", e => { if(e.key === "Enter") $("run").click(); });

function num(v){ return typeof v === "number" && isFinite(v) }
function n2(v){ return num(v) ? v.toFixed(2) : "–" }
function ms(v){ return num(v) ? v.toFixed(0) + "ms" : "n/a" }
function usd(v){ return num(v) ? "$" + v.toFixed(2) : "cost unknown" }

// keepOrder: a score question's criteria are a ladder, so they are shown in
// the ladder's own order. A choice question is a ranking, so it is sorted by
// probability.
function dist(pairs, keepOrder){
  const vals = pairs.map(p => p[1]).filter(num);
  const top = vals.length ? Math.max(...vals) : null;
  const list = keepOrder ? pairs : pairs.slice().sort((a,b)=>b[1]-a[1]);
  return '<div class="dist">' + list.map(([k,v])=>
    '<span class="' + (top !== null && v === top && v > 0 ? 'hot' : '') + '"><b>' +
    esc(k) + '</b> <span class="v">' + n2(v) + '</span></span>')
    .join("&nbsp;&nbsp;&middot;&nbsp;&nbsp;") + '</div>';
}

// The API returns a score distribution keyed by the criteria index. Swap the
// index for the criterion it names, so "2 0.97" reads as "restricted 0.97".
function scaleOf(qdef){
  return qdef && Array.isArray(qdef.criteria) ? qdef.criteria : null;
}
function labelledPairs(a, qdef){
  const probs = a.probabilities || {};
  const keys = Object.keys(probs);
  const scale = scaleOf(qdef);
  const indexed = keys.length > 0 && keys.every(k => /^\d+$/.test(k));
  if(a.type === "score" && scale && indexed){
    return scale.map((name, i) => [name, num(probs[i]) ? probs[i] : 0]);
  }
  return Object.entries(probs);
}
function scoreLabel(a, qdef){
  const scale = scaleOf(qdef);
  if(scale && typeof a.score === "number" && scale[a.score] !== undefined) return scale[a.score];
  if(scale && /^\d+$/.test(String(a.score)) && scale[+a.score] !== undefined) return scale[+a.score];
  return a.score;
}

// DISPLAY is the only place the trace order lives. Question numbers are
// derived from it, so adding a source to CATALOG shifts Q3-Q8 onward and
// nothing else has to change.
//
// `values` remaps a score index to a short label for display only. What the
// API is asked never changes — the criteria text is semantic input to the
// model, the label here is for the reader.
const DISPLAY = [
  {key:"intent",                  section:"gate",    label:"Answer or build"},
  {key:"should_split",            section:"gate",    label:"Split check"},
  {key:"__sources__",             section:"gate",    label:"DB check"},
  {key:"capability_needed",       section:"fit",     label:"Complexity",
   values:["lookup only","small model","mid model","big model"]},
  {key:"task_type",               section:"fit",     label:"Task type"},
  {key:"answer_style_needed",     section:"fit",     label:"Answer style"},
  {key:"needs_live_data",         section:"fit",     label:"Live check"},
  {key:"idea_match",              section:"idea",    label:"Idea match",
   labels: Object.assign({none:"none"}, IDEA_TITLES)},
  {key:"idea_action",             section:"idea",    label:"Join / fork / new"},
  {key:"latency_sensitive",       section:"network", label:"Latency sensitive"},
  {key:"sovereignty_constrained", section:"network", label:"Sovereign check"},
];

const SECTIONS = [
  {id:"gate",    title:"GATE",    hint:""},
  // Selection is three stages and no section owns it. FIT narrows the pool by
  // capability, task and style; NETWORK then constrains it by residency and
  // ranks what is left by RTT or cost; pick_endpoint takes the head of that
  // list. The hints say what each stage contributes, not what the whole
  // pipeline produces.
  //
  // Three of FIT's four questions read only the query; answer_style_needed is
  // the one that reads role and skill.
  {id:"fit",     title:"FIT",     hint:"query + who is asking \u2192 narrows the model pool"},
  {id:"idea",    title:"IDEA",    hint:""},
  {id:"network", title:"NETWORK", hint:"call 2 \u2192 constrains and ranks what is left"},
];

// A noul reads as the side that won and how sure it is: noul 0.09 is "N 0.91".
function noulCell(a){
  if(!a || !num(a.noul)) return {v:"\u2013", p:null};
  return a.noul >= 0.5 ? {v:"Y", p:a.noul} : {v:"N", p:1 - a.noul};
}
function choiceCell(a, entry){
  if(!a || a.choice == null) return {v:"\u2013", p:null};
  const labels = (entry && entry.labels) || {};
  return {v: labels[a.choice] || a.choice, p:(a.probabilities || {})[a.choice]};
}
function scoreCell(a, entry, qdef){
  if(!a) return {v:"\u2013", p:null};
  const scale = scaleOf(qdef);
  let idx = typeof a.score === "number" ? a.score
          : (scale ? scale.indexOf(a.score) : -1);
  const label = (entry.values && entry.values[idx] !== undefined) ? entry.values[idx]
              : (scale && scale[idx] !== undefined ? scale[idx] : a.score);
  const probs = a.probabilities || {};
  return {v:label, p:num(probs[idx]) ? probs[idx] : a.confidence};
}
function cellFor(a, entry, qdef){
  if(!a) return {v:"\u2013", p:null};
  if(a.type === "noul") return noulCell(a);
  if(a.type === "score") return scoreCell(a, entry, qdef);
  return choiceCell(a, entry);
}

// Two different numbers sit on each row, so name them.
const ROWS_HEAD =
  '<div class="rhead">' +
  '<span>type</span>' +
  '<span>question &middot; probability per option</span>' +
  '<span class="hint" title="How sure the model is about this one answer. ' +
  'Reported by the API for score and choice; for noul it is the distance from ' +
  '0.5, doubled.">conf</span>' +
  '</div>';

function rowFor(name, a, qdef){
  const label = qdef ? (qdef.instructions.question || qdef.instructions) : name;
  let type = a.type, pairs = [], conf = null, headline, ordered = false;
  if(type === "noul"){
    if(num(a.noul)){
      headline = a.noul >= 0.5 ? "yes" : "no";
      pairs = [["yes", a.noul], ["no", 1 - a.noul]];
      conf = Math.abs(a.noul - 0.5) * 2;
    } else {
      headline = "no answer";
    }
  } else if(type === "score"){
    headline = scoreLabel(a, qdef);
    conf = a.confidence;
    pairs = labelledPairs(a, qdef);
    ordered = true;
  } else {
    headline = a.choice;
    conf = a.confidence;
    pairs = Object.entries(a.probabilities || {});
  }
  const low = conf !== null && conf !== undefined && conf < 0.5;
  return '<div class="r' + (low ? ' low' : '') + '">' +
    '<div class="badge ' + esc(type) + '">' + esc(type) + '</div>' +
    '<div><p class="qt">' + esc(label) + '</p>' +
    (pairs.length ? dist(pairs, ordered)
                  : '<div class="dist"><span class="hot"><b>' + esc(headline) + '</b></span></div>') +
    '</div>' +
    '<div class="conf' + (low ? ' low' : '') + '">' + (conf == null ? "" : n2(conf)) + '</div></div>';
}

// The API does not report a model-side duration, so the only honest number is
// the round trip.
function chainFor(call){
  const res = (call && call.response) || {};
  const ans = res.answers || {};
  const bits = ['<span class="seg ts">round trip <span class="ms">' +
                ms(call && call.wall_ms) + '</span></span>'];
  bits.push('<span class="arrow">&middot;</span>');
  bits.push('<span class="seg code">' + Object.keys(ans).length + ' judgments, one pass</span>');
  if(res.usage && res.usage.input_tokens != null){
    bits.push('<span class="arrow">&middot;</span>');
    bits.push('<span class="seg code">' + res.usage.input_tokens + ' tok in</span>');
  }
  return bits.join("");
}

// Five "would this query need source X" rows say one thing: which sources are
// in. Rendered as one ranked block, the answer is readable at a glance and the
// threshold that drives the gate is visible.
const SOURCE_THRESHOLD = 0.5;

function sourceBlock(entries, catalog){
  const byId = {};
  (catalog || []).forEach(e => { byId[e.id] = e; });
  const rows = entries
    .map(([key, a]) => ({
      id: key.replace(/^use_/, "").replace(/_/g, "-"),
      p: num(a.noul) ? a.noul : null,
      entry: null,
    }))
    .map(r => (r.entry = byId[r.id] || null, r))
    .sort((a, b) => (b.p === null ? -1 : b.p) - (a.p === null ? -1 : a.p));

  const on = rows.filter(r => r.p !== null && r.p > SOURCE_THRESHOLD);
  const body = rows.map(r => {
    const sel = r.p !== null && r.p > SOURCE_THRESHOLD;
    const cls = r.entry ? r.entry.classification : "?";
    const what = r.entry ? r.entry.id + " — " + r.entry.what : r.id;
    return '<div class="src' + (sel ? ' on' : '') + '" title="' + esc(what) + '">' +
      '<span class="dot"></span>' +
      '<span class="id">' + esc(r.id) + '</span>' +
      '<span class="cls">' + esc(cls) + '</span>' +
      '<span class="bar"><i style="width:' + Math.round((r.p || 0) * 100) + '%"></i></span>' +
      '<span class="p">' + n2(r.p) + '</span></div>';
  }).join("");

  return '<div class="srcs">' +
    '<div class="srch"><span class="t">sources</span>' +
    '<span class="q">noul &times; ' + rows.length + ', one per catalog entry</span>' +
    '<span class="n">' + on.length + ' selected</span></div>' +
    body +
    '<div class="srcf">threshold ' + n2(SOURCE_THRESHOLD) +
    ' &middot; the gate takes max(classification) over the selected rows</div>' +
    '</div>';
}

function renderRows(el, call, qdefs, catalog){
  const ans = ((call && call.response) || {}).answers || {};
  const keys = Object.keys(ans);
  if(!keys.length){ el.innerHTML = '<div class="empty">no answers came back</div>'; return; }
  const sources = keys.filter(k => /^use_/.test(k)).map(k => [k, ans[k]]);
  const rest = keys.filter(k => !/^use_/.test(k));
  el.innerHTML =
    (sources.length ? sourceBlock(sources, catalog) : "") +
    (rest.length ? ROWS_HEAD + rest.map(k => rowFor(k, ans[k], qdefs[k])).join("") : "");
}

// The bar is the argument: two model calls fill it, and the code that makes
// the actual decisions is too small to draw. Those numbers are stated instead
// of being rounded up into a visible sliver.
function us(v){ return num(v) ? (v < 1 ? (v * 1000).toFixed(0) + "\u00b5s"
                                       : v.toFixed(v < 10 ? 2 : 0) + "ms") : "\u2013"; }

// What can be separated, and what cannot.
//
//   connect   TCP + TLS. Measured directly, only on the call that opened the
//             connection. Pure transport.
//   exchange  request out, response back. One round trip of transport plus
//             whatever the model spent.
//
// Subtracting one measured RTT from the exchange leaves an estimate of the
// model's own time. It is labelled an estimate because it is one.
function callSplit(label, c, rtt){
  if(!c) return "";
  const net = num(rtt) ? Math.min(rtt, c.exchange_ms) : null;
  const inf = net === null ? null : c.exchange_ms - net;
  const seg = [];
  if(c.connect_ms > 0) seg.push("handshake <b>" + us(c.connect_ms) + "</b>");
  else seg.push("handshake <b>reused</b>");
  if(net !== null){
    seg.push("transport <b>~" + us(net) + "</b>");
    seg.push("model <b>~" + us(inf) + "</b>");
  } else {
    seg.push("exchange <b>" + us(c.exchange_ms) + "</b>");
  }
  return '<div class="row"><span class="lbl">' + esc(label) + '</span>' +
         '<span class="seg">' + seg.join("") + '</span></div>';
}

function renderTiming(t){
  if(!t){ $("timing").hidden = true; return; }
  const c1 = num(t.call1_ms) ? t.call1_ms : 0;
  const c2 = num(t.call2_ms) ? t.call2_ms : 0;
  const span = c1 + c2;

  $("ttot").textContent = us(t.total_ms) + " total";
  // Segment the bar by what each slice actually is, not by which call it was.
  function slices(c){
    if(!c) return [];
    const net = num(t.rtt_ms) ? Math.min(t.rtt_ms, c.exchange_ms) : 0;
    const out = [];
    if(c.connect_ms > 0) out.push(["hs", c.connect_ms]);
    if(net > 0) out.push(["net", net]);
    out.push(["inf", c.exchange_ms - net]);
    return out;
  }
  const all = slices(t.call1).concat(slices(t.call2));
  $("tbar").innerHTML = span > 0
    ? (all.length
        ? all.map(([cls, v]) => '<span class="' + cls + '" style="width:' +
                                (v / span * 100) + '%"></span>').join("")
        : '<span class="c1" style="width:' + (c1 / span * 100) + '%"></span>' +
          (c2 ? '<span class="c2" style="width:' + (c2 / span * 100) + '%"></span>' : ""))
    : "";

  const keys = [];
  if(num(t.rtt_ms)){
    keys.push('<span class="k hs">handshake</span>');
    keys.push('<span class="k net">transport</span>');
    keys.push('<span class="k m">model</span>');
  } else {
    keys.push('<span class="k m">call 1 <b>' + us(t.call1_ms) + '</b></span>');
    if(c2) keys.push('<span class="k m2">call 2 <b>' + us(t.call2_ms) + '</b></span>');
  }
  keys.push('<span class="k o">gate <b>' + us(t.gate_ms) + '</b></span>');
  if(num(t.routing_ms)) keys.push('<span class="k o">routing <b>' + us(t.routing_ms) + '</b></span>');
  $("tkeys").innerHTML = keys.join("");

  const split = callSplit("call 1", t.call1, t.rtt_ms) + callSplit("call 2", t.call2, t.rtt_ms);
  $("tsplit").innerHTML = split;
  $("tsplit").hidden = !split;

  const code = (num(t.gate_ms) ? t.gate_ms : 0) + (num(t.routing_ms) ? t.routing_ms : 0);
  const share = span > 0 ? (code / (span + code) * 100) : 0;
  const bits = [];
  bits.push("Gate and routing are " + share.toFixed(4) +
            "% of the total — the bar cannot show them.");
  if(num(t.rtt_ms)){
    bits.push("The connection is reused, so only the first call pays a " +
              "handshake. One round trip to the API host measures " +
              t.rtt_ms.toFixed(0) + "ms; subtracting it from each exchange " +
              "leaves the model estimate.");
  }
  bits.push("The model figure is an estimate. The API reports token usage " +
            "but no server-side duration, so the exact split is not available " +
            "— open the raw response below to check for yourself.");
  $("tnote").innerHTML = bits.map(esc).join(" ");
  $("timing").hidden = false;
}

// The whole question list, in DISPLAY order, numbered continuously.
//
// Two kinds of absence, deliberately shown differently:
//   not used  the question WAS asked in call 1 and has an answer, but this
//             branch ignores it. Dimmed, real value shown. That it was
//             answered anyway at no extra latency is the point.
//   skipped   the question was never asked, because call 2 did not run.
//             No value, and the reason is named.
// A row shows one number: the option that won. The rest of the distribution
// is the interesting part when the model was unsure, so every row opens to
// show all of it, plus the question as it was actually asked — the list shows
// a short label, and the wording sent to the model is not the same thing.
function askedText(qdef){
  if(!qdef) return "";
  const i = qdef.instructions;
  return typeof i === "string" ? i : ((i && i.question) || "");
}

function fullPairs(a, entry, qdef){
  if(!a) return [];
  if(a.type === "noul"){
    if(!num(a.noul)) return [];
    return [["yes", a.noul], ["no", 1 - a.noul]];
  }
  if(a.type === "score"){
    const pairs = labelledPairs(a, qdef);
    // The display labels are shorter; use them when the entry defines them.
    if(entry && entry.values){
      return pairs.map((p, i) => [entry.values[i] !== undefined ? entry.values[i] : p[0], p[1]]);
    }
    return pairs;
  }
  const labels = (entry && entry.labels) || {};
  return Object.entries(a.probabilities || {})
    .map(([k, v]) => [labels[k] || k, v])
    .sort((x, y) => y[1] - x[1]);
}

function barList(pairs, winner){
  if(!pairs.length) return '<p class="dnote">no distribution returned</p>';
  const top = Math.max(...pairs.map(p => num(p[1]) ? p[1] : 0));
  return '<div class="dbars">' + pairs.map(([k, v]) => {
    const w = num(v) ? Math.max(v * 100, 0) : 0;
    const hot = num(v) && v === top && v > 0;
    return '<div class="dbar' + (hot ? ' hot' : '') + '">' +
           '<span class="dk">' + esc(k) + '</span>' +
           '<span class="dt"><i style="width:' + w + '%"></i></span>' +
           '<span class="dv">' + n2(v) + '</span></div>';
  }).join("") + '</div>';
}

function detailFor(key, a, entry, qdef){
  const asked = askedText(qdef);
  const head = asked ? '<p class="dq">' + esc(asked) + '</p>' : "";
  if(!a) return head + '<p class="dnote">not answered</p>';
  return head + barList(fullPairs(a, entry, qdef));
}

// The sources row opens to all six at once, each with its own split.
function sourcesDetail(sourceKeys, answers, q1, catalog){
  return sourceKeys.map(k => {
    const id = k.replace(/^use_/, "").replace(/_/g, "-");
    const e = catalog[id];
    const a = answers[k];
    return '<div class="dsrc"><p class="dq">' + esc(id) +
           (e ? ' \u2014 ' + esc(e.what) + ' (' + esc(e.classification) + ')' : '') +
           '</p>' + barList(fullPairs(a, null, q1[k])) + '</div>';
  }).join("");
}

function renderQuestions(out, q1, q2){
  const a1 = ((out.call1 || {}).response || {}).answers || {};
  const a2 = ((out.call2 || {}).response || {}).answers || {};
  const answers = Object.assign({}, a1, a2);
  const qdefs = Object.assign({}, q1 || {}, q2 || {});
  const verdict = (out.gate || {}).verdict;
  const isBuild = verdict === "build";
  // Before a run there are no answers, so no row is "not used" and nothing has
  // been skipped — the list shows what will be asked, with the values blank.
  const ran = !!(out.call1 || out.call2);

  // Sources are one row, numbered as the span they occupy.
  const sourceKeys = Object.keys(q1 || {}).filter(k => /^use_/.test(k));
  const catalog = {};
  ((out.state_catalog) || []).forEach(e => { catalog[e.id] = e; });

  let n = 0;
  const bySection = {};
  DISPLAY.forEach(entry => {
    const rows = bySection[entry.section] || (bySection[entry.section] = []);
    if(entry.key === "__sources__"){
      const from = n + 1, to = n + sourceKeys.length;
      n = to;
      // A source chip shows the source's own probability, not the winning
      // side's: "incident N 0.08" says how weakly it was wanted, which is the
      // useful number here. Y/N already carries which way it fell.
      if(!ran){
        rows.push({q: sourceKeys.length ? "Q" + from + "\u2013" + to : "Q" + n,
                   label: entry.label, wide: true, html:
                   sourceKeys.map(k => '<span class="chip">' +
                     esc(k.replace(/^use_/, "").replace(/_/g, "-")) +
                     '</span>').join(""),
                   detail: sourcesDetail(sourceKeys, {}, q1 || {}, catalog)});
        return;
      }
      const chips = sourceKeys.map(k => {
        const id = k.replace(/^use_/, "").replace(/_/g, "-");
        const a = answers[k];
        const p = (a && num(a.noul)) ? a.noul : null;
        const yes = p !== null && p > 0.5;
        return '<span class="chip' + (yes ? ' on' : '') + '">' +
               esc(id) + ' <b>' + (p === null ? "\u2013" : (yes ? "Y" : "N")) +
               '</b> ' + n2(p) + '</span>';
      }).join("");
      rows.push({q: sourceKeys.length ? "Q" + from + "\u2013" + to : "Q" + (n + 1),
                 label: entry.label, html: chips, wide: true,
                 detail: sourcesDetail(sourceKeys, answers, q1 || {}, catalog)});
      return;
    }
    n += 1;
    const inCall2 = entry.section === "network";
    const asked = inCall2 ? !!out.call2 : true;
    const a = answers[entry.key];
    const cell = cellFor(a, entry, qdefs[entry.key]);
    let state = "";
    if(!ran){
      rows.push({q:"Q" + n, label: entry.label, cell:{v:"", p:null}, state:"pending",
                 detail: detailFor(entry.key, null, entry, qdefs[entry.key])});
      return;
    }
    if(inCall2 && !asked){
      state = "skipped";
    } else if((isBuild && entry.section === "fit") ||
              (!isBuild && entry.section === "idea")){
      state = "unused";
    }
    rows.push({q:"Q" + n, label: entry.label, cell: cell, state: state,
               detail: detailFor(entry.key, a, entry, qdefs[entry.key])});
  });

  const reason = isBuild ? "build"
               : (verdict === "block" ? "blocked"
               : (verdict === "split" ? "split" : "no call 2"));

  const html = SECTIONS.map(sec => {
    const rows = bySection[sec.id] || [];
    if(!rows.length) return "";
    return '<div class="qsec"><div class="qsh">' + esc(sec.title) +
      (sec.hint ? '<span class="hint">(' + esc(sec.hint) + ')</span>' : '') + '</div>' +
      rows.map(r => {
        const det = r.detail
          ? '<div class="qd">' + r.detail + '</div>' : "";
        const open = ' tabindex="0" role="button" aria-expanded="false"';
        if(r.html !== undefined){
          return '<div class="qr dbrow' + (r.wide ? '' : '') + '"' + open + '>' +
                 '<span class="qn">' + esc(r.q) + '</span>' +
                 '<span class="ql">' + esc(r.label) + '</span>' +
                 '<span class="qv chips">' + r.html + '</span>' + det + '</div>';
        }
        if(r.state === "pending"){
          return '<div class="qr pending"' + open + '><span class="qn">' + esc(r.q) +
                 '</span><span class="ql">' + esc(r.label) + '</span>' +
                 '<span class="qv"></span>' + det + '</div>';
        }
        if(r.state === "skipped"){
          return '<div class="qr skipped"' + open + '><span class="qn">' + esc(r.q) +
                 '</span><span class="ql">' + esc(r.label) + '</span>' +
                 '<span class="qv">skipped \u2014 ' + esc(reason) + '</span>' +
                 det + '</div>';
        }
        return '<div class="qr' + (r.state === "unused" ? ' unused' : '') + '"' + open + '>' +
               '<span class="qn">' + esc(r.q) + '</span>' +
               '<span class="ql">' + esc(r.label) + '</span>' +
               '<span class="qv"><b>' + esc(r.cell.v) + '</b> ' + n2(r.cell.p) +
               (r.state === "unused" ? '<span class="tag">not used</span>' : '') +
               '</span>' + det + '</div>';
      }).join("") + '</div>';
  }).join("");

  $("qlist").innerHTML = html;
  const now = new Date();
  const hhmmss = [now.getHours(), now.getMinutes(), now.getSeconds()]
    .map(x => String(x).padStart(2, "0")).join(":");
  if(ran){
    const bits = [hhmmss, "call 1 " + ms((out.call1 || {}).wall_ms)];
    bits.push(out.call2 ? "call 2 " + ms(out.call2.wall_ms) : "call 2 " + reason);
    $("qhead").textContent = bits.join("  \u00b7  ");
  } else {
    $("qhead").textContent = DISPLAY.length + " groups \u00b7 not run yet";
  }
  $("questions").hidden = false;
  $("rows1").hidden = ran;
}

function renderConverge(c){
  if(!c){ $("converge").hidden = true; return; }
  const hasIdea = !!c.match_title && c.match_title !== "none";
  $("cmatch").innerHTML =
    '<div class="cm' + (hasIdea ? '' : ' none') + '">' +
    '<span class="title">' + esc(hasIdea ? c.match_title : "no close match") + '</span>' +
    (c.idea ? '<span class="meta">' + esc(c.idea.phase) + ' &middot; owner ' +
              esc(c.idea.owner) + ' &middot; ' + esc(c.idea.region) + ' &middot; ' +
              esc(c.idea.runtime) + ' &middot; match ' + n2(c.match_p) + '</span>' : '') +
    (c.runner_up ? '<span class="up">runner-up ' + esc(c.runner_up.title) +
                   ' ' + n2(c.runner_up.p) + '</span>' : '') +
    '</div>' +
    (c.action ? '<p><span class="act">' + esc(c.action) + '</span>' +
                esc(c.reason) + '</p>' : '');
  const reqs = (c.reviews || []).map(r => '<div class="req">' + esc(r) + '</div>');
  if(c.network_note) reqs.push('<div class="req net">' + esc(c.network_note) + '</div>');
  $("creq").innerHTML = reqs.join("");
  $("csteps").textContent = (c.steps || []).join("\n");
  $("converge").hidden = false;
}

function renderRouting(r){
  if(!r){ $("routing").hidden = true; return; }
  const c = r.chosen;
  $("chosen").innerHTML = c
    ? '<div class="pick"><b>' + esc(c.id) + '</b>' +
      '<span class="meta">' + esc(c.region) +
      (c.on_prem ? ' &middot; on-prem' : '') +
      ' &middot; capability ' + c.capability +
      ' &middot; ' + (num(c.rtt_ms) ? c.rtt_ms + 'ms' : 'RTT unknown') +
      ' &middot; congestion ' + esc(c.congestion) +
      ' &middot; ' + usd(c.cost) + '</span></div>'
    : '<div class="pick none">' + (r.skipped ? 'no model call' : 'no endpoint available') + '</div>';
  $("rsteps").textContent = (r.steps || []).join("\n");
  $("rejected").innerHTML = (r.rejected && r.rejected.length)
    ? '<p class="rejh">rejected</p>' +
      r.rejected.map(line => '<div class="rej">' + esc(line) + '</div>').join("")
    : "";
  $("routing").hidden = false;
}

$("run").addEventListener("click", async () => {
  let state, questions1, questions2;
  try{
    state = JSON.parse($("state").value);
    questions1 = JSON.parse($("questions1").value);
    questions2 = JSON.parse($("questions2").value);
  }catch(e){
    $("err").innerHTML = '<div class="err">That is not valid JSON: '+esc(e.message)+'</div>';
    return;
  }

  reset("waiting for call 1");
  $("tq").textContent = '"' + state.query + '"';
  $("rows1").innerHTML = '<div class="empty">waiting for call 1</div>';
  $("run").disabled = true; $("run").textContent = "...";

  try{
    const r = await fetch("/run", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({state, questions1, questions2, network: $("net").value})});
    const out = await r.json();
    if(out.error){
      $("err").innerHTML = '<div class="err">' + esc(out.error) + '</div>';
      $("rows1").innerHTML = '<div class="empty">no result</div>';
    } else {
      renderTiming(out.timing);
      out.state_catalog = state.catalog;
      renderQuestions(out, questions1, questions2);
      // The API's own fields, verbatim. If a server-side duration ever shows
      // up here, chainFor and renderTiming will start using it automatically.
      const rawCalls = {call_1: (out.call1 || {}).response};
      if(out.call2) rawCalls.call_2 = out.call2.response;
      $("rawjson").value = JSON.stringify(rawCalls, null, 2);
      const top = Object.keys((out.call1 || {}).response || {});
      $("rawn").textContent = top.length ? top.join(", ") : "empty";

      const g = out.gate || {};
      $("gate").className = "gate " + (g.verdict || "");
      $("gv").textContent = g.verdict || "";
      $("gp").textContent = (g.steps || []).join("\n");
      $("gate").hidden = false;

      renderConverge(out.converge);
      if(out.call2){
        renderRouting(out.routing);
      } else {
        $("skip").textContent = out.no_second_call || "call 2 was not made";
        $("skip").hidden = false;
      }
    }
  }catch(e){
    $("err").innerHTML = '<div class="err">' + esc(e.message) + '</div>';
  }
  $("run").disabled = false; $("run").textContent = "Send";
});

// Delegated once: renderQuestions replaces the list wholesale, so a listener
// per row would be rebound on every run.
function toggleRow(row){
  if(!row || !row.querySelector(".qd")) return;
  const open = row.classList.toggle("open");
  row.setAttribute("aria-expanded", open ? "true" : "false");
}
$("qlist").addEventListener("click", e => {
  if(e.target.closest(".qd")) return;   // clicks inside the detail do not close it
  toggleRow(e.target.closest(".qr"));
});
$("qlist").addEventListener("keydown", e => {
  if(e.key !== "Enter" && e.key !== " ") return;
  const row = e.target.closest(".qr");
  if(!row) return;
  e.preventDefault();
  toggleRow(row);
});

fill();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))

    def _send(self, code, body, ctype):
        raw = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self._send(404, "not found", "text/plain")
            return
        slim = [{"name": s["name"], "note": s["note"], "state": s["state"],
                 "questions1": s["questions1"], "questions2": s["questions2"]}
                for s in SCENARIOS]
        nets = [{"id": k, "label": v["label"], "note": v["note"]}
                for k, v in NETWORK.items()]
        page = (PAGE.replace("__SCENARIOS__", json.dumps(slim))
                    .replace("__NETWORKS__", json.dumps(nets))
                    .replace("__IDEA_TITLES__",
                             json.dumps({i["id"]: i["title"] for i in IDEAS}))
                    .replace("__DEFAULT_NETWORK__", json.dumps(DEFAULT_NETWORK)))
        self._send(200, page, "text/html; charset=utf-8")

    def do_POST(self):
        if self.path != "/run":
            self._send(404, "not found", "text/plain")
            return
        n = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(n).decode())
            state = req["state"]
            q1 = req.get("questions1") or CALL1_QUESTIONS
            q2 = req.get("questions2") or CALL2_QUESTIONS
            network = req.get("network") or DEFAULT_NETWORK

            data1, t1 = call_api(state, q1)
            t0 = time.perf_counter()
            gate = gate_after_call1(state, data1.get("answers", {}))
            gate_ms = (time.perf_counter() - t0) * 1000

            out = {
                "call1": {"response": data1, "wall_ms": t1["wall_ms"]},
                "gate": gate,
                "call2": None,
                "routing": None,
                "converge": None,
                "network": network,
                "timing": {
                    "call1": t1,
                    "call2": None,
                    "call1_ms": t1["wall_ms"],
                    "gate_ms": gate_ms,
                    "call2_ms": None,
                    "routing_ms": None,
                    "total_ms": t1["wall_ms"] + gate_ms,
                    # One TCP handshake to the API host. Subtracting it from a
                    # reused call's exchange leaves the model's own time.
                    "rtt_ms": network_rtt_ms(),
                },
            }

            # A build request has nothing to retrieve and nothing to route, so
            # the idea branch runs on call 1's answers alone. No call 2.
            if gate["verdict"] == "build":
                t0 = time.perf_counter()
                out["converge"] = converge(state, data1.get("answers", {}), gate)
                out["timing"]["converge_ms"] = (time.perf_counter() - t0) * 1000
                out["timing"]["total_ms"] += out["timing"]["converge_ms"]
                out["no_second_call"] = (
                    "intent is build — the idea branch runs on call 1 alone, "
                    "so call 2 was not made")
            # The second call costs money and time. A query that is blocked or
            # needs splitting has nothing left to route, so it never happens.
            elif gate["verdict"] != "pass":
                out["no_second_call"] = (
                    "verdict is %s — nothing to route, so call 2 was not made"
                    % gate["verdict"])
            else:
                data2, t2 = call_api(state, q2)
                out["call2"] = {"response": data2, "wall_ms": t2["wall_ms"]}
                t0 = time.perf_counter()
                out["routing"] = pick_endpoint(gate, data2.get("answers", {}), network)
                out["timing"]["routing_ms"] = (time.perf_counter() - t0) * 1000
                out["timing"]["call2"] = t2
                out["timing"]["call2_ms"] = t2["wall_ms"]
                out["timing"]["total_ms"] = (
                    out["timing"]["call1_ms"] + out["timing"]["gate_ms"] +
                    t2["wall_ms"] + out["timing"]["routing_ms"])
        except Exception as e:
            out = {"error": str(e)}
        self._send(200, json.dumps(out), "application/json")


def main():
    global API_KEY
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    API_KEY = env("JEV_API_KEY")
    ua = env("JEV_USER_AGENT")
    if ua:
        globals()["USER_AGENT"] = ua
    if not API_KEY:
        sys.exit("JEV_API_KEY not found. Put it in .env next to this file, or export it.")

    url = "http://localhost:%d" % args.port
    print("Jev demo on %s   (ctrl-c to stop)" % url)
    if not args.no_open:
        webbrowser.open(url)
    try:
        ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
