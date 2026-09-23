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
    from scenarios import CLEARANCE_RANK, SCENARIOS
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


def apply_gate(state, answers):
    """The part that is ordinary code, not a model judgment."""
    steps, verdict = [], "pass"
    clearance = state.get("user", {}).get("clearance", "public")

    conf = answers.get("confidentiality", {})
    needed = conf.get("score") if isinstance(conf, dict) else None
    if isinstance(needed, str):
        label = needed
    elif isinstance(needed, (int, float)):
        label = ["public", "internal", "restricted"][min(int(round(needed)), 2)]
    else:
        label = None

    if label is None:
        steps.append("no classification answer — cannot gate, send to manual review")
        verdict = "block"
    elif CLEARANCE_RANK.get(clearance, 0) < CLEARANCE_RANK.get(label, 2):
        steps.append("rank(%s) < rank(%s)  ->  block, notify data owner" % (clearance, label))
        verdict = "block"
    else:
        steps.append("rank(%s) >= rank(%s)  ->  cleared" % (clearance, label))

    comp = answers.get("compound", {})
    if isinstance(comp, dict) and comp.get("noul", 0) > 0.5:
        steps.append("compound %.2f  ->  split into separate queries first" % comp["noul"])
        if verdict == "pass":
            verdict = "split"

    src = answers.get("data_source", {})
    if isinstance(src, dict) and src.get("probabilities"):
        also = [k for k, v in src["probabilities"].items()
                if k != src.get("choice") and v > 0.15]
        if also:
            steps.append("second source above 0.15: %s  ->  retrieve both" % ", ".join(also))

    cap = answers.get("capability_needed", {})
    capscore = cap.get("score") if isinstance(cap, dict) else None
    if isinstance(capscore, str):
        steps.append("capability: %s" % capscore)
    elif isinstance(capscore, (int, float)):
        if capscore < 0.5:
            steps.append("capability %.2f  ->  no generation needed, return the record" % capscore)
        else:
            steps.append("capability %.2f  ->  route to a model in that tier" % capscore)

    live = answers.get("needs_live_data", {})
    if isinstance(live, dict) and live.get("noul", 0) > 0.5:
        steps.append("live data %.2f  ->  call the feed before answering" % live["noul"])

    return verdict, steps


PAGE = r"""<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jev demo</title>
<style>
:root{--bg:#EAEEF0;--panel:#F7F9FA;--ink:#141B20;--soft:#5D6A71;--rule:#C6CFD4;
--sig:#1B4FD8;--sigbg:#DCE5FA;--gate:#B0600A;--gatebg:#F7E7CF;--pass:#0B6F5F;--passbg:#D6ECE7;}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#121A1F;--panel:#1A242B;
--ink:#E5EBEE;--soft:#94A3AB;--rule:#2D3B44;--sig:#7098FF;--sigbg:#1D2C4E;--gate:#E19343;
--gatebg:#3B2B16;--pass:#4DC1A9;--passbg:#12332E;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 "IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif}
.mono{font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums}
.wrap{max-width:980px;margin:0 auto;padding:34px 20px 80px}
h1{font-size:25px;font-weight:600;letter-spacing:-.02em;margin:0 0 6px}
.sub{color:var(--soft);margin:0 0 24px}
.pick{display:flex;flex-wrap:wrap;border:1px solid var(--rule);margin-bottom:16px}
.pick button{flex:1 1 170px;text-align:left;background:var(--panel);border:0;border-right:1px solid var(--rule);
padding:11px 13px;font:inherit;color:var(--soft);cursor:pointer}
.pick button:last-child{border-right:0}
.pick button[aria-pressed=true]{background:var(--bg);color:var(--ink);box-shadow:inset 0 2px 0 var(--sig)}
.pick b{display:block;font-weight:500}
.pick i{display:block;font-style:normal;font-size:12.5px;margin-top:1px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px}
@media(max-width:760px){.cols{grid-template-columns:1fr}}
.quick{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.qfield{flex:0 0 auto}
.qfield.grow{flex:1 1 320px;min-width:0}
.quick input,.quick select{width:100%;background:var(--panel);color:var(--ink);
border:1px solid var(--rule);padding:9px 10px;font:inherit;font-size:14px}
.quick input:focus,.quick select:focus{outline:2px solid var(--sig);outline-offset:-1px}
.adv{border:1px solid var(--rule);background:var(--panel);padding:10px 13px;margin-bottom:16px}
.adv[open]{padding-bottom:2px}
.adv summary{cursor:pointer;font-size:13px;color:var(--soft)}
.adv .cols{margin-top:12px}
label{display:block;font-size:12.5px;color:var(--soft);margin-bottom:5px}
textarea{width:100%;min-height:210px;background:var(--panel);color:var(--ink);border:1px solid var(--rule);
padding:11px;font-family:"IBM Plex Mono",Menlo,monospace;font-size:12.5px;line-height:1.5;resize:vertical}
textarea:focus{outline:2px solid var(--sig);outline-offset:-1px}
.bar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:20px}
button.run{background:var(--sig);color:#fff;border:0;padding:10px 22px;font:inherit;font-weight:500;cursor:pointer;border-radius:2px}
button.run[disabled]{opacity:.5;cursor:default}
.meta{font-size:12.5px;color:var(--soft)}
.err{border:1px solid var(--gate);background:var(--gatebg);padding:12px 14px;margin-bottom:14px;white-space:pre-wrap;font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));border:1px solid var(--rule);border-bottom:0;margin-bottom:14px}
.card{border-bottom:1px solid var(--rule);border-right:1px solid var(--rule);padding:13px 15px;background:var(--panel)}
.card h3{font-size:12.5px;color:var(--soft);font-weight:400;margin:0 0 8px;line-height:1.4}
.val{font-size:18px;font-weight:600;letter-spacing:-.015em;margin:0 0 9px;display:flex;align-items:baseline;gap:8px}
.val em{font-style:normal;font-size:12.5px;font-weight:400;color:var(--soft)}
.val em.low{color:var(--gate)}
.row{display:grid;grid-template-columns:1fr 36px;gap:7px;align-items:center;font-size:12.5px;margin-bottom:3px}
.lab{position:relative;padding:1px 5px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.fill{position:absolute;left:0;top:0;bottom:0;background:var(--sigbg)}
.lab span{position:relative}
.num{text-align:right;color:var(--soft);font-size:12px}
.verdict{border:1px solid var(--rule);padding:15px;background:var(--panel);margin-bottom:14px}
.verdict.block{border-left:3px solid var(--gate);background:var(--gatebg)}
.verdict.pass{border-left:3px solid var(--pass);background:var(--passbg)}
.verdict.split{border-left:3px solid var(--sig);background:var(--sigbg)}
.verdict h2{font-size:15px;margin:0 0 8px;font-weight:600}
.verdict pre{margin:0;font-size:12.5px;white-space:pre-wrap;color:var(--ink)}
details{border:1px solid var(--rule);background:var(--panel);padding:10px 13px}
summary{cursor:pointer;font-size:13px;color:var(--soft)}
details pre{font-size:12px;overflow-x:auto;margin:10px 0 0}
</style>
</head>
<body>
<div class="wrap">
<h1>Jev demo</h1>
<p class="sub">Pick a query, send it to the API, see every judgment land at once. Edit either box before running.</p>

<div class="pick" id="pick"></div>

<div class="quick">
  <div class="qfield grow">
    <label for="q">query</label>
    <input id="q" type="text" spellcheck="false" placeholder="Ask anything">
  </div>
  <div class="qfield">
    <label for="clr">clearance</label>
    <select id="clr">
      <option>public</option><option>internal</option><option>restricted</option>
    </select>
  </div>
  <div class="qfield">
    <label for="reg">region</label>
    <select id="reg">
      <option>FRA</option><option>SJC</option><option>SIN</option><option>LON</option>
    </select>
  </div>
</div>

<details class="adv"><summary>Full request</summary>
<div class="cols">
  <div><label for="state">state</label><textarea id="state" spellcheck="false"></textarea></div>
  <div><label for="questions">questions</label><textarea id="questions" spellcheck="false"></textarea></div>
</div>
</details>

<div class="bar">
  <button class="run" id="run" type="button">Run</button>
  <span class="meta" id="meta"></span>
</div>

<div id="err"></div>
<div class="grid" id="grid" hidden></div>
<div class="verdict" id="verdict" hidden><h2 id="vh"></h2><pre id="vp" class="mono"></pre></div>
<details id="raw" hidden><summary>Raw response</summary><pre class="mono" id="rawbody"></pre></details>
</div>

<script>
const SCENARIOS = __SCENARIOS__;
let cur = 0;
const $ = i => document.getElementById(i);

$("pick").innerHTML = SCENARIOS.map((s,i)=>
  '<button type="button" data-i="'+i+'" aria-pressed="'+(i===0)+'"><b>'+s.name+'</b><i>'+s.note+'</i></button>').join("");
$("pick").addEventListener("click", e => {
  const b = e.target.closest("button"); if(!b) return;
  cur = +b.dataset.i;
  [...$("pick").children].forEach((x,i)=>x.setAttribute("aria-pressed", i===cur));
  fill();
});

function fill(){
  $("state").value = JSON.stringify(SCENARIOS[cur].state, null, 2);
  $("questions").value = JSON.stringify(SCENARIOS[cur].questions, null, 2);
  syncFromState();
  $("grid").hidden = $("verdict").hidden = $("raw").hidden = true;
  $("err").innerHTML = ""; $("meta").textContent = "";
}

/* quick fields <-> state JSON */
function syncFromState(){
  try {
    const s = JSON.parse($("state").value);
    $("q").value = s.query || "";
    if (s.user) {
      if (s.user.clearance) $("clr").value = s.user.clearance;
      if (s.user.region) $("reg").value = s.user.region;
    }
  } catch (e) { /* mid-edit, leave the quick fields alone */ }
}

function syncToState(){
  let s;
  try { s = JSON.parse($("state").value); }
  catch (e) { return; }   /* JSON is being hand-edited; don't clobber it */
  s.query = $("q").value;
  s.user = s.user || {};
  s.user.clearance = $("clr").value;
  s.user.region = $("reg").value;
  $("state").value = JSON.stringify(s, null, 2);
}

["q","clr","reg"].forEach(id => {
  $(id).addEventListener("input", syncToState);
  $(id).addEventListener("change", syncToState);
});
$("state").addEventListener("input", syncFromState);
$("q").addEventListener("keydown", e => { if (e.key === "Enter") $("run").click(); });

function pct(v){ return (v*100).toFixed(0)+"%" }

function bars(entries){
  return entries.sort((a,b)=>b[1]-a[1]).map(([k,v])=>
    '<div class="row"><div class="lab"><div class="fill" style="width:'+(v*100)+'%"></div>'+
    '<span>'+k+'</span></div><div class="num mono">'+pct(v)+'</div></div>').join("");
}

function render(a){
  if(a.type === "noul"){
    return '<p class="val">'+(a.noul>=0.5?"yes":"no")+'</p>'+bars([["probability yes", a.noul]]);
  }
  if(a.type === "score"){
    const conf = a.confidence!==undefined ?
      '<em class="'+(a.confidence<0.5?"low":"")+'">confidence '+a.confidence.toFixed(2)+'</em>' : "";
    const p = a.probabilities ? bars(Object.entries(a.probabilities)) : "";
    return '<p class="val">'+a.score+conf+'</p>'+p;
  }
  const conf = a.confidence!==undefined ?
    '<em class="'+(a.confidence<0.5?"low":"")+'">confidence '+a.confidence.toFixed(2)+'</em>' : "";
  return '<p class="val">'+a.choice+conf+'</p>'+bars(Object.entries(a.probabilities||{}));
}

$("run").addEventListener("click", async () => {
  let state, questions;
  try {
    state = JSON.parse($("state").value);
    questions = JSON.parse($("questions").value);
  } catch (e) {
    $("err").innerHTML = '<div class="err">That is not valid JSON: '+e.message+'</div>';
    return;
  }
  $("err").innerHTML = ""; $("run").disabled = true; $("run").textContent = "Running";
  $("meta").textContent = "";
  try {
    const r = await fetch("/run", {method:"POST", headers:{"Content-Type":"application/json"},
                                   body: JSON.stringify({state, questions})});
    const out = await r.json();
    if (out.error) {
      $("err").innerHTML = '<div class="err">'+out.error+'</div>';
    } else {
      const ans = out.response.answers || {};
      $("grid").innerHTML = Object.entries(ans).map(([k,a]) =>
        '<div class="card"><h3>'+(questions[k] ? (questions[k].instructions.question || questions[k].instructions) : k)+
        '</h3>'+render(a)+'</div>').join("");
      $("grid").hidden = false;
      $("verdict").className = "verdict " + out.verdict;
      $("vh").textContent = {block:"Held at the gate", pass:"Cleared", split:"Split first"}[out.verdict];
      $("vp").textContent = out.steps.join("\n");
      $("verdict").hidden = false;
      $("rawbody").textContent = JSON.stringify(out.response, null, 2);
      $("raw").hidden = false;
      const u = out.response.usage || {};
      $("meta").textContent =
        (out.response.evaluation_time_ms ? out.response.evaluation_time_ms.toFixed(0)+" ms model" : "") +
        " · " + out.wall_ms.toFixed(0) + " ms round trip · " +
        (u.input_tokens||"?") + " in / " + (u.output_tokens||"?") + " out";
    }
  } catch (e) {
    $("err").innerHTML = '<div class="err">'+e.message+'</div>';
  }
  $("run").disabled = false; $("run").textContent = "Run";
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
        slim = [{"name": s["name"], "note": s["note"],
                 "state": s["state"], "questions": s["questions"]} for s in SCENARIOS]
        page = PAGE.replace("__SCENARIOS__", json.dumps(slim))
        self._send(200, page, "text/html; charset=utf-8")

    def do_POST(self):
        if self.path != "/run":
            self._send(404, "not found", "text/plain")
            return
        n = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(n).decode())
            data, wall = call_api(req["state"], req["questions"])
            verdict, steps = apply_gate(req["state"], data.get("answers", {}))
            out = {"response": data, "wall_ms": wall, "verdict": verdict, "steps": steps}
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
