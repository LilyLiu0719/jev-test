#!/usr/bin/env python3
"""
Parse Mitsubishi/CMC service manual .docx files into a ground-truth dataset
of multi-hop routing decisions.

Outputs (under --out, default ./out):
  transitions.jsonl        one record per 是/否 branch
  graphs/G<group>_<dtc>.json   full decision graph per procedure
  terminals.json           terminal-id -> exact source text registry
  report.txt               validation report (also printed)

Usage:
  python3 parse_manuals.py --groups 55
  python3 parse_manuals.py --groups 13 23 35C 37 52B 54A 55
"""

import argparse
import json
import math
import re
import subprocess
import sys
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CACHE = ROOT / "cache"

# Pinned extraction. extract-text is preferred but is not installed in this
# environment, so the pandoc fallback is what actually produced the cache.
# --wrap=none is load-bearing: pandoc's default wrapping splits long 是/否
# branch lines mid-sentence, which silently truncates terminal actions.
EXTRACT_TEXT_CMD = ["extract-text"]                       # + [src]
PANDOC_CMD = ["pandoc"]                                   # + [src] + PANDOC_ARGS
PANDOC_ARGS = ["-t", "markdown", "--wrap=none"]


def pandoc_version() -> str:
    try:
        r = subprocess.run(["pandoc", "--version"], capture_output=True)
        return r.stdout.decode().splitlines()[0].strip()
    except Exception:
        return "unknown"

# ---------------------------------------------------------------- extraction

def extract(group: str, refresh: bool = False) -> str:
    """Extract a .docx to markdown, cached at ./cache/<group>.md."""
    CACHE.mkdir(exist_ok=True)
    md = CACHE / f"{group}.md"
    if md.exists() and not refresh:
        return md.read_text(encoding="utf-8")

    src = DATA / f"{group}.docx"
    if not src.exists():
        raise FileNotFoundError(src)

    text, provenance = None, None
    try:
        r = subprocess.run(EXTRACT_TEXT_CMD + [str(src)], capture_output=True)
        if r.returncode == 0 and r.stdout.strip():
            text = r.stdout.decode("utf-8", errors="replace")
            provenance = {"tool": "extract-text",
                          "cmd": " ".join(EXTRACT_TEXT_CMD + [str(src.name)])}
    except FileNotFoundError:
        pass

    if text is None:  # pinned fallback
        cmd = PANDOC_CMD + [str(src)] + PANDOC_ARGS
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(f"pandoc failed on {src}: {r.stderr.decode()[:400]}")
        text = r.stdout.decode("utf-8", errors="replace")
        provenance = {"tool": "pandoc", "version": pandoc_version(),
                      "cmd": " ".join(PANDOC_CMD + [src.name] + PANDOC_ARGS)}

    md.write_text(text, encoding="utf-8")
    (CACHE / f"{group}.meta.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
    return text


def extraction_provenance(group: str) -> dict:
    meta = CACHE / f"{group}.meta.json"
    if meta.exists():
        return json.loads(meta.read_text())
    return {"tool": "pandoc", "version": pandoc_version(),
            "cmd": " ".join(PANDOC_CMD + [f"{group}.docx"] + PANDOC_ARGS),
            "note": "cache predates provenance tracking; re-run --refresh to confirm"}


# ------------------------------------------------------------- normalisation

FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

NOISE_PATTERNS = [
    re.compile(r"Error!\s*Use the Home tab"),      # trap 3
    re.compile(r"Error!\s*(Bookmark|Reference|No text)"),
    re.compile(r"^!\[\]\("),                        # images
    re.compile(r"^\s*<!--.*-->\s*$"),
    re.compile(r"^\s*[-=+|]{3,}[-=+|\s]*$"),        # pandoc table rules
    re.compile(r"^\s*:+\s*$"),
    re.compile(r"^\s*#+\s*$"),                      # empty headings
    re.compile(r"^\s*\\?\*{0,2}\\?\s*$"),           # **\ , \ , ** artifacts
    re.compile(r"^\s*\{?width=|^\s*height="),
]


def debold(s: str) -> str:
    """Strip markdown emphasis + escapes so patterns can match (trap 2)."""
    s = s.replace("**", "").replace("__", "")
    s = re.sub(r"\\([%_&#$~^{}\[\]\\])", r"\1", s)
    s = s.replace("\u3000", " ").replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def clean_text(s: str) -> str:
    """Human-readable text for the dataset."""
    s = debold(s)
    s = re.sub(r"\{[^{}]*\}", "", s)
    s = re.sub(r"(錯誤!\s*尚未定義書籤。?|Error!\s*Bookmark not defined\.?)", "", s)
    s = re.sub(r"[（(]\s*(請參閱|參閱|參考)\s*P\.?\s*[)）]", "", s)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    return re.sub(r"\s+", " ", s).strip()


def is_noise(raw: str) -> bool:
    if not raw.strip():
        return True
    return any(p.search(raw) for p in NOISE_PATTERNS)


def clean_lines(text: str):
    """-> list of (clean, debolded, heading_level, raw_lineno)"""
    out = []
    for i, raw in enumerate(text.splitlines(), 1):
        if is_noise(raw):
            continue
        stripped = raw.strip()
        # pandoc wraps some paragraphs in blockquotes; "> **否：** ..." is a
        # real branch line and must not be hidden behind the quote marker.
        stripped = re.sub(r"^(?:\s*>+\s*)+", "", stripped)
        m = re.match(r"^(#{1,6})\s*(.*)$", stripped)
        level, body = (len(m.group(1)), m.group(2)) if m else (0, stripped)
        c = clean_text(body)
        if not c:
            continue
        out.append((c, debold(body).translate(FULLWIDTH_DIGITS), level, i))
    return out


# ------------------------------------------------------------------ patterns

# trap 6, widened: the brief says [BPCU][0-9]{6}, but GROUP_13/23/35C carry
# hex letters in real codes (P000A00, P062F44, C121E41). A digits-only regex
# silently drops those procedures, so hex is allowed after the first digit.
DTC_RE = re.compile(r"(?<![0-9A-Za-z])[BPCU][0-9][0-9A-F]{5}(?![0-9A-Za-z])")
# headers appear as "診斷故障碼 B100111：..." (55) or "DTC P000A00：..." (13/23)
DTC_HEADER_RE = re.compile(
    r"(?:診斷故障碼|DTC)\s*[:：]?\s*[BPCU][0-9][0-9A-F]{5}")
# trap 2: "步驟**** 1. ****C" debolds to "步驟 1. C"; trap 1: full-width digits
STEP_RE = re.compile(r"^步\s*驟\s*([0-9]+)\s*[.．、:：]?\s*(.*)$")
Q_RE = re.compile(r"^Q\s*[:：]\s*(.*)$")
BRANCH_RE = re.compile(r"^([是否])\s*[:：]\s*(.*)$")
BULLET_RE = re.compile(r"^[-*•]\s+(.*)$")
# trap 1: 進行步驟 N / 至步驟 N / 執行步驟N, with or without spaces
STEP_REF_RE = re.compile(
    r"(?:請)?(?:進行|前往|轉至|跳至|執行|回到|返回|重複|至)?\s*步\s*驟\s*([0-9]+)"
)
# "然後進行步驟。" -- the manual omits the number entirely
STEP_REF_NO_NUM_RE = re.compile(
    r"(?:進行|前往|轉至|跳至|執行|回到|返回|重複|至)\s*步\s*驟\s*(?![0-9])")
STEP_REF_ONLY_RE = re.compile(
    r"^(?:請)?(?:進行|前往|轉至|跳至|執行|回到|返回|重複|至)?\s*步\s*驟\s*([0-9]+)\s*[。.，,、]?$"
)

# Section headings differ per group; first match wins, longest first.
SECTION_ALIASES = [
    ("symptom",   ["故障症狀說明", "故障症狀", "症狀說明", "故障模式"]),
    ("criteria",  ["故障判定條件", "故障判定", "故障判斷", "判定基準",
                   "故障確認條件"]),
    ("causes",    ["可能原因", "故障原因"]),
    ("procedure", ["診斷故障碼程序", "診斷程序", "診斷步驟", "故障排除程序"]),
    ("clearing",  ["故障清除確認", "故障碼清除確認"]),
    ("effect",    ["故障模式影響", "作用", "功能"]),
]
SECTION_PROCEDURE_STARTERS = ("診斷程序", "診斷步驟")
SECTION_DTC_PROCEDURES = ["診斷故障碼程序", "診斷故障碼排除程序", "故障碼排除程序"]
SECTION_DTC_TABLE = "診斷故障碼表"


def section_of(deb: str):
    """Match a standalone section heading line (works with or without #)."""
    t = deb.strip().strip("：:").strip()
    if len(t) > 12:
        return None
    for name, aliases in SECTION_ALIASES:
        if t in aliases:
            return name
    return None

# headings that mean "the DTC-procedures section has ended"
# A DTC-procedures section ends here. 故障症狀表 / 症狀檢查程序 / 檢查程序 N
# introduce GROUP_35C's *symptom*-keyed decision trees: real trees, but not
# keyed by a DTC, so they belong to a separate dataset, not to the last DTC.
# Below this many markdown headings a file counts as "heading-less" (13, 23).
HEADING_THRESHOLD = 20

END_SECTION_RE = re.compile(
    r"^(CMUT相關操作設定|客製化編碼|作動器測試|ECU資訊|維修資料參考表|"
    r"規格|特殊工具|保養|現場緊急處理|拆卸和安裝|"
    r"故障症狀表|症狀檢查程序|症狀表|故障症狀排除程序|檢查程序\s*[0-9０-９]|"
    r"檢查程序$|"
    r"診斷故障碼表|故障排除$|診斷功能|如何讀取診斷故障碼|如何刪除診斷故障碼|"
    r"檢查凍結畫面資料|診斷故障排除的標準流程|作動器測試)"
)


# ------------------------------------------------------------- terminal ids

TERMINAL_LEXICON = [
    # order matters: most specific first (first pattern to match wins)
    (r"(維修|檢查).*(CAN|匯流排|總線)", "repair_can_bus"),
    (r"(更換|替換).*(CAN|匯流排|總線)", "replace_can_bus"),
    (r"維修.*(線束|接頭|線路|配線)", "repair_harness"),
    (r"(更換|替換).*(線束|接頭|配線)", "replace_harness"),
    (r"(更換|替換).*(ECU|ECM|TCU|BCM|SRS|ASC|模組|控制器|電腦|單元)", "replace_ecu"),
    (r"(更換|替換).*(感知器|感測器|開關)", "replace_sensor"),
    (r"(更換|替換).*(馬達|致動器|作動器)", "replace_motor"),
    (r"(更換|替換).*(繼電器|保險絲)", "replace_relay"),
    (r"(更換|替換)", "replace_part"),
    (r"間歇性故障", "intermittent_fault"),
    (r"(程序完成|檢修完成|結束|正常)", "procedure_complete"),
    (r"(維修|修理|修復)", "repair_other"),
    (r"(檢查|確認|參閱|參考)", "check_other"),
]


class TerminalRegistry:
    """Stable ids for terminal actions, deduped by exact normalised string."""

    def __init__(self):
        self.by_text = OrderedDict()
        self.used = set()

    def id_for(self, text: str) -> str:
        key = text.strip().rstrip("。.")
        if key in self.by_text:
            return self.by_text[key]
        slug = "other"
        for pat, name in TERMINAL_LEXICON:
            if re.search(pat, key):
                slug = name
                break
        base = f"terminal_{slug}"
        tid, n = base, 1
        while tid in self.used:
            n += 1
            tid = f"{base}_{n}"
        self.used.add(tid)
        self.by_text[key] = tid
        return tid

    def dump(self):
        return [{"id": v, "text": k} for k, v in self.by_text.items()]


# ------------------------------------------------------------------- parsing

def split_dtc_header(line: str):
    """'診斷故障碼 B100111：X 診斷故障碼 B102213：Y' -> [(code,name),...] (trap 7)."""
    out = []
    parts = [x for x in re.split(r"(?=診斷故障碼|(?<![A-Za-z])DTC\s)", line)
             if "診斷故障碼" in x or re.match(r"\s*DTC\s", x)]
    if not parts:
        parts = [line]
    for part in parts:
        for m in DTC_RE.finditer(part):
            code = m.group(0)
            rest = part[m.end():].strip()
            rest = re.sub(r"^[:：\s]*", "", rest)
            rest = re.sub(r"\s*\d+-\d+\s*$", "", rest)  # page refs "55-4"
            out.append((code, rest.strip()))
    return out


def find_procedure_regions(lines):
    """Spans of the document that hold DTC procedures.

    Marker-anchored slicing is not enough: GROUP_54A has four 診斷故障碼程序
    markers but a fifth subsystem (雷達) whose procedures carry no marker at
    all, and its tables of contents repeat the marker with a page number.

    So: skip the front matter up to the first exact-name marker, then treat
    everything after it as procedure content EXCEPT the spans that begin at a
    non-procedure heading (特殊工具, 故障症狀表, CMUT相關操作設定, ...) and run
    until the next DTC header. That keeps unmarked procedure blocks and drops
    symptom trees, parts tables and front matter alike."""
    start = None
    for i, (c, d, lvl, ln) in enumerate(lines):
        if d.strip() in SECTION_DTC_PROCEDURES:     # exact: TOC rows carry "54A-14"
            start = i + 1
            break
    if start is None:                                # trap 4: no marker anywhere
        for i, (c, d, lvl, ln) in enumerate(lines):
            if is_dtc_header(c, d, lvl) and any(
                    STEP_RE.match(x[1]) or Q_RE.match(x[1])
                    for x in lines[i:i + 80]):
                start = i
                break
    if start is None:
        return []

    # In a file that carries real markdown headings, a terminator only counts
    # as a section end when it IS a heading: GROUP_54A repeats "診斷功能" as a
    # plain line inside procedures, which would otherwise chop every one of
    # them. GROUP_13/23 extract with almost no headings, so there we have to
    # accept bare lines.
    headed = sum(1 for _, _, lvl, _ in lines if lvl) >= HEADING_THRESHOLD

    regions, seg_start, suppressed = [], start, False
    for i in range(start, len(lines)):
        c, d, lvl, ln = lines[i]
        if not suppressed:
            if END_SECTION_RE.match(d) and (lvl if headed else len(d) < 20):
                if i > seg_start:
                    regions.append((seg_start, i))
                suppressed = True
        elif is_dtc_header(c, d, lvl):
            seg_start, suppressed = i, False
    if not suppressed and seg_start < len(lines):
        regions.append((seg_start, len(lines)))
    return regions


def is_dtc_header(clean, deb, level):
    """A procedure header, not a body mention of a DTC."""
    t = deb.strip()
    if not DTC_HEADER_RE.match(t):      # must START with the header form
        return False
    if BRANCH_RE.match(t) or Q_RE.match(t) or STEP_RE.match(t):
        return False
    if level >= 1:
        return True
    # trap 4: heading-less files (13/23) -> short standalone header line
    return len(t) < 120


def parse_group(group: str, lines, terminals: TerminalRegistry):
    """-> (procedures, region_lines) over every 診斷故障碼程序 section."""
    regions = find_procedure_regions(lines)
    procedures, region_lines = [], []

    for sec_i, (start, end) in enumerate(regions):
        region = lines[start:end]
        region_lines.extend(region)
        cur = None
        for clean, deb, lvl, ln in region:
            if is_dtc_header(clean, deb, lvl):
                pairs = split_dtc_header(deb)
                if cur is not None and not cur["body"]:
                    cur["dtcs"].extend(pairs)        # back-to-back headers
                    continue
                cur = {"dtcs": list(pairs), "body": [], "line": ln,
                       "section": sec_i}
                procedures.append(cur)
            elif cur is not None:
                cur["body"].append((clean, deb, lvl, ln))

    parsed = [parse_procedure(group, p, terminals) for p in procedures]

    # Record ids must be unique: the same DTC can legitimately have separate
    # procedures in different subsystem sections (54A), so disambiguate only
    # the codes that actually repeat within a group.
    counts = Counter((p["dtc_codes"] or ["UNKNOWN"])[0] for p in parsed)
    seen = Counter()
    for i, p_ in enumerate(parsed):
        p_["procedure_index"] = i
        code = (p_["dtc_codes"] or ["UNKNOWN"])[0]
        if counts[code] > 1:
            p_["id_suffix"] = f"_p{seen[code]}"
            seen[code] += 1
        else:
            p_["id_suffix"] = ""

    return parsed, region_lines


def parse_procedure(group, proc, terminals):
    codes = [c for c, _ in proc["dtcs"]]
    names = [n for _, n in proc["dtcs"]]
    seen, codes_u, names_u = set(), [], []
    for c, n in zip(codes, names):
        if c not in seen:
            seen.add(c)
            codes_u.append(c)
            names_u.append(n)

    symptom, criteria, causes = None, None, []
    section = None
    sym_buf, crit_buf = [], []
    steps = OrderedDict()          # n -> dict
    cur_step = None
    cur_q = None
    anomalies = []

    extra, pending = defaultdict(list), []

    for clean, deb, lvl, ln in proc["body"]:
        # section headings (with or without markdown "#", trap 4)
        sec = section_of(deb)
        if sec is not None:
            section = sec
            pending = []
            continue

        sm = STEP_RE.match(deb)
        if sm:
            section = "procedure"
            n = int(sm.group(1))
            title = clean_text(sm.group(2))
            if n in steps:
                # The manual sometimes numbers two different checks 步驟1
                # (GROUP_13). Keep both, flag it, and exclude from eval.
                anomalies.append(f"duplicate 步驟 {n} (line {ln})")
                steps[n]["duplicate_number"] = True
                cur_step = steps[n]
                if title:
                    cur_step["body"].append(title)
            else:
                cur_step = {"n": n, "title": title, "body": [], "q": None,
                            "branches": [], "line": ln}
                steps[n] = cur_step
            cur_q = None
            continue

        qm = Q_RE.match(deb)
        bm = BRANCH_RE.match(deb)
        if (qm or bm) and cur_step is None:
            # Some procedures (GROUP_23, GROUP_37) state a single check with no
            # "步驟 N" line at all. Synthesise step 1 from the preceding prose so
            # the branch is not silently dropped.
            cur_step = {"n": 1, "title": clean_text(pending[0]) if pending else "",
                        "body": [clean_text(x) for x in pending[1:]], "q": None,
                        "branches": [], "line": ln, "implicit": True}
            steps[1] = cur_step
            anomalies.append("implicit 步驟 1 synthesised (no 步驟 line in source)")

        if qm:
            cur_q = clean_text(qm.group(1))
            cur_step["q"] = cur_q
            continue

        if bm and cur_step is not None:
            cur_step["branches"].append({
                "answer": bm.group(1),
                "text": clean_text(bm.group(2)),
                "line": ln,
            })
            continue

        if section == "symptom":
            sym_buf.append(clean)
        elif section == "criteria":
            crit_buf.append(clean)
        elif section == "causes":
            b = BULLET_RE.match(clean)
            causes.append(clean_text(b.group(1)) if b else clean)
        elif section == "procedure":
            if cur_step is not None and cur_q is None:
                if not clean.startswith("("):   # keep sub-steps out of the gist
                    cur_step["body"].append(clean)
            elif cur_step is None:
                pending.append(clean)           # prose before any 步驟 line
        elif section in ("clearing", "effect"):
            extra[section].append(clean)

    symptom = " ".join(sym_buf).strip() or None
    criteria = " ".join(crit_buf).strip() or None

    # --- resolve branches -------------------------------------------------
    step_ids = [f"step_{n}" for n in sorted(steps)]
    terminal_ids = OrderedDict()

    for n in sorted(steps):
        st = steps[n]
        for br in st["branches"]:
            t = br["text"]
            only = STEP_REF_ONLY_RE.match(t)
            if only:                                   # pure "進行步驟 N" / "至步驟 N"
                br["kind"] = "step"
                br["target"] = f"step_{int(only.group(1))}"
                br["followup_step"] = None
                br["incomplete_step_ref"] = False
            else:                                      # trap 5: terminal action
                br["kind"] = "terminal"
                br["target"] = terminals.id_for(t)
                fu = STEP_REF_RE.search(t)
                br["followup_step"] = int(fu.group(1)) if fu else None
                br["incomplete_step_ref"] = bool(
                    not fu and STEP_REF_NO_NUM_RE.search(t))
                terminal_ids.setdefault(br["target"], t.rstrip("。."))

    candidate_actions = (
        [{"id": f"step_{n}", "text": steps[n]["title"] or
          (steps[n]["body"][0] if steps[n]["body"] else "")} for n in sorted(steps)]
        + [{"id": tid, "text": txt} for tid, txt in terminal_ids.items()]
    )

    return {
        "group": group,
        "dtc_codes": codes_u,
        "dtc_names": names_u,
        "symptom": symptom,
        "failure_criteria": criteria,
        "possible_causes": causes,
        "steps": steps,
        "candidate_actions": candidate_actions,
        "terminal_ids": terminal_ids,
        "anomalies": anomalies,
        "extra_sections": {k: " ".join(v) for k, v in extra.items()},
        "section_index": proc.get("section", 0),
        "line": proc["line"],
    }


# --------------------------------------------------------- symptom procedures

# GROUP_35C carries a second family of decision trees keyed by a customer
# complaint ("檢查程序 3：ABS 警示燈不會亮起") rather than by a stored DTC.
# Same shape, different entry point, so they go to their own dataset.
SYMPTOM_MARKERS = ["症狀檢查程序", "故障症狀表", "故障症狀排除程序"]
SYMPTOM_HEADER_RE = re.compile(r"^檢查程序\s*([0-9]+)\s*[:：]?\s*(.*)$")
SYMPTOM_END_RE = re.compile(
    r"^(CMUT相關操作設定|客製化編碼|作動器測試|ECU資訊|維修資料參考表|"
    r"規格|特殊工具|保養|現場緊急處理|拆卸和安裝|診斷故障碼表|診斷故障碼程序)")


def find_symptom_regions(lines):
    headed = sum(1 for _, _, lvl, _ in lines if lvl) >= HEADING_THRESHOLD
    regions, i = [], 0
    while i < len(lines):
        d = lines[i][1].strip()
        if any(d.startswith(m) for m in SYMPTOM_MARKERS) and len(d) < 30:
            end, seen, has_steps = len(lines), False, False
            for j in range(i + 1, len(lines)):
                c2, d2, lvl2, ln2 = lines[j]
                if SYMPTOM_HEADER_RE.match(d2):
                    seen = True
                elif seen and (STEP_RE.match(d2) or Q_RE.match(d2)):
                    has_steps = True
                if seen and SYMPTOM_END_RE.match(d2) and (
                        lvl2 if headed else len(d2) < 20):
                    end = j
                    break
            if seen and has_steps:
                regions.append((i + 1, end))
                i = end
                continue
        i += 1
    return regions


def parse_symptom_group(group, lines, terminals):
    """-> (procedures, region_lines) for the symptom-keyed trees."""
    procedures, region_lines = [], []
    for sec_i, (start, end) in enumerate(find_symptom_regions(lines)):
        region = lines[start:end]
        region_lines.extend(region)
        cur = None
        for clean, deb, lvl, ln in region:
            m = SYMPTOM_HEADER_RE.match(deb)
            if m:
                cur = {"dtcs": [], "body": [], "line": ln, "section": sec_i,
                       "sym_no": int(m.group(1)),
                       "sym_text": clean_text(m.group(2)).rstrip("。")}
                procedures.append(cur)
            elif cur is not None:
                cur["body"].append((clean, deb, lvl, ln))

    parsed = []
    for i, raw in enumerate(procedures):
        p_ = parse_procedure(group, raw, terminals)
        p_.update(kind="symptom", procedure_index=i, id_suffix="",
                  sym_no=raw["sym_no"], sym_text=raw["sym_text"])
        parsed.append(p_)
    return parsed, region_lines


# ------------------------------------------------------- defects / exclusions

# A record is excluded from eval by default when the *source procedure* is
# defective at that point -- the manual, not the parser, is wrong. Such records
# stay in the dataset so a model that routes better than the manual can be
# measured separately rather than scored as wrong.

def analyze_procedure(proc):
    """-> (branch_defects, step_defects, issues) for one procedure."""
    steps = proc["steps"]
    branch_defects = defaultdict(list)   # (step_n, answer) -> [reason, ...]
    step_defects = defaultdict(list)     # step_n -> [reason, ...]
    issues = defaultdict(list)
    code = (f"SYM{proc['sym_no']}" if proc.get("kind") == "symptom"
            else "/".join(proc["dtc_codes"]) or f"line{proc['line']}")

    for a in proc["anomalies"]:
        issues["parse anomalies"].append(f"{code}: {a}")

    if not steps:
        issues["procedures with no steps"].append(code)
        return branch_defects, step_defects, issues

    present = set(steps)

    for n in sorted(steps):
        for br in steps[n]["branches"]:
            key = (n, br["answer"])
            if br["kind"] == "step":
                tgt = int(br["target"].split("_")[1])
                if tgt not in present:
                    branch_defects[key].append(
                        f"dangling_step_ref: 步驟{n} {br['answer']} -> 步驟{tgt}, "
                        f"which does not exist in this procedure")
                    issues["branch -> missing step"].append(
                        f"{code}: 步驟{n} {br['answer']} -> 步驟{tgt} (not in procedure)")
                elif tgt == n:
                    branch_defects[key].append(
                        f"self_loop: 步驟{n} {br['answer']} -> 步驟{n} (itself)")
                    issues["self-loop branch"].append(
                        f"{code}: 步驟{n} {br['answer']} -> itself")
            if br.get("incomplete_step_ref"):
                branch_defects[key].append(
                    f"incomplete_step_ref: 步驟{n} {br['answer']} says to go to a "
                    f"step but the number is missing in the source")
                issues["step reference with no number"].append(
                    f"{code}: 步驟{n} {br['answer']}")
            if br["followup_step"] and br["followup_step"] not in present:
                branch_defects[key].append(
                    f"dangling_followup: terminal action refers to 步驟"
                    f"{br['followup_step']}, which does not exist")
                issues["terminal follow-up -> missing step"].append(
                    f"{code}: 步驟{n} {br['answer']} -> 步驟{br['followup_step']}")

    # reachability from the entry step
    entry = min(present)
    seen, stack = {entry}, [entry]
    while stack:
        cur = stack.pop()
        for br in steps[cur]["branches"]:
            nxt = None
            if br["kind"] == "step":
                nxt = int(br["target"].split("_")[1])
            elif br["followup_step"]:
                nxt = br["followup_step"]
            if nxt in present and nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    for n in sorted(present - seen):
        step_defects[n].append(
            f"unreachable_step: 步驟{n} is not reachable from entry 步驟{entry}")
        issues["unreachable steps"].append(f"{code}: 步驟{n}")

    if not any(br["kind"] == "terminal"
               for st in steps.values() for br in st["branches"]):
        issues["procedures with no terminal action"].append(code)
        for n in present:
            step_defects[n].append(
                "no_terminal_action: procedure never reaches a terminal action")

    for n in sorted(steps):
        st = steps[n]
        answers = {br["answer"] for br in st["branches"]}
        if st.get("duplicate_number") or len(st["branches"]) > 2:
            step_defects[n].append(
                f"duplicate_step_number: 步驟{n} appears more than once in this "
                f"procedure ({len(st['branches'])} branches merged onto it)")
            issues["duplicate step numbers"].append(f"{code}: 步驟{n}")
        if st["q"] is None and not st["branches"]:
            issues["steps with no Q and no branches"].append(f"{code}: 步驟{n}")
        elif st["q"] is None:
            issues["steps with branches but no Q"].append(f"{code}: 步驟{n}")
            step_defects[n].append(
                f"missing_question: 步驟{n} has 是/否 branches but no Q line")
        elif answers != {"是", "否"}:
            issues["steps with an incomplete 是/否 pair"].append(
                f"{code}: 步驟{n} has {sorted(answers) or 'no branches'}")
            step_defects[n].append(
                f"incomplete_branch_pair: 步驟{n} has "
                f"{sorted(answers) or 'no'} branch(es), not both 是 and 否")

    return branch_defects, step_defects, issues


# ------------------------------------------------------------------- emitting

# The literal 是->normal / 否->abnormal mapping is UNRELIABLE and is kept only
# as a derived convenience. Many procedures ask 是否出現診斷故障碼? where 是
# means a fault IS present, which this mapping inverts. Eval state must use
# `question` (raw Q text) + `answer_zh` (raw 是/否) instead.
NAIVE_CHECK_RESULT = {"是": "normal", "否": "abnormal"}
SYNTHESIZED_REASON = ("synthesized_step: step 1 was synthesised by the parser "
                      "(the source states a check with no 步驟 line)")
ANSWER_EN = {"是": "yes", "否": "no"}


def step_gist(st):
    parts = ([st["title"]] if st["title"] else []) + st["body"]
    return " / ".join(p for p in parts if p)


def emit(proc, branch_defects, step_defects):
    """-> (transition records, graph dict)"""
    recs = []
    steps = proc["steps"]
    sym = proc.get("kind") == "symptom"
    code = (proc["dtc_codes"][0] if proc["dtc_codes"]
            else (None if sym else "UNKNOWN"))
    g = proc["group"]

    for n in sorted(steps):
        st = steps[n]
        occ = Counter()
        synth = bool(st.get("implicit"))
        for br in st["branches"]:
            zh = br["answer"]
            occ[zh] += 1
            dup = f"_b{occ[zh]}" if occ[zh] > 1 else ""
            reasons = list(step_defects.get(n, [])) + \
                list(branch_defects.get((n, zh), []))
            if synth:
                # Excluded by the same mechanism as a source defect, but it is
                # NOT one: the parser invented this step, so it cannot serve as
                # ground truth. Counted separately in the report.
                reasons.append(SYNTHESIZED_REASON)
            if proc.get("kind") == "symptom":
                rid = f"G{g}_SYM{proc['sym_no']}_s{n}_{ANSWER_EN[zh]}{dup}"
            else:
                rid = (f"G{g}_{code}{proc['id_suffix']}"
                       f"_s{n}_{ANSWER_EN[zh]}{dup}")
            recs.append({
                "id": rid,
                "group": g,
                "entry_kind": proc.get("kind", "dtc"),
                "symptom_id": (f"G{g}_SYM{proc['sym_no']}"
                               if proc.get("kind") == "symptom" else None),
                "symptom_title": proc.get("sym_text"),
                "dtc_code": code,
                "dtc_codes": proc["dtc_codes"],
                "dtc_name": proc["dtc_names"][0] if proc["dtc_names"] else "",
                "section_index": proc["section_index"],
                "procedure_index": proc["procedure_index"],
                "dtc_names": proc["dtc_names"],
                "symptom": proc["symptom"],
                "failure_criteria": proc["failure_criteria"],
                "possible_causes": proc["possible_causes"],
                "extra_sections": proc["extra_sections"],
                "current_step": n,
                "current_step_text": step_gist(st),
                # --- observed state the model sees: raw question + raw answer
                "question": st["q"],
                "answer_zh": zh,
                "answer": ANSWER_EN[zh],
                # --- derived convenience only; see NAIVE_CHECK_RESULT above
                "check_result_naive": NAIVE_CHECK_RESULT[zh],
                "check_result_naive_is_unreliable": True,
                "candidate_actions": proc["candidate_actions"],
                "ground_truth_action": br["target"],
                "ground_truth_kind": br["kind"],
                "followup_step": br["followup_step"],
                "source_line": br["line"],       # 1-based line in cache/<g>.md
                "incomplete_step_ref": br.get("incomplete_step_ref", False),
                "synthesized": synth,
                "excluded_from_eval": bool(reasons),
                "exclusion_reason": "; ".join(reasons) or None,
            })

    graph = {
        "group": g,
        "entry_kind": proc.get("kind", "dtc"),
        "symptom_id": (f"G{g}_SYM{proc['sym_no']}"
                       if proc.get("kind") == "symptom" else None),
        "symptom_title": proc.get("sym_text"),
        "dtc_code": code,
        "dtc_codes": proc["dtc_codes"],
        "dtc_names": proc["dtc_names"],
        "symptom": proc["symptom"],
        "possible_causes": proc["possible_causes"],
        "section_index": proc["section_index"],
        "procedure_index": proc["procedure_index"],
        "entry_step": min(steps) if steps else None,
        "nodes": {
            str(n): {
                "step": n,
                "title": steps[n]["title"],
                "text": step_gist(steps[n]),
                "question": steps[n]["q"],
                "defects": step_defects.get(n, []),
            } for n in sorted(steps)
        },
        "terminals": [{"id": t, "text": x} for t, x in proc["terminal_ids"].items()],
        "edges": [
            {"from": n, "on": ANSWER_EN[br["answer"]], "answer_zh": br["answer"],
             "to": br["target"], "kind": br["kind"],
             "followup_step": br["followup_step"],
             "source_line": br["line"],
             "defects": branch_defects.get((n, br["answer"]), [])}
            for n in sorted(steps) for br in steps[n]["branches"]
        ],
    }
    return recs, graph


# ----------------------------------------------------------------- validation

def raw_counts_for(raw_text):
    deb = debold(raw_text).translate(FULLWIDTH_DIGITS)
    return {
        "進行步驟": len(re.findall(r"進\s*行\s*步\s*驟", deb)),
        "至步驟": len(re.findall(r"(?<!進行)(?<!轉)(?<!跳)至\s*步\s*驟", deb)),
        "other 步驟 refs": len(re.findall(
            r"(?:執行|前往|轉至|跳至|回到|返回|重複)\s*步\s*驟", deb)),
        "是/否 branch lines": len(re.findall(
            r"(?m)^(?:\s*>+)*\s*\**\s*[是否]\s*[:：]", deb)),
    }


def pct(sorted_vals, q):
    """Nearest-rank percentile (no numpy dependency)."""
    if not sorted_vals:
        return 0
    k = max(1, math.ceil(q * len(sorted_vals)))
    return sorted_vals[k - 1]


def candidate_profile(recs, label="candidate-set profile"):
    """len(candidate_actions) for STEP-ROUTING records only -- decides whether a
    single-question Choice fits or tournament sampling is needed."""
    vals = sorted(len(r["candidate_actions"]) for r in recs
                  if r["ground_truth_kind"] == "step")
    if not vals:
        return f"{label} (step-routing records): none"
    n = len(vals)
    over = sum(1 for v in vals if v > 255)
    L = [f"{label} -- len(candidate_actions), step-routing records only "
         f"(n={n}):",
         f"  min {vals[0]}   median {pct(vals, 0.5)}   p90 {pct(vals, 0.9)}   "
         f"max {vals[-1]}",
         f"  records with >255 candidates : {over}"
         + ("" if over else "   (single-question Choice is viable)")]
    return "\n".join(L)


def report(group, procs, doc_counts, raw_counts, issues, recs, provenance):
    L = []
    A = L.append
    A(f"\n{'='*74}\nGROUP {group}\n{'='*74}")
    A(f"extraction: {provenance.get('tool')} "
      f"{provenance.get('version', '')}".rstrip())
    A(f"  cmd: {provenance.get('cmd')}")
    if provenance.get("note"):
        A(f"  note: {provenance['note']}")
    A("")
    A(f"procedures parsed      : {len(procs)}")
    all_codes = {c for p in procs for c in p["dtc_codes"]}
    with_recs = {c for p in procs for c in p["dtc_codes"] if p["steps"]}
    A(f"DTC codes covered      : {len(all_codes)} distinct "
      f"({sum(len(p['dtc_codes']) for p in procs)} header entries)")
    A(f"DTC coverage           : {len(with_recs)} of {len(all_codes)} DTCs "
      f"produce records; {len(all_codes) - len(with_recs)} have a description "
      f"but no decision tree")
    A(f"steps parsed           : {sum(len(p['steps']) for p in procs)}")
    A(f"transitions emitted    : {len(recs)}")
    A("")

    # --- split by record class (step routing vs terminal action)
    step_recs = [r for r in recs if r["ground_truth_kind"] == "step"]
    term_recs = [r for r in recs if r["ground_truth_kind"] == "terminal"]
    A(f"{'record class':<22} {'total':>7} {'eval-eligible':>15} {'excluded':>10}")
    for label, rs in (("step routing", step_recs), ("terminal action", term_recs),
                      ("ALL", recs)):
        ex = sum(1 for r in rs if r["excluded_from_eval"])
        A(f"{label:<22} {len(rs):>7} {len(rs)-ex:>15} {ex:>10}")
    A("")

    defect_recs = [r for r in recs
                   if r["excluded_from_eval"] and not r["synthesized"]]
    synth_recs = [r for r in recs if r["synthesized"]]
    if defect_recs:
        A("excluded: source defects (the manual is wrong; kept in dataset so a")
        A("model that routes better than the source can be scored separately):")
        kinds = Counter()
        for r in defect_recs:
            for reason in r["exclusion_reason"].split("; "):
                if not reason.startswith("synthesized_step"):
                    kinds[(reason.split(":")[0], r["ground_truth_kind"])] += 1
        for (reason, kind), c in sorted(kinds.items()):
            A(f"  {reason:<26} [{kind:<8}] : {c}")
        A("")
    if synth_recs:
        sr = sum(1 for r in synth_recs if r["ground_truth_kind"] == "step")
        A("excluded: parser-synthesized steps (NOT a source defect -- the step "
          "was")
        A("invented by the parser, so it cannot serve as ground truth):")
        A(f"  synthesized_step           [step    ] : {sr}")
        A(f"  synthesized_step           [terminal] : {len(synth_recs) - sr}")
        A(f"  procedures affected                   : "
          f"{len({r['procedure_index'] for r in synth_recs})}")
        A("")

    A(candidate_profile(recs))
    A("")

    A("raw counts (sanity check) -- 'region' = the 診斷故障碼程序 slice only,")
    A("which is what the parser is responsible for; 'doc' = whole file:")
    A(f"  {'':<22} {'region':>8} {'doc':>8}")
    for k in raw_counts:
        A(f"  {k:<22} {raw_counts[k]:>8} {doc_counts[k]:>8}")
    ref_in_terminal = sum(1 for r in term_recs if r["followup_step"])
    numberless = sum(1 for r in recs if r.get("incomplete_step_ref"))
    raw_refs = sum(raw_counts[k] for k in ("進行步驟", "至步驟", "other 步驟 refs"))
    A(f"  step refs embedded in terminal actions : {ref_in_terminal}")
    if numberless:
        A(f"  step refs with no number (source defect): {numberless}")
    got = len(step_recs) + ref_in_terminal + numberless
    A(f"  step transitions + embedded + numberless: {got}"
      f"  (vs all raw 步驟 references = {raw_refs})"
      + ("  MATCH" if got == raw_refs else "  <-- MISMATCH"))
    missing = raw_counts["是/否 branch lines"] - len(recs)
    A(f"  branch lines in region vs emitted      : "
      f"{raw_counts['是/否 branch lines']} vs {len(recs)}"
      + ("  MATCH" if missing == 0 else f"  <-- {missing} DROPPED"))
    outside = doc_counts["是/否 branch lines"] - raw_counts["是/否 branch lines"]
    if outside:
        A(f"  branch lines outside the region        : {outside} "
          f"(preliminary checks / symptom charts; not DTC procedures)")
    A("")
    if not issues:
        A("validation: no issues found.")
    else:
        A("validation issues:")
        for k in sorted(issues):
            v = issues[k]
            A(f"\n  [{k}] ({len(v)})")
            for item in v[:25]:
                A(f"    - {item}")
            if len(v) > 25:
                A(f"    ... {len(v)-25} more")
    return "\n".join(L)


def symptom_report(group, procs, raw_counts, issues, recs):
    L, A = [], None
    A = L.append
    A(f"\n{'-'*74}\nGROUP {group} -- SYMPTOM-KEYED TREES (separate dataset)\n"
      f"{'-'*74}")
    A(f"symptom procedures     : {len(procs)}")
    A(f"steps parsed           : {sum(len(p['steps']) for p in procs)}")
    A(f"transitions emitted    : {len(recs)}")
    step_recs = [r for r in recs if r["ground_truth_kind"] == "step"]
    term_recs = [r for r in recs if r["ground_truth_kind"] == "terminal"]
    A(f"{'record class':<22} {'total':>7} {'eval-eligible':>15} {'excluded':>10}")
    for label, rs in (("step routing", step_recs), ("terminal action", term_recs),
                      ("ALL", recs)):
        ex = sum(1 for r in rs if r["excluded_from_eval"])
        A(f"{label:<22} {len(rs):>7} {len(rs)-ex:>15} {ex:>10}")
    synth = sum(1 for r in recs if r["synthesized"])
    defect = sum(1 for r in recs if r["excluded_from_eval"] and not r["synthesized"])
    A(f"  excluded: {defect} source defect, {synth} parser-synthesized")
    A("")
    A(f"  branch lines in region vs emitted      : "
      f"{raw_counts['是/否 branch lines']} vs {len(recs)}"
      + ("  MATCH" if raw_counts["是/否 branch lines"] == len(recs)
         else f"  <-- {raw_counts['是/否 branch lines']-len(recs)} DROPPED"))
    A("")
    A(candidate_profile(recs))
    if issues:
        A("")
        A("validation issues:")
        for k in sorted(issues):
            v = issues[k]
            A(f"  [{k}] ({len(v)})")
            for item in v[:10]:
                A(f"    - {item}")
            if len(v) > 10:
                A(f"    ... {len(v)-10} more")
    return "\n".join(L)


# ---------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--groups", nargs="+", default=["55"])
    ap.add_argument("--out", default=str(ROOT / "out"))
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    out = Path(args.out)
    (out / "graphs").mkdir(parents=True, exist_ok=True)
    terminals = TerminalRegistry()

    all_recs, report_parts, totals = [], [], []
    sym_recs, sym_reports = [], []
    for group in args.groups:
        raw = extract(group, args.refresh)
        lines = clean_lines(raw)
        procs = parse_group(group, lines, terminals)   # (procs, region)

        recs, issues = [], defaultdict(list)
        procs, region = procs
        for p_ in procs:
            bdef, sdef, iss = analyze_procedure(p_)
            for k, v in iss.items():
                issues[k].extend(v)
            r, graph = emit(p_, bdef, sdef)
            recs.extend(r)
            codes = graph["dtc_codes"]
            if not codes:
                stem = "UNKNOWN"
            elif len(codes) <= 3:
                stem = "_".join(codes)
            else:                       # keep filenames sane for big merges
                stem = f"{codes[0]}_plus{len(codes)-1}"
            name = f"G{group}_{stem}{p_['id_suffix']}.json"
            (out / "graphs" / name).write_text(
                json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")

        # --- second pass: symptom-keyed trees, kept in a separate dataset
        sprocs, sregion = parse_symptom_group(group, lines, terminals)
        if sprocs:
            srecs, sissues = [], defaultdict(list)
            for sp in sprocs:
                bdef, sdef, iss = analyze_procedure(sp)
                for k, v in iss.items():
                    sissues[k].extend(v)
                r, graph = emit(sp, bdef, sdef)
                srecs.extend(r)
                (out / "symptom_graphs").mkdir(parents=True, exist_ok=True)
                (out / "symptom_graphs" / f"G{group}_SYM{sp['sym_no']}.json"
                 ).write_text(json.dumps(graph, ensure_ascii=False, indent=2),
                              encoding="utf-8")
            stext = "\n".join(d for _, d, _, _ in sregion)
            sym_reports.append(symptom_report(group, sprocs,
                                              raw_counts_for(stext),
                                              sissues, srecs))
            sym_recs.extend(srecs)

        region_text = "\n".join(d for _, d, _, _ in region)
        report_parts.append(report(group, procs, raw_counts_for(raw),
                                   raw_counts_for(region_text), issues, recs,
                                   extraction_provenance(group)))
        all_recs.extend(recs)
        totals.append((group, procs, recs))

    with (out / "transitions.jsonl").open("w", encoding="utf-8") as f:
        for r in all_recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    if sym_recs:
        with (out / "symptom_transitions.jsonl").open("w", encoding="utf-8") as f:
            for r in sym_recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    (out / "terminals.json").write_text(
        json.dumps(terminals.dump(), ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- cross-group summary, split by record class
    S = [f"\n{'='*74}\nSUMMARY (all groups)\n{'='*74}"]
    hdr = (f"{'group':<8}{'procs':>7}{'DTCs':>6}{'steps':>7}"
           f"{'step-route':>12}{'(eval)':>8}{'terminal':>10}{'(eval)':>8}{'total':>8}")
    S.append(hdr)
    S.append("-" * len(hdr))
    tot = [0] * 7
    all_codes, codes_all, codes_with = set(), set(), set()
    for group, procs, recs in totals:
        sr = [r for r in recs if r["ground_truth_kind"] == "step"]
        tr = [r for r in recs if r["ground_truth_kind"] == "terminal"]
        row = [len(procs), len({c for p_ in procs for c in p_["dtc_codes"]}),
               sum(len(p_["steps"]) for p_ in procs),
               len(sr), sum(1 for r in sr if not r["excluded_from_eval"]),
               len(tr), sum(1 for r in tr if not r["excluded_from_eval"])]
        all_codes |= {c for p_ in procs for c in p_["dtc_codes"]}
        codes_all |= {c for p_ in procs for c in p_["dtc_codes"]}
        codes_with |= {c for p_ in procs for c in p_["dtc_codes"] if p_["steps"]}
        tot = [a + b for a, b in zip(tot, row)]
        S.append(f"{group:<8}{row[0]:>7}{row[1]:>6}{row[2]:>7}"
                 f"{row[3]:>12}{row[4]:>8}{row[5]:>10}{row[6]:>8}"
                 f"{row[3]+row[5]:>8}")
    S.append("-" * len(hdr))
    tot[1] = len(all_codes)
    S.append(f"{'TOTAL':<8}{tot[0]:>7}{tot[1]:>6}{tot[2]:>7}"
             f"{tot[3]:>12}{tot[4]:>8}{tot[5]:>10}{tot[6]:>8}{tot[3]+tot[5]:>8}")
    S.append("")
    synth = [r for r in all_recs if r["synthesized"]]
    defect = [r for r in all_recs
              if r["excluded_from_eval"] and not r["synthesized"]]
    sd = sum(1 for r in defect if r["ground_truth_kind"] == "step")
    ss = sum(1 for r in synth if r["ground_truth_kind"] == "step")
    S.append(f"eval-eligible step-routing records : {tot[4]}  "
             f"(excluded: {sd} source defect, {ss} parser-synthesized)")
    S.append(f"eval-eligible terminal records     : {tot[6]}  "
             f"(excluded: {len(defect)-sd} source defect, "
             f"{len(synth)-ss} parser-synthesized)")
    S.append("")
    S.append(f"DTC coverage (all groups)          : {len(codes_with)} of "
             f"{len(codes_all)} DTCs produce records; "
             f"{len(codes_all)-len(codes_with)} have a description but no "
             f"decision tree")
    S.append("")
    S.append(candidate_profile(all_recs, "candidate-set profile (all groups)"))

    if sym_recs:
        S.append("")
        S.append(f"symptom-keyed dataset (out/symptom_transitions.jsonl): "
                 f"{len(sym_recs)} transitions, "
                 f"{sum(1 for r in sym_recs if not r['excluded_from_eval'])} "
                 f"eval-eligible -- NOT merged into the DTC dataset")

    text = ("\n".join(report_parts) + "\n" + "\n".join(sym_reports)
            + "\n" + "\n".join(S) + "\n")
    (out / "report.txt").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
