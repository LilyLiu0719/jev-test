#!/usr/bin/env python3
"""
中華汽車 問診第一階段，網頁版。

    python3 cmcweb.py                 # http://localhost:8010
    python3 cmcweb.py --port 9010

跟 jevdemo 共用同一個殼：同一支 call_api（含 keep-alive 與分段計時）、同一套
CSS、同一種「左邊 request、右邊 trace」的版面。但它是獨立的一支，因為兩個
demo 給的對象不同，之後一定會各走各的。

CSS 是從 jevdemo.PAGE 裡抽出來重用的，不是複製一份。jevdemo 改了配色，這邊
跟著改；jevdemo 沒有的規則才寫在下面。代價是這裡假設了 jevdemo.PAGE 有一個
<style> 區塊。真的拆掉的話這裡會在啟動時就爆，不會靜靜跑出沒有樣式的頁面。

分工跟 jevdemo 一樣，只是換了題目：
  模型   把客戶的話對應成結構化事實，只抽已經講出來的
  程式   拿事實篩症狀，算資訊增益挑出下三題
"""

import argparse
import json
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jevdemo as J
import cmcdemo as C
from intake import ASK_BUDGET, QUESTIONS, QUESTION_BY_FACT

try:
    BASE_CSS = J.PAGE.split("<style>")[1].split("</style>")[0]
except IndexError:
    sys.exit("jevdemo.PAGE 裡找不到 <style> 區塊，無法共用樣式。")


COMPLAINTS = [
    {"note": "燈一直不熄", "text": "早上發動之後 ABS 的燈一直亮著不會熄"},
    {"note": "手煞車已放，燈還亮", "text": "手煞車明明放下來了，那個紅色驚嘆號還是亮著"},
    {"note": "功能不作動", "text": "上坡起步的時候車子會往後溜，以前不會這樣"},
    {"note": "作動太頻繁 + 異音", "text": "煞車的時候方向盤會抖，而且一直聽到噠噠噠的聲音"},
    {"note": "不該作動時作動", "text": "平地起步車子會頓一下，好像被什麼拉住"},
    {"note": "什麼都沒講", "text": "車子怪怪的，你幫我看一下"},
]


PAGE = r"""<!DOCTYPE html>
<html lang="zh-Hant" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>問診第一階段</title>
<style>
__BASE_CSS__

/* ---- 這個 demo 自己的 ---- */
details.about{margin-bottom:14px;border:1px solid var(--rule);background:var(--panel);
border-radius:3px;padding:9px 14px}
details.about summary{cursor:pointer;list-style:none;display:flex;align-items:baseline;
gap:9px;flex-wrap:wrap;font-size:12.5px;color:var(--soft)}
details.about summary::-webkit-details-marker{display:none}
details.about summary::before{content:"+";font-family:"IBM Plex Mono",Menlo,monospace;
color:var(--sig);font-weight:600}
details.about[open] summary::before{content:"\2212"}
details.about summary:hover{color:var(--ink)}
details.about summary:focus-visible{outline:2px solid var(--sig);outline-offset:2px}
details.about summary b{color:var(--ink);font-weight:600}
details.about summary .m{font-family:"IBM Plex Mono",Menlo,monospace;color:var(--faint)}
.ab{margin-top:11px;padding-top:11px;border-top:1px solid var(--rule);
display:grid;grid-template-columns:repeat(auto-fit,minmax(248px,1fr));gap:14px 22px}
.ab h4{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--soft);
font-weight:700;margin:0 0 6px}
.ab p{margin:0 0 5px;font-size:12.5px;line-height:1.6;color:var(--ink)}
.ab .k{font-family:"IBM Plex Mono",Menlo,monospace;font-size:11.5px;color:var(--faint)}
.ab ol{margin:0;padding-left:1.3em;font-size:12.5px;line-height:1.7;color:var(--ink)}
.ab ol .o{color:var(--faint);font-family:"IBM Plex Mono",Menlo,monospace;font-size:11px}
.ab .warn{color:var(--gate)}
.cand{padding:10px 16px 12px}
.cand .ch{display:flex;align-items:baseline;gap:10px;margin-bottom:7px;
font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--soft);
font-weight:700}
.cand .ch .n{margin-left:auto;font-family:"IBM Plex Mono",Menlo,monospace;
font-size:12.5px;letter-spacing:0;text-transform:none;color:var(--ink)}
.c1{display:grid;grid-template-columns:96px minmax(0,1fr);gap:0 10px;
padding:2px 0;font-family:"IBM Plex Mono",Menlo,monospace;font-size:12px;
line-height:1.45}
.c1 .cid{color:var(--faint)}
.c1 .ct{color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.c1.tool .ct{color:var(--soft)}
.c1 .tool{margin-left:8px;font-size:10px;color:var(--gate);
border:1px solid var(--gate);border-radius:2px;padding:0 4px}

.aq{padding:11px 16px 13px}
.aq h3{font-size:11px;letter-spacing:.06em;text-transform:uppercase;
color:var(--ocode);font-weight:700;margin:0 0 8px}
.aqi{border-left:3px solid var(--sig);background:var(--panel);
padding:8px 12px;margin-bottom:7px;border-radius:0 3px 3px 0}
.aqi .t{font-size:14px;color:var(--ink);line-height:1.5}
.aqi .m{margin-top:4px;font-family:"IBM Plex Mono",Menlo,monospace;
font-size:11px;color:var(--faint)}
.aqi .m b{color:var(--soft);font-weight:500}
.aqi.second,.aqi.third{border-left-color:var(--rule);opacity:.8}
.done{padding:11px 16px;color:var(--pass);font-size:13px;
border-left:3px solid var(--pass);background:var(--passbg)}
.halt{padding:11px 16px;color:var(--stop);font-size:13px;
border-left:3px solid var(--stop);background:var(--stopbg)}
</style>
</head>
<body>
<div class="wrap">

<div class="cmd">
  <div class="f f-q">
    <label class="fld" for="q">客戶說的話</label>
    <input id="q" type="text" spellcheck="false" placeholder="例：早上發動之後 ABS 的燈一直亮著">
  </div>
  <button id="run" type="button">問診</button>
</div>

<details class="picker" id="picker">
  <summary>範例<span class="sel" id="picksel"></span><span class="n" id="pickn"></span></summary>
  <div class="chips" id="chips"></div>
</details>

<details class="about">
  <summary><b>資料來源與題庫</b>
    <span class="m">__ABOUT_SUMMARY__</span></summary>
  <div class="ab">
    <div>
      <h4>資料來源</h4>
      <p>中華汽車維修手冊 <b>Group 35C</b>，ABS／ASC 煞車系統。</p>
      <p class="k">data/35C.docx → parse_manuals.py → out/symptom_graphs/</p>
      <p>手冊裡的 17 個症狀入口，共 51 種可能原因。手冊另有 24 張 DTC 決策圖，
         那是技師進工位之後用的，這裡不碰。</p>
    </div>
    <div>
      <h4>題庫從哪來</h4>
      <p>不是從手冊取的。手冊的 1,468 個檢查節點裡有 66% 要 C.M.U.T. 或萬用表，
         業務答不出來。</p>
      <p>題庫是另外寫的，寫在 <span class="k">intake.py</span>，用客戶聽得懂的話問。
         症狀事實表有 <span class="warn">10 格是人工判讀</span>，需要技術課確認。</p>
    </div>
    <div>
      <h4>題庫 6 分項</h4>
      <ol id="ablist"></ol>
    </div>
  </div>
</details>

<div id="err"></div>

<div class="split">

  <div class="panel">
    <div class="phead">
      <h2>Request</h2>
      <p>六個分項平行問。客戶沒提到的歸為 not_stated，不納入篩選。</p>
    </div>
    <div class="pbody">
      <p class="fld">state</p>
      <textarea id="state" spellcheck="false"></textarea>
      <details class="qs"><summary>questions <span class="n" id="qn"></span></summary>
        <div class="qbody"><textarea id="questions" spellcheck="false"></textarea></div>
      </details>
    </div>
  </div>

  <div class="trace">
    <div class="thead">
      <h2>問診</h2>
      <p class="tq" id="tq">還沒開始。</p>
      <div class="legend">
        <i class="m">模型判斷</i><i class="c">程式計算</i>
      </div>
    </div>

    <div class="stage qpanel" id="questions_panel" hidden>
      <div class="shead"><h3>這句話說了什麼</h3><span class="kind">model</span>
        <div class="chain" id="qhead"></div></div>
      <div class="qlist" id="qlist"></div>
    </div>
    <div class="rows" id="ph"><div class="empty">選一個範例或自己輸入，按「問診」。</div></div>

    <div class="gate" id="cand" hidden>
      <div class="cand">
        <div class="ch">剩下的可能<span class="n" id="candn"></span></div>
        <div id="candlist"></div>
      </div>
    </div>

    <div class="routing" id="ask" hidden>
      <div class="aq">
        <h3>接下來問這幾題（依切分力排序）</h3>
        <div id="asklist"></div>
      </div>
    </div>

    <div class="timing" id="timing" hidden>
      <div class="th"><span class="t">where the time went</span>
        <span class="tot" id="ttot"></span></div>
      <div class="tkeys" id="tkeys"></div>
      <div class="tnote" id="tnote"></div>
      <details class="qs raw"><summary>raw API response <span class="n" id="rawn"></span></summary>
        <div class="qbody"><textarea id="rawjson" spellcheck="false" readonly></textarea></div>
      </details>
    </div>
  </div>

</div>
</div>
<script>
const COMPLAINTS = __COMPLAINTS__;
const FACTS = __FACTS__;
const QUESTIONS = __QUESTIONS__;
let cur = 0;
const $ = i => document.getElementById(i);
function esc(t){ return String(t == null ? "" : t)
  .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }
function num(v){ return typeof v === "number" && isFinite(v) }
function n2(v){ return num(v) ? v.toFixed(2) : "–" }
function ms(v){ return num(v) ? v.toFixed(0) + "ms" : "n/a" }

$("ablist").innerHTML = FACTS.map(f =>
  '<li>' + esc(f.label) + ' <span class="o">' +
  Object.keys(f.opts).length + ' 選項</span></li>').join("");

$("chips").innerHTML = COMPLAINTS.map((c,i)=>
  '<button type="button" data-i="'+i+'" aria-pressed="'+(i===0)+'">' +
  '<span class="cq">' + esc(c.text) + '</span>' +
  '<span class="cn">' + esc(c.note) + '</span></button>').join("");
$("chips").addEventListener("click", e => {
  const b = e.target.closest("button"); if(!b) return;
  cur = +b.dataset.i;
  [...$("chips").children].forEach((x,i)=>x.setAttribute("aria-pressed", i===cur));
  $("picker").open = false;
  fill(); reset("已載入，尚未送出。");
});

function syncPicker(){
  $("picksel").textContent = COMPLAINTS[cur] ? COMPLAINTS[cur].text : "";
  $("pickn").textContent = COMPLAINTS.length + " 則";
}
function fill(){
  $("q").value = COMPLAINTS[cur].text;
  syncToState();
  $("questions").value = JSON.stringify(QUESTIONS, null, 2);
  $("qn").textContent = Object.keys(QUESTIONS).length + " 個面向";
  syncPicker();
  drawSkeleton();
}
function syncToState(){
  $("state").value = JSON.stringify({complaint: $("q").value}, null, 2);
}
$("q").addEventListener("input", syncToState);
$("q").addEventListener("keydown", e => { if(e.key === "Enter") $("run").click(); });

function reset(msg){
  $("err").innerHTML = "";
  $("ph").innerHTML = '<div class="empty">' + esc(msg) + '</div>';
  $("ph").hidden = false;
  $("cand").hidden = true; $("ask").hidden = true; $("timing").hidden = true;
  drawSkeleton();
}

// 六個分項在跑之前就畫出來，只是沒有值。看得到會問什麼，才知道沒問什麼。
function drawSkeleton(){ renderFacts(null); }

// 列上只寫勝出的那個選項。落選的機率是判斷品質所在，所以每一列都能點開，
// 連同送出去的題目原文一起顯示。
function detailFor(f, a){
  const head = '<p class="dq">' + esc(f.probe || f.label) + '</p>';
  const probs = (a && a.probabilities) || {};
  const keys = Object.keys(f.opts).concat(["not_stated"]);
  const vals = keys.map(k => num(probs[k]) ? probs[k] : 0);
  const top = Math.max.apply(null, vals.concat([0]));
  if(!a) return head + '<p class="dnote">尚未作答</p>';
  return head + '<div class="dbars">' + keys.map((k, i) => {
    const v = vals[i];
    const label = k === "not_stated" ? "客戶沒提到" : (f.opts[k] || k);
    return '<div class="dbar' + (v > 0 && v === top ? ' hot' : '') + '">' +
      '<span class="dk">' + esc(label) + '</span>' +
      '<span class="dt"><i style="width:' + (v * 100) + '%"></i></span>' +
      '<span class="dv">' + n2(v) + '</span></div>';
  }).join("") + '</div>';
}

function renderFacts(answers){
  const rows = FACTS.map((f, i) => {
    const a = answers ? answers[f.fact] : null;
    const pick = a && a.choice;
    const stated = pick && pick !== "not_stated";
    const p = stated ? (a.probabilities || {})[pick] : null;
    const val = !answers ? ""
      : (stated ? (f.opts[pick] || pick) : "客戶沒提到");
    return '<div class="qr' + (answers && !stated ? " unused" : "") +
           (!answers ? " pending" : "") +
           '" tabindex="0" role="button" aria-expanded="false">' +
      '<span class="qn">Q' + (i+1) + '</span>' +
      '<span class="ql">' + esc(f.label) + '</span>' +
      '<span class="qv">' + (stated ? '<b>' + esc(val) + '</b> ' + n2(p)
                                    : esc(val)) +
      (answers && !stated ? '<span class="tag">未用於篩選</span>' : '') +
      '</span>' +
      '<div class="qd">' + detailFor(f, a) + '</div></div>';
  }).join("");
  $("qlist").innerHTML = '<div class="qsec">' + rows + '</div>';
  $("questions_panel").hidden = false;
}

// 委派一次。renderFacts 每次重繪都換掉整份 innerHTML，逐列綁會失效。
function toggleRow(row){
  if(!row) return;
  const open = row.classList.toggle("open");
  row.setAttribute("aria-expanded", open ? "true" : "false");
}
$("qlist").addEventListener("click", e => {
  if(e.target.closest(".qd")) return;
  toggleRow(e.target.closest(".qr"));
});
$("qlist").addEventListener("keydown", e => {
  if(e.key !== "Enter" && e.key !== " ") return;
  const row = e.target.closest(".qr");
  if(!row) return;
  e.preventDefault();
  toggleRow(row);
});

function renderCandidates(d){
  $("candn").textContent = d.symptom_count + " 個症狀 · " + d.cause_count + " 種可能原因";
  $("candlist").innerHTML = d.candidates.map(c =>
    '<div class="c1' + (c.needs_tool ? " tool" : "") + '">' +
    '<span class="cid">' + esc(c.id.replace("G35C_","")) + '</span>' +
    '<span class="ct">' + esc(c.title) +
    (c.needs_tool ? '<span class="tool">需診斷電腦</span>' : '') +
    '</span></div>').join("");
  $("cand").className = "gate " + (d.symptom_count <= 3 ? "pass" : "");
  $("cand").hidden = false;
}

const STOP_TEXT = {
  empty:     ["halt", "沒有症狀與這些答案相符。事實表可能填錯，或客戶描述的不在這 17 個症狀裡。"],
  converged: ["done", "已收斂。剩下一個候選，交給工廠。"],
  budget:    ["done", "問滿 3 題，剩下交給工廠。"],
  no_split:  ["halt", "剩下的候選沒有業務問得出來的題可以分辨，交給工廠。"],
};

function renderAsk(qs, stop){
  if(!qs.length){
    const t = STOP_TEXT[stop] || ["halt", "沒有下一題。"];
    $("asklist").innerHTML =
      '<div class="' + t[0] + '">' + esc(t[1]) + '</div>';
  } else {
    const rank = ["", "second", "third"];
    $("asklist").innerHTML = qs.map((q,i) =>
      '<div class="aqi ' + rank[i] + '"><div class="t">' + esc(q.ask) + '</div>' +
      '<div class="m">切分力 <b>' + q.score.toFixed(2) + '</b> · ' +
      Object.entries(q.splits).map(([k,v]) => esc(k) + " " + v).join(" · ") +
      '</div></div>').join("");
  }
  $("ask").hidden = false;
}

$("run").addEventListener("click", async () => {
  let state;
  try{ state = JSON.parse($("state").value); }
  catch(e){ $("err").innerHTML = '<div class="err">state 不是合法 JSON：'+esc(e.message)+'</div>'; return; }

  reset("問診中");
  $("tq").textContent = '「' + (state.complaint || "") + '」';
  $("run").disabled = true; $("run").textContent = "...";
  try{
    const r = await fetch("/triage", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify(state)});
    const out = await r.json();
    if(out.error){
      $("err").innerHTML = '<div class="err">' + esc(out.error) + '</div>';
    } else {
      $("ph").hidden = true;
      renderFacts(out.answers);
      $("qhead").textContent = "round trip " + ms(out.timing.wall_ms);
      renderCandidates(out);
      renderAsk(out.questions, out.stop);
      $("ttot").textContent = ms(out.timing.wall_ms) + " total";
      $("tkeys").innerHTML =
        '<span class="k m">模型 <b>' + ms(out.timing.wall_ms) + '</b></span>' +
        '<span class="k o">篩選 + 挑題 <b>' + (out.compute_ms*1000).toFixed(0) + 'µs</b></span>';
      $("tnote").textContent = "挑題用熵，不經過模型。同一個候選集得到同一題。";
      $("rawjson").value = JSON.stringify(out.raw, null, 2);
      $("rawn").textContent = Object.keys(out.raw || {}).join(", ");
      $("timing").hidden = false;
    }
  }catch(e){ $("err").innerHTML = '<div class="err">' + esc(e.message) + '</div>'; }
  $("run").disabled = false; $("run").textContent = "問診";
});

fill();
</script>
</body>
</html>
"""


SYMPTOMS = None   # 啟動時載入一次


def triage_payload(complaint):
    t0 = time.perf_counter()
    questions = C.build_questions()
    state = {"complaint": complaint,
             "known_symptoms": [{"id": s["id"], "title": s["title"]}
                                for s in SYMPTOMS.values()]}
    data, timing = J.call_api(state, questions)
    answers = data.get("answers", {})

    t1 = time.perf_counter()
    facts, _ = C.read_facts(answers)
    candidates = C.narrow(SYMPTOMS, facts)
    nxt = C.next_questions(candidates, set(facts), limit=ASK_BUDGET)
    stop = C.stop_reason(candidates, nxt, set(facts))
    compute_ms = (time.perf_counter() - t1) * 1000

    return {
        "answers": answers,
        "candidates": [{"id": c["id"], "title": c["title"],
                        "needs_tool": c["needs_tool"]} for c in candidates],
        "symptom_count": len(candidates),
        "cause_count": C.cause_count(candidates),
        "questions": nxt,
        "stop": stop,
        "timing": timing,
        "compute_ms": compute_ms,
        "raw": data,
    }


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
        facts = [{"fact": q["fact"], "label": q["ask"], "probe": q["probe"],
                  "opts": {str(k): v for k, v in q["opts"].items()}}
                 for q in QUESTIONS]
        summary = ("35C 煞車系統 \u00b7 %d 個症狀 \u00b7 %d 種可能原因 \u00b7 題庫 %d 分項"
                   % (len(SYMPTOMS), C.cause_count(list(SYMPTOMS.values())),
                      len(QUESTIONS)))
        page = (PAGE.replace("__ABOUT_SUMMARY__", summary)
                    .replace("__BASE_CSS__", BASE_CSS)
                    .replace("__COMPLAINTS__", json.dumps(COMPLAINTS, ensure_ascii=False))
                    .replace("__FACTS__", json.dumps(facts, ensure_ascii=False))
                    .replace("__QUESTIONS__", json.dumps(C.build_questions(), ensure_ascii=False)))
        self._send(200, page, "text/html; charset=utf-8")

    def do_POST(self):
        if self.path != "/triage":
            self._send(404, "not found", "text/plain")
            return
        n = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(n).decode())
            out = triage_payload(req.get("complaint", ""))
        except Exception as e:
            out = {"error": str(e)}
        self._send(200, json.dumps(out, ensure_ascii=False), "application/json")


def main():
    global SYMPTOMS
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    J.API_KEY = J.env("JEV_API_KEY")
    if not J.API_KEY:
        sys.exit("JEV_API_KEY 找不到。放在 .env 或 export。")
    ua = J.env("JEV_USER_AGENT")
    if ua:
        J.USER_AGENT = ua

    SYMPTOMS = C.load_symptoms()
    authored = [s["id"] for s in SYMPTOMS.values() if s["src"] == "authored"]
    sys.stderr.write("載入 %d 個症狀，其中 %d 個事實是人工判讀：%s\n"
                     % (len(SYMPTOMS), len(authored), ", ".join(sorted(authored))))

    url = "http://localhost:%d" % args.port
    print("問診 demo：%s   (ctrl-c 停止)" % url)
    if not args.no_open:
        webbrowser.open(url)
    try:
        ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
