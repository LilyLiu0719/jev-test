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
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from scenarios import (ACCESS_RANK, CALL1_QUESTIONS, CALL2_QUESTIONS,
                           CATALOG_BY_ID, DEFAULT_NETWORK, MODELS, NETWORK,
                           SCENARIOS, SOURCE_BY_QUESTION_KEY, SOURCE_THRESHOLD)
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


def call_api(state, questions):
    """POST to the TypeSafe API. Returns (response_dict, wall_ms)."""
    body = json.dumps({"model": MODEL, "state": state, "questions": questions}).encode()
    req = urllib.request.Request(API_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + API_KEY)
    req.add_header("Accept", "application/json")
    # urllib's default User-Agent trips Cloudflare's bot check (403, error 1010).
    req.add_header("User-Agent", USER_AGENT)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:800]
        if "1010" in detail or "Cloudflare" in detail:
            raise RuntimeError(
                "HTTP %s — blocked by Cloudflare (error 1010), not by the API.\n"
                "The User-Agent was rejected. Try a different one via "
                "JEV_USER_AGENT in .env, or run the request through curl to confirm "
                "the key works:\n\n"
                "  curl -sS https://api.typesafe.ai/v1/models "
                "-H \"Authorization: Bearer $JEV_API_KEY\"" % e.code)
        if "authentication_error" in detail or "API key" in detail:
            raise RuntimeError(
                "HTTP %s — the API rejected the key. Check JEV_API_KEY in .env.\n%s"
                % (e.code, detail))
        raise RuntimeError("HTTP %s from the API: %s" % (e.code, detail))
    except urllib.error.URLError as e:
        raise RuntimeError("Could not reach the API: %s" % e.reason)
    return data, (time.perf_counter() - t0) * 1000


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

    source_regions = sorted({CATALOG_BY_ID[s]["region"]
                             for s in selected_ids if s in CATALOG_BY_ID})

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
        "user_region": user.get("region"),
        "source_regions": source_regions,
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

    pool = []
    for model in MODELS:
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
.panel textarea{width:100%;background:var(--bg);color:var(--ink);
border:1px solid var(--rule);border-radius:3px;padding:10px;display:block;
font-family:"IBM Plex Mono",Menlo,monospace;font-size:12.5px;line-height:1.55;
resize:vertical}
.panel textarea:focus{outline:2px solid var(--sig);outline-offset:-1px}
#state{min-height:230px}
#questions{min-height:300px}

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

@media(max-width:900px){.split{grid-template-columns:1fr}
#state{min-height:170px}#questions{min-height:230px}}
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

    <div class="stage" id="s1">
      <div class="shead"><h3>Model call 1</h3><span class="kind">model</span><div class="chain" id="chain1"></div></div>
      <div class="rows" id="rows1"><div class="empty">Pick a query on the left, then press Send.</div></div>
    </div>

    <div class="gate" id="gate" hidden>
      <h3>Gate &mdash; computed, not judged<span class="verdict" id="gv"></span></h3>
      <pre id="gp"></pre>
    </div>

    <div class="stage" id="s2" hidden>
      <div class="shead"><h3>Model call 2</h3><span class="kind">model</span><div class="chain" id="chain2"></div></div>
      <div class="rows" id="rows2"></div>
    </div>

    <div class="skip" id="skip" hidden></div>

    <div class="routing" id="routing" hidden>
      <h3>Routing &mdash; arithmetic, not judged</h3>
      <div id="chosen"></div>
      <pre id="rsteps"></pre>
      <div id="rejected"></div>
    </div>
  </div>

</div>

</div>
<script>
const SCENARIOS = __SCENARIOS__;
const NETWORKS = __NETWORKS__;
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
  // Load only. Sending is the user's call, so the request can be read first.
  fill();
  reset('Loaded. Press Send to run it.');
  $("tq").textContent = '"' + SCENARIOS[cur].state.query + '"';
});

function reset(msg){
  $("err").innerHTML = "";
  $("chain1").innerHTML = ""; $("chain2").innerHTML = "";
  $("rows1").innerHTML = '<div class="empty">' + esc(msg) + '</div>';
  $("rows2").innerHTML = "";
  $("gate").hidden = true; $("s2").hidden = true;
  $("skip").hidden = true; $("routing").hidden = true;
}

function fill(){
  $("state").value = JSON.stringify(SCENARIOS[cur].state, null, 2);
  $("questions1").value = JSON.stringify(SCENARIOS[cur].questions1, null, 2);
  $("questions2").value = JSON.stringify(SCENARIOS[cur].questions2, null, 2);
  syncFromState();
  countQuestions();
  syncPicker();
}
function countQuestions(){
  [["questions1","qn1"],["questions2","qn2"]].forEach(([src,out])=>{
    let n = null;
    try{ n = Object.keys(JSON.parse($(src).value)).length; }catch(e){}
    $(out).textContent = n === null ? "unparsed" : n + (n === 1 ? " question" : " questions");
  });
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
                if(s.user.region) setRegion(s.user.region); }
  }catch(e){}
}
function syncToState(){
  let s; try{ s = JSON.parse($("state").value); }catch(e){ return; }
  s.query = $("q").value;
  s.user = s.user || {};
  s.user.access_level = $("clr").value;
  s.user.region = $("reg").value;
  $("state").value = JSON.stringify(s, null, 2);
}
["q","clr","reg"].forEach(id=>{
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
  $("chain1").innerHTML = '<span class="seg ts">running</span>';
  $("run").disabled = true; $("run").textContent = "...";

  try{
    const r = await fetch("/run", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({state, questions1, questions2, network: $("net").value})});
    const out = await r.json();
    if(out.error){
      $("err").innerHTML = '<div class="err">' + esc(out.error) + '</div>';
      $("chain1").innerHTML = "";
      $("rows1").innerHTML = '<div class="empty">no result</div>';
    } else {
      $("chain1").innerHTML = chainFor(out.call1);
      renderRows($("rows1"), out.call1, questions1, state.catalog);

      const g = out.gate || {};
      $("gate").className = "gate " + (g.verdict || "");
      $("gv").textContent = g.verdict || "";
      $("gp").textContent = (g.steps || []).join("\n");
      $("gate").hidden = false;

      if(out.call2){
        $("chain2").innerHTML = chainFor(out.call2);
        renderRows($("rows2"), out.call2, questions2, state.catalog);
        $("s2").hidden = false;
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

            data1, wall1 = call_api(state, q1)
            gate = gate_after_call1(state, data1.get("answers", {}))

            out = {
                "call1": {"response": data1, "wall_ms": wall1},
                "gate": gate,
                "call2": None,
                "routing": None,
                "network": network,
            }

            # The second call costs money and time. A query that is blocked or
            # needs splitting has nothing left to route, so it never happens.
            if gate["verdict"] != "pass":
                out["no_second_call"] = (
                    "verdict is %s — nothing to route, so call 2 was not made"
                    % gate["verdict"])
            else:
                data2, wall2 = call_api(state, q2)
                out["call2"] = {"response": data2, "wall_ms": wall2}
                out["routing"] = pick_endpoint(gate, data2.get("answers", {}), network)
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
