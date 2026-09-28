#!/usr/bin/env python3
"""
問診第一階段 —— 客戶一句話進來，回答「下一題該問什麼」與「還剩幾種可能」。

    python3 cmcdemo.py "早上發動之後 ABS 的燈一直亮著不會熄"
    python3 cmcweb.py                           # 網頁版，可以逐題追問
    python3 cmcdemo.py --selftest               # 不打 API，驗證切分邏輯

分工跟 jevdemo 一樣，只是換了題目：

  模型   把客戶的話對應成結構化事實。它只抽「已經講出來的」，沒講的留空。
  程式   拿事實去篩症狀，再算哪一題最能切開剩下的，挑出前三題。

程式這一端沒有任何判斷，只有集合運算和熵。挑哪一題問是資訊增益算出來的，
不是模型選的——所以同一個候選集永遠問同一題，可以被審核。
"""

import argparse
import glob
import json
import math
import os
import sys

import jevdemo as J
from intake import (ASK_BUDGET, FACT_KEYS, FACT_VALUES, QUESTIONS,
                    QUESTION_BY_FACT, SHOW_LIMIT, SYMPTOM_FACTS)

SYMPTOM_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "out", "symptom_graphs")


def load_symptoms():
    """症狀圖 + 事實表。事實表沒收錄的症狀會被指出來，而不是默默漏掉。"""
    out = {}
    for path in sorted(glob.glob(os.path.join(SYMPTOM_DIR, "*.json"))):
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        sid = d["symptom_id"]
        entry = SYMPTOM_FACTS.get(sid)
        if entry is None:
            sys.stderr.write("  ! %s 沒有事實表，先當成不可切分\n" % sid)
            entry = {"facts": {k: None for k in FACT_KEYS}, "src": "missing"}
        out[sid] = {
            "id": sid,
            "title": d["symptom_title"],
            "causes": [c.strip() for c in d.get("possible_causes", []) if c.strip()],
            "facts": entry["facts"],
            "needs_tool": entry.get("needs_tool", False),
            "src": entry.get("src", "?"),
        }
    return out


def allowed(sym, fact):
    """這個症狀在這個面向上接受哪些答案。None 代表全部接受。"""
    v = sym["facts"].get(fact)
    if v is None:
        return None
    if isinstance(v, (tuple, list, set, frozenset)):
        return set(v)
    return {v}


def compatible(sym, facts):
    """答案落在允許集合外才排除。允許集合是 None 就永遠相容。"""
    for key, value in facts.items():
        if value is None:
            continue
        ok = allowed(sym, key)
        if ok is not None and value not in ok:
            return False
    return True


def narrow(symptoms, facts):
    return [s for s in symptoms.values() if compatible(s, facts)]


def cause_count(candidates):
    seen = set()
    for c in candidates:
        seen.update(c["causes"])
    return len(seen)


def discriminating_power(sym, fact):
    """這一題能排除掉多少比例的答案空間。1.0 = 只接受一個答案，0.0 = 全接受。"""
    ok = allowed(sym, fact)
    domain = len(FACT_VALUES.get(fact, ()))
    if ok is None or domain < 2:
        return 0.0
    return (domain - len(ok)) / (domain - 1)


def buckets_for(candidates, fact):
    """把候選分到各個答案底下。接受多個答案的候選按比例分攤。

    ("none","abs") 的症狀在 none 與 abs 各算 0.5 個。它確實兩邊都可能出現，
    算成整整一個會高估這一題的切分力。
    """
    out = {}
    for c in candidates:
        ok = allowed(c, fact)
        if ok is None:
            continue
        share = 1.0 / len(ok)
        for v in ok:
            out[v] = out.get(v, 0.0) + share
    return out


def split_score(candidates, fact):
    """這一題把剩下的候選切成幾堆、有多平均。

    用熵而不是「切掉幾個」，因為把 10 個切成 5/5 比切成 9/1 有用得多。
    再乘上平均鑑別力：接受越多答案的候選，這一題對它越沒用。
    """
    b = buckets_for(candidates, fact)
    if len(b) < 2:
        return 0.0
    total = sum(b.values())
    ent = -sum((n / total) * math.log2(n / total) for n in b.values() if n > 0)
    power = sum(discriminating_power(c, fact) for c in candidates) / len(candidates)
    return ent * power


def stop_reason(candidates, questions, asked, rounds=None):
    """沒有下一題時，分辨是收斂了還是問不下去。兩者的下游動作不一樣。

    流程圖上是兩個出口：收斂交付走綠色，問不下去走紅色。程式一開始只回一個
    空清單，畫面就把成功也講成失敗。

    rounds 是真的問出去幾題。預設拿 len(asked) 當替代，那是單次呼叫的寫法：
    開場那句話抽到幾個事實就算幾題。但客戶自己講出來的不該算在預算裡，預算
    是「還能煩客戶幾次」。互動時要把真正的輪數傳進來。
    """
    n = len(asked) if rounds is None else rounds
    askable = [c for c in candidates if not c["needs_tool"]]
    if not candidates:
        # 所有症狀都被排除。不是收斂，是矛盾：事實表填錯，或客戶講的不在這 17 個裡。
        return "empty"
    if questions:
        return None
    if len(askable) <= 1:
        return "converged"
    if n >= ASK_BUDGET:
        return "budget"
    return "no_split"


def next_questions(candidates, asked, limit=SHOW_LIMIT, rounds=None):
    """挑接下來最值得問的幾題，附上為什麼。

    rounds 傳進來就會卡預算：問滿了一題都不給。不卡的話畫面會在第 3 題之後
    還列出第 4 題，下一輪才說「問滿了」——等於叫業務問了才說不該問。
    selftest 的走訪刻意不傳，它要量的就是最壞情況需要幾題，卡住會看不到。
    """
    if rounds is not None and rounds >= ASK_BUDGET:
        return []
    ranked = []
    for q in QUESTIONS:
        if q["fact"] in asked:
            continue
        score = split_score(candidates, q["fact"])
        if score <= 0:
            continue
        b = buckets_for(candidates, q["fact"])
        blind = sum(1 for c in candidates if allowed(c, q["fact"]) is None)
        splits = {q["opts"].get(k, str(k)): round(v, 1)
                  for k, v in sorted(b.items(), key=lambda kv: -kv[1])}
        if blind:
            splits["（這題答不出來）"] = blind
        ranked.append({
            "fact": q["fact"], "ask": q["ask"], "score": score, "splits": splits,
        })
    ranked.sort(key=lambda r: -r["score"])
    return ranked[:limit]


# ----------------------------------------------------------------- 模型 -----

def build_questions(asked_facts=None):
    """call 1 的題目：每個還沒確定的面向一題 choice，都給 not_stated。

    刻意每個面向都問，包括這通問診大概用不到的。它們在同一次呼叫裡平行回答，
    多問幾題幾乎不花錢，少問一題卻可能要多一趟來回。
    """
    asked_facts = asked_facts or set()
    out = {}
    for q in QUESTIONS:
        if q["fact"] in asked_facts:
            continue
        criteria = {str(k): {"what": v} for k, v in q["opts"].items()}
        criteria["not_stated"] = {"what": "客戶沒有提到這件事"}
        out[q["fact"]] = {
            "type": "choice",
            "instructions": q["probe"],
            "criteria": criteria,
        }
    return out


def read_facts(answers):
    """把模型的答案轉回事實。not_stated 與低信心都當作沒講。"""
    facts, confidence = {}, {}
    for fact, a in answers.items():
        if not isinstance(a, dict):
            continue
        pick = a.get("choice")
        if pick in (None, "not_stated"):
            continue
        conf = (a.get("probabilities") or {}).get(pick)
        if isinstance(conf, (int, float)) and conf < 0.5:
            # 模型自己都不確定，不要拿去砍候選集
            continue
        value = pick
        if pick == "True":
            value = True
        elif pick == "False":
            value = False
        facts[fact] = value
        confidence[fact] = conf
    return facts, confidence


def triage(sentence, symptoms):
    """一句話 -> 事實 -> 候選 -> 下三題。"""
    questions = build_questions()
    state = {"complaint": sentence,
             "known_symptoms": [{"id": s["id"], "title": s["title"]}
                                for s in symptoms.values()]}
    data, timing = J.call_api(state, questions)
    facts, conf = read_facts(data.get("answers", {}))
    candidates = narrow(symptoms, facts)
    return {
        "facts": facts, "confidence": conf,
        "candidates": candidates,
        "questions": next_questions(candidates, set(facts)),
        "timing": timing,
        "raw": data,
    }


# --------------------------------------------------------------- 輸出 -------

def show(result, symptoms):
    facts = result["facts"]
    cands = result["candidates"]
    print()
    if facts:
        print("從這句話讀到的：")
        for k, v in facts.items():
            q = QUESTION_BY_FACT[k]
            label = q["opts"].get(v, str(v))
            c = result["confidence"].get(k)
            print("  %-16s %-22s %s" % (k, label, ("%.2f" % c) if c else ""))
    else:
        print("這句話沒有提供任何可以縮小範圍的資訊。")

    print()
    print("剩下 %d 種症狀、%d 種可能原因" % (len(cands), cause_count(cands)))
    for c in cands[:8]:
        flag = "  ← 需要診斷電腦，業務問不出來" if c["needs_tool"] else ""
        print("  %-14s %s%s" % (c["id"], c["title"][:46], flag))
    if len(cands) > 8:
        print("  ... 另外 %d 個" % (len(cands) - 8))

    qs = result["questions"]
    print()
    if not qs:
        why = stop_reason(cands, qs, set(facts))
        print({"empty":     "沒有症狀與這些答案相符。事實表可能填錯，"
                            "或客戶描述的不在這 17 個症狀裡。",
               "converged": "已收斂，交給工廠。",
               "budget":    "問滿 %d 題，剩下交給工廠。" % ASK_BUDGET,
               "no_split":  "剩下的候選沒有業務問得出來的題可以分辨，交給工廠。",
               }.get(why, "沒有下一題。"))
        return
    print("接下來問這 %d 題（依切分力排序）：" % len(qs))
    for i, q in enumerate(qs, 1):
        print("  %d. %s" % (i, q["ask"]))
        print("     切分力 %.2f  →  %s" % (
            q["score"], "  ".join("%s %d" % (k, v) for k, v in q["splits"].items())))


# --------------------------------------------------------------- 自我測試 ---

def reviewable(symptoms):
    """逐格列出需要技術課確認的判讀，附上我填的理由。"""
    from intake import SYMPTOM_FACTS
    rows = []
    for sid, s in sorted(symptoms.items()):
        entry = SYMPTOM_FACTS.get(sid, {})
        for fact in entry.get("draft", []):
            rows.append((sid, fact, s["facts"].get(fact), s["title"]))
    return rows


def _answer_combos(target, cap=8):
    """一個症狀可能給出的答案組合。

    事實是 ("none","abs") 的，客戶會講其中一個，不是講整個 tuple。走訪時
    把整包當答案餵進去，連目標自己都會被排除——測試就在說謊。
    """
    import itertools
    keys, choices = [], []
    for k, v in target["facts"].items():
        if v is None:
            continue
        keys.append(k)
        choices.append(list(v) if isinstance(v, (tuple, list, set, frozenset)) else [v])
    combos = []
    for picked in itertools.islice(itertools.product(*choices), cap):
        combos.append(dict(zip(keys, picked)))
    return combos or [{}]


def selftest(symptoms):
    """不打 API。直接給事實，看切分邏輯對不對。"""
    print("題庫涵蓋度")
    missing = [s["id"] for s in symptoms.values() if s["src"] == "missing"]
    authored = [s["id"] for s in symptoms.values() if s["src"] == "authored"]
    draft = reviewable(symptoms)
    print("  症狀 %d 個，整張事實表 %d 格" % (len(symptoms), len(symptoms) * len(FACT_KEYS)))
    print("  整筆人工判讀：%s" % (", ".join(sorted(authored)) or "無"))
    print("  逐格草稿（需技術課確認）：%d 格" % len(draft))
    print("  沒有事實表的：%s" % (", ".join(missing) or "無"))

    print()
    print("每一題單獨能把 17 個症狀切成幾堆")
    allc = list(symptoms.values())
    for q in QUESTIONS:
        b = buckets_for(allc, q["fact"])
        undec = sum(1 for c in allc if allowed(c, q["fact"]) is None)
        multi = sum(1 for c in allc
                    if allowed(c, q["fact"]) and len(allowed(c, q["fact"])) > 1)
        print("  %-16s 切分力 %.2f  堆數 %d  答不出來 %d  接受多值 %d"
              % (q["fact"], split_score(allc, q["fact"]), len(b), undec, multi))

    print()
    print("需技術課逐格確認的判讀")
    for sid, fact, value, title in draft:
        q = QUESTION_BY_FACT[fact]
        vals = value if isinstance(value, (tuple, list)) else [value]
        print("  %-14s %-16s = %s" % (sid, fact,
              " 或 ".join(q["opts"].get(v, str(v)) for v in vals)))

    print()
    print("走訪：每次都挑切分力最高的一題，看幾題能收斂")
    print("（needs_tool 的症狀業務排除不了，永遠留著，不計入收斂目標）")
    worst, failures = 0, []
    for target in symptoms.values():
        # 接受多個答案的症狀，客戶會給其中一個。逐一模擬，取最差的結果。
        runs = []
        for answer_set in _answer_combos(target):
            facts, asked = {}, set()
            for _ in range(len(QUESTIONS)):
                cands = narrow(symptoms, facts)
                if len(cands) <= 1:
                    break
                nxt = next_questions(cands, asked, limit=1)
                if not nxt:
                    break
                f = nxt[0]["fact"]
                asked.add(f)
                if f in answer_set:
                    facts[f] = answer_set[f]
            runs.append((narrow(symptoms, facts), asked))
        cands, asked = max(runs, key=lambda r: len(r[0]))
        askable = [c for c in cands if not c["needs_tool"]]
        assert target["id"] in {c["id"] for c in cands}, \
            "%s 被自己的答案排除了" % target["id"]
        worst = max(worst, len(asked))
        over = len(askable) > 3
        if over:
            failures.append(target["id"])
        print("  %-14s 問了 %d 題 -> 剩 %d 症狀（可排除的 %d）/ %d 原因%s%s"
              % (target["id"], len(asked), len(cands), len(askable),
                 cause_count(cands), "  [%d 組答案取最差]" % len(runs) if len(runs) > 1 else "",
                 "   ← 收斂不足" if over else ""))
    print()
    print("收斂不足 %d/%d，最多問了 %d 題（業務預算 %d 題）"
          % (len(failures), len(symptoms), worst, ASK_BUDGET))
    if worst > ASK_BUDGET:
        print("  最壞情況超過預算 %d 題 —— 最後一題交給工廠，或把預算放寬"
              % (worst - ASK_BUDGET))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sentence", nargs="?", help="客戶說的一句話")
    ap.add_argument("--selftest", action="store_true", help="不打 API，只驗證切分邏輯")
    args = ap.parse_args()

    symptoms = load_symptoms()

    if args.selftest:
        selftest(symptoms)
        return
    if not args.sentence:
        sys.exit("給我一句客戶的話，或用 --selftest")

    J.API_KEY = J.env("JEV_API_KEY")
    if not J.API_KEY:
        sys.exit("JEV_API_KEY 找不到。放在 .env 或 export。")
    ua = J.env("JEV_USER_AGENT")
    if ua:
        J.USER_AGENT = ua

    print("客戶：「%s」" % args.sentence)
    result = triage(args.sentence, symptoms)
    show(result, symptoms)
    print()
    print("（call 1 %.0fms，%d 個分項平行作答）"
          % (result["timing"]["wall_ms"], len(result["raw"].get("answers", {}))))


if __name__ == "__main__":
    main()
