"""
Demo scenarios for jevdemo.py. Edit freely — nothing else needs to change.

Two model calls, not one.

  call 1   depends only on the query and the catalog: does it split, which
           sources does it need, how much model does it take, is it live data
  gate     ordinary code: the classification a query needs is max() over the
           classifications of the sources it selected, compared against the
           user's access level
  call 2   only runs if the gate passed: the semantic constraints that decide
           routing — latency sensitivity and data sovereignty
  routing  ordinary code: filter endpoints by capability and sovereignty, then
           pick by RTT or by cost

Nothing here asks the model to do arithmetic or a table lookup. The old
`classification` question did exactly that and is gone: the catalog already
knows each source's classification, so once the model says which sources a
query needs, the classification it needs is computed — and therefore always
right, instead of usually right.
"""

# ---------------------------------------------------------------- catalog ---

CATALOG = [
    {
        "id": "eu-finance-db",
        "what": "EU entity financial records: revenue, cost, headcount",
        "classification": "restricted",
        "region": "Frankfurt",
    },
    {
        "id": "global-finance-summary",
        "what": "Published quarterly summaries, all regions",
        "classification": "public",
        "region": "Silicon Valley",
    },
    {
        "id": "incident-db",
        "what": "Network and platform incident reports",
        "classification": "internal",
        "region": "Frankfurt",
    },
    {
        "id": "hr-records",
        "what": "Employee records, compensation, performance",
        "classification": "restricted",
        "region": "Frankfurt",
    },
    {
        "id": "product-docs",
        "what": "Public product documentation and specs",
        "classification": "public",
        "region": "Silicon Valley",
    },
]

CATALOG_BY_ID = {entry["id"]: entry for entry in CATALOG}

ACCESS_RANK = {"public": 0, "internal": 1, "restricted": 2}

# A use_* answer above this means the query needs that source.
SOURCE_THRESHOLD = 0.5


def source_question_key(source_id):
    """Catalog id -> question key. 'eu-finance-db' -> 'use_eu_finance_db'."""
    return "use_" + source_id.replace("-", "_")


# question key -> catalog id, for reading the answers back
SOURCE_BY_QUESTION_KEY = {source_question_key(e["id"]): e["id"] for e in CATALOG}


# ------------------------------------------------------------- call 1 ------

# SCALE NOTE
#
# One noul per source works to roughly 20 sources. The questions are
# independent, they are cheap, and a query that genuinely needs three sources
# can say so without the answers having to compete for probability mass.
#
# Past ~20 the prompt gets long and independent yes/no answers start to drift.
# Switch to a single `choice` over sources and take everything above a
# probability threshold: one distribution instead of N booleans, at the cost of
# making the sources compete.
#
# Past 255 a flat choice stops being workable. Go hierarchical — one choice
# over domains (finance / infrastructure / people / product), then a second
# call that chooses a source within the winning domain. Two round trips, but
# each distribution stays small enough to mean something.
#
# The demo has five sources, so none of this applies here.

CALL1_QUESTIONS = {
    "should_split": {
        "type": "noul",
        "instructions": (
            "Does this query contain more than one distinct request that would "
            "need to be answered separately?"
        ),
    },
}

for _entry in CATALOG:
    CALL1_QUESTIONS[source_question_key(_entry["id"])] = {
        "type": "noul",
        "instructions": "Would answering this query require data from %s: %s?"
        % (_entry["id"], _entry["what"]),
    }

CALL1_QUESTIONS["capability_needed"] = {
    "type": "score",
    "instructions": "What level of model capability does answering this query require?",
    "criteria": [
        "lookup only, no generation needed",
        "extraction or summarization from retrieved text",
        "reasoning across multiple sources",
        "open-ended analysis",
    ],
}

CALL1_QUESTIONS["needs_live_data"] = {
    "type": "noul",
    "instructions": (
        "Does this require fetching live or real-time data, as opposed to "
        "stored records?"
    ),
}


# ------------------------------------------------------------- call 2 ------

# Semantic constraints only. Which endpoint to use is arithmetic on RTT and
# cost, so code does that — see pick_endpoint in jevdemo.py.

CALL2_QUESTIONS = {
    "latency_sensitive": {
        "type": "noul",
        "instructions": (
            "Is this an interactive request where a slow answer is a bad "
            "answer, as opposed to a background or batch job?"
        ),
    },
    "sovereignty_constrained": {
        "type": "noul",
        "instructions": (
            "Must this data be processed inside the region it is stored in?"
        ),
    },
}


# ----------------------------------------------------------- endpoints -----

# capability is the same 0-3 scale as capability_needed.
# cost is USD per million output tokens, used for ranking only.

MODELS = [
    {"id": "haiku-fra",    "capability": 1, "cost": 0.25,  "region": "Frankfurt",      "on_prem": False},
    {"id": "sonnet-fra",   "capability": 2, "cost": 3.00,  "region": "Frankfurt",      "on_prem": False},
    {"id": "llama-fra-op", "capability": 2, "cost": 0.00,  "region": "Frankfurt",      "on_prem": True},
    {"id": "haiku-sjc",    "capability": 1, "cost": 0.25,  "region": "Silicon Valley", "on_prem": False},
    {"id": "sonnet-sjc",   "capability": 2, "cost": 3.00,  "region": "Silicon Valley", "on_prem": False},
    {"id": "opus-sjc",     "capability": 3, "cost": 15.00, "region": "Silicon Valley", "on_prem": False},
    {"id": "haiku-sin",    "capability": 1, "cost": 0.25,  "region": "Singapore",      "on_prem": False},
    {"id": "sonnet-lon",   "capability": 2, "cost": 3.00,  "region": "London",         "on_prem": False},
]

REGIONS = ["Frankfurt", "London", "Silicon Valley", "Singapore"]

# One direction only; _rtt_matrix mirrors it.
_BASE_RTT = {
    ("Frankfurt", "Frankfurt"): 2,
    ("Frankfurt", "London"): 14,
    ("Frankfurt", "Silicon Valley"): 148,
    ("Frankfurt", "Singapore"): 165,
    ("London", "London"): 2,
    ("London", "Silicon Valley"): 138,
    ("London", "Singapore"): 178,
    ("Silicon Valley", "Silicon Valley"): 2,
    ("Silicon Valley", "Singapore"): 172,
    ("Singapore", "Singapore"): 2,
}


def _rtt_matrix(queue_ms=None):
    """Effective RTT = propagation + queueing at the destination.

    Propagation is a property of distance and never changes. Congestion is
    queueing at the endpoint you are trying to reach, so it is added at the
    destination, not multiplied along the path. This is what makes a congested
    local endpoint worse than a healthy remote one — which is the whole point
    of the network selector.
    """
    queue_ms = queue_ms or {}
    matrix = {}
    for (a, b), base in _BASE_RTT.items():
        for x, y in ((a, b), (b, a)):
            matrix.setdefault(x, {})[y] = base + int(queue_ms.get(y, 0))
    return matrix


def _levels(overrides=None):
    levels = {r: "none" for r in REGIONS}
    levels.update(overrides or {})
    return levels


NETWORK = {
    "baseline": {
        "label": "Baseline",
        "note": "no queueing anywhere",
        "rtt_ms": _rtt_matrix(),
        "congestion": _levels(),
    },
    "sjc-congested": {
        "label": "SV congested",
        "note": "SV endpoints queue ~260ms; a SV user is better off in Frankfurt",
        "rtt_ms": _rtt_matrix({"Silicon Valley": 260}),
        "congestion": _levels({"Silicon Valley": "high"}),
    },
    "apac-degraded": {
        "label": "APAC degraded",
        "note": "Singapore heavily queued, SV mildly",
        "rtt_ms": _rtt_matrix({"Singapore": 240, "Silicon Valley": 45}),
        "congestion": _levels({"Singapore": "high", "Silicon Valley": "moderate"}),
    },
}

DEFAULT_NETWORK = "baseline"


# ----------------------------------------------------------- scenarios -----

SCENARIOS = [
    {
        "name": "Restricted financials",
        "note": "internal user, restricted data → blocked",
        "state": {
            "user": {"id": "analyst@corp", "region": "Frankfurt",
                     "access_level": "internal", "role": "financial analyst"},
            "query": "Show me Q3 revenue breakdown for the EU entity",
            "catalog": CATALOG,
        },
    },
    {
        "name": "Incident summary",
        "note": "internal user, internal data → allowed",
        "state": {
            "user": {"id": "engineer@corp", "region": "Frankfurt",
                     "access_level": "internal", "role": "platform engineer"},
            "query": "Summarize the latest network incident report",
            "catalog": CATALOG,
        },
    },
    {
        "name": "Two requests in one",
        "note": "one query, two classifications → split",
        "state": {
            "user": {"id": "engineer@corp", "region": "Frankfurt",
                     "access_level": "internal", "role": "platform engineer"},
            "query": "What happened with the EU platform issue last quarter and did it affect revenue?",
            "catalog": CATALOG,
        },
    },
    {
        "name": "Public lookup",
        "note": "public data, no generation needed",
        "state": {
            "user": {"id": "sales@corp", "region": "Silicon Valley",
                     "access_level": "public", "role": "account executive"},
            "query": "What is the maximum port speed on the standard cross connect?",
            "catalog": CATALOG,
        },
    },
    {
        "name": "Allowed for restricted",
        "note": "same query as #1, restricted user → allowed",
        "state": {
            "user": {"id": "cfo@corp", "region": "Frankfurt",
                     "access_level": "restricted", "role": "finance director"},
            "query": "Show me Q3 revenue breakdown for the EU entity",
            "catalog": CATALOG,
        },
    },
    {
        "name": "Cross-region analysis",
        "note": "sovereignty vs. the nearest endpoint",
        "state": {
            "user": {"id": "cfo@corp", "region": "Silicon Valley",
                     "access_level": "restricted", "role": "finance director"},
            "query": "Compare EU entity cost structure against the published global summary and explain the gap",
            "catalog": CATALOG,
        },
    },
]

# Both question sets travel with every scenario so the request panel can show
# exactly what goes out, and you can edit either one before sending.
for _s in SCENARIOS:
    _s["questions1"] = CALL1_QUESTIONS
    _s["questions2"] = CALL2_QUESTIONS
