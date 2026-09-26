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
    {
        "id": "idea-pool",
        "what": "Proposed and in-flight internal tools, with owners and phases",
        "classification": "internal",
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


# The one list that both the task_type question and MODELS.task_fit draw from.
# Keeping them on the same vocabulary is what lets code match them; a model is
# never matched against a person's job title.
TASK_TYPES = ["config", "troubleshooting", "explanation", "summarization", "analysis"]

ANSWER_STYLES = ["guided explanation", "config only"]

ROLES = ["seller", "solution engineer", "network architect"]


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

# All call-1 questions go out in one request, including ones the chosen
# branch will ignore. On Jev hosted, extra questions cost almost nothing,
# while a second round trip costs ~300ms. Revisit if we move to a
# self-hosted layer like AnyJev, where each choice question costs K
# prefills, or if state + the longest question approaches 32k tokens.
# Either would justify asking intent first and branching.
CALL1_QUESTIONS = {
    "intent": {
        "type": "choice",
        "instructions": "Is this a request for an answer, or an idea for something to build?",
        "criteria": {
            "answer": {
                "what": "a question to be answered from data that already exists",
                "not_for": "proposals for something new to be built",
            },
            "build": {
                "what": "a proposal for a tool, agent or system someone wants to build",
                "not_for": "questions answerable from existing records",
            },
        },
    },
    "task_type": {
        "type": "choice",
        "instructions": "What kind of task is this?",
        "criteria": {
            "config": {"what": "produce or change a configuration, rule or script"},
            "troubleshooting": {"what": "work out why something is broken or behaving oddly"},
            "explanation": {"what": "explain how something works or what something means"},
            "summarization": {"what": "condense existing material into a shorter form"},
            "analysis": {"what": "compare, aggregate or reason across several things"},
        },
    },
    "answer_style_needed": {
        "type": "choice",
        "instructions": (
            "Given who is asking and what they asked, which kind of answer would "
            "serve them?"
        ),
        "criteria": {
            "guided explanation": {
                "what": "reasoning and context spelled out, for someone who does "
                        "not work in this area daily",
            },
            "config only": {
                "what": "the artefact itself with minimal prose, for someone who "
                        "will read it as reference",
            },
        },
    },
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

# Seeded ideas. Deliberately adjacent: two of them work on the same workshop
# transcripts and differ only in what they produce, so a match has to
# discriminate rather than keyword-spot.
IDEAS = [
    {
        "id": "solution-workshop-agent",
        "title": "Solution Workshop Agent",
        "description": (
            "Summarizes customer workshop transcripts and generates the "
            "deliverable decks that go back to the customer."
        ),
        "owner": "Lily",
        "phase": "Phase I",
        "data_classes": ["restricted customer data"],
        "region": "US",
        "runtime": "sandboxed container, on-demand",
    },
    {
        "id": "workshop-transcript-index",
        "title": "Workshop Transcript Index",
        "description": (
            "Indexes the same workshop transcripts so past sessions can be "
            "searched later. Retrieval, not deck generation."
        ),
        "owner": "Priya",
        "phase": "Phase II",
        "data_classes": ["restricted customer data"],
        "region": "EU",
        "runtime": "always-on service",
    },
    {
        "id": "incident-postmortem-writer",
        "title": "Incident Postmortem Writer",
        "description": (
            "Drafts postmortems from incident timelines and on-call chat logs."
        ),
        "owner": "Kenji",
        "phase": "Phase II",
        "data_classes": ["internal"],
        "region": "EU",
        "runtime": "batch, triggered on incident close",
    },
    {
        "id": "capacity-forecast-bot",
        "title": "Capacity Forecast Bot",
        "description": (
            "Forecasts cabinet and power utilisation per metro from facility "
            "telemetry."
        ),
        "owner": "Sven",
        "phase": "Phase III",
        "data_classes": ["internal"],
        "region": "EU",
        "runtime": "scheduled, nightly",
    },
    {
        "id": "contract-clause-extractor",
        "title": "Contract Clause Extractor",
        "description": (
            "Pulls renewal dates and SLA terms out of signed customer contracts."
        ),
        "owner": "Dana",
        "phase": "Phase I",
        "data_classes": ["restricted legal"],
        "region": "US",
        "runtime": "batch, on upload",
    },
]

IDEAS_BY_ID = {i["id"]: i for i in IDEAS}

# A data class counts as restricted when it says so. Kept as a substring test
# so "restricted customer data" and "restricted legal" both trip it without a
# second vocabulary to maintain.
def idea_is_restricted(idea):
    return any("restricted" in c.lower() for c in idea.get("data_classes", []))


CALL1_QUESTIONS["idea_match"] = {
    "type": "choice",
    "instructions": "Which existing idea is this closest to?",
    "criteria": dict(
        {i["id"]: {"what": i["description"]} for i in IDEAS},
        none={"what": "not close to any of these"},
    ),
}

CALL1_QUESTIONS["idea_action"] = {
    "type": "choice",
    "instructions": (
        "Should this person join the existing idea, fork it for a different "
        "need, or start something new?"
    ),
    "criteria": {
        "join": {"what": "the same need; contribute to the existing effort"},
        "fork": {"what": "close, but a different need the existing idea does not serve"},
        "new": {"what": "unrelated to anything already proposed"},
    },
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

# task_fit holds task types, never roles. A person's role reaches this table
# only through answer_style_needed, which the model answers after reading role
# and skill — so fit is judged per person and per question, not assigned by job
# title. A seller asking a precise config question still gets the config model.
#
# latency_tier / cost_tier are the coarse bands used when ranking; the exact
# RTT and dollar figures stay for the arithmetic.
MODELS = [
    {"id": "haiku-fra",    "capability": 1, "cost": 0.25,  "region": "Frankfurt",      "on_prem": False,
     "task_fit": ["summarization", "explanation"],                "answer_style": "guided explanation",
     "latency_tier": 1, "cost_tier": 1},
    {"id": "sonnet-fra",   "capability": 2, "cost": 3.00,  "region": "Frankfurt",      "on_prem": False,
     "task_fit": ["explanation", "troubleshooting", "analysis"],  "answer_style": "guided explanation",
     "latency_tier": 2, "cost_tier": 2},
    {"id": "llama-fra-op", "capability": 2, "cost": 0.00,  "region": "Frankfurt",      "on_prem": True,
     "task_fit": ["config", "troubleshooting"],                   "answer_style": "config only",
     "latency_tier": 2, "cost_tier": 0},
    {"id": "haiku-sjc",    "capability": 1, "cost": 0.25,  "region": "Silicon Valley", "on_prem": False,
     "task_fit": ["summarization", "explanation"],                "answer_style": "guided explanation",
     "latency_tier": 1, "cost_tier": 1},
    {"id": "sonnet-sjc",   "capability": 2, "cost": 3.00,  "region": "Silicon Valley", "on_prem": False,
     "task_fit": ["explanation", "troubleshooting", "analysis"],  "answer_style": "guided explanation",
     "latency_tier": 2, "cost_tier": 2},
    {"id": "opus-sjc",     "capability": 3, "cost": 15.00, "region": "Silicon Valley", "on_prem": False,
     "task_fit": ["analysis", "explanation", "troubleshooting"],  "answer_style": "guided explanation",
     "latency_tier": 3, "cost_tier": 3},
    {"id": "haiku-sin",    "capability": 1, "cost": 0.25,  "region": "Singapore",      "on_prem": False,
     "task_fit": ["summarization"],                               "answer_style": "guided explanation",
     "latency_tier": 1, "cost_tier": 1},
    {"id": "sonnet-lon",   "capability": 2, "cost": 3.00,  "region": "London",         "on_prem": False,
     "task_fit": ["config", "explanation"],                       "answer_style": "config only",
     "latency_tier": 2, "cost_tier": 2},
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
                     "access_level": "internal", "role": "solution engineer",
                     "skill": "reads finance dashboards, no hands-on networking"},
            "query": "Show me Q3 revenue breakdown for the EU entity",
            "catalog": CATALOG,
        },
    },
    {
        "name": "Incident summary",
        "note": "internal user, internal data → allowed",
        "state": {
            "user": {"id": "engineer@corp", "region": "Frankfurt",
                     "access_level": "internal", "role": "network architect",
                     "skill": "writes BGP config daily"},
            "query": "Summarize the latest network incident report",
            "catalog": CATALOG,
        },
    },
    {
        "name": "Two requests in one",
        "note": "one query, two classifications → split",
        "state": {
            "user": {"id": "engineer@corp", "region": "Frankfurt",
                     "access_level": "internal", "role": "network architect",
                     "skill": "writes BGP config daily"},
            "query": "What happened with the EU platform issue last quarter and did it affect revenue?",
            "catalog": CATALOG,
        },
    },
    {
        "name": "Public lookup",
        "note": "public data, no generation needed",
        "state": {
            "user": {"id": "sales@corp", "region": "Silicon Valley",
                     "access_level": "public", "role": "seller",
                     "skill": "sells Fabric, no hands-on networking"},
            "query": "What is the maximum port speed on the standard cross connect?",
            "catalog": CATALOG,
        },
    },
    {
        "name": "Allowed for restricted",
        "note": "same query as #1, restricted user → allowed",
        "state": {
            "user": {"id": "cfo@corp", "region": "Frankfurt",
                     "access_level": "restricted", "role": "seller",
                     "skill": "reads board packs, delegates every technical call"},
            "query": "Show me Q3 revenue breakdown for the EU entity",
            "catalog": CATALOG,
        },
    },
    {
        "name": "Cross-region analysis",
        "note": "sovereignty vs. the nearest endpoint",
        "state": {
            "user": {"id": "cfo@corp", "region": "Silicon Valley",
                     "access_level": "restricted", "role": "seller",
                     "skill": "reads board packs, delegates every technical call"},
            "query": "Compare EU entity cost structure against the published global summary and explain the gap",
            "catalog": CATALOG,
        },
    },
]

SCENARIOS += [
    {
        "name": "Same question, seller",
        "note": "seller asks a config question \u2192 guided explanation",
        "state": {
            "user": {"id": "sales@corp", "region": "Frankfurt",
                     "access_level": "internal", "role": "seller",
                     "skill": "sells Fabric, no hands-on networking"},
            "query": "How do I set up BGP peering on a new Fabric port?",
            "catalog": CATALOG,
        },
    },
    {
        "name": "Same question, architect",
        "note": "same words, network architect \u2192 config only",
        "state": {
            "user": {"id": "engineer@corp", "region": "Frankfurt",
                     "access_level": "internal", "role": "network architect",
                     "skill": "writes BGP config daily"},
            "query": "How do I set up BGP peering on a new Fabric port?",
            "catalog": CATALOG,
        },
    },
    {
        "name": "Build: workshop decks",
        "note": "build intent \u2192 idea branch, matches an existing idea",
        "state": {
            "user": {"id": "lily@corp", "region": "Silicon Valley",
                     "access_level": "internal", "role": "solution engineer",
                     "skill": "runs customer workshops, light scripting"},
            "query": ("I want to build something that takes our customer workshop "
                      "recordings and turns them into the deck we hand back"),
            "catalog": CATALOG,
        },
    },
    {
        "name": "Build: search past workshops",
        "note": "adjacent to the same idea \u2192 fork, not join",
        "state": {
            "user": {"id": "priya@corp", "region": "Frankfurt",
                     "access_level": "internal", "role": "solution engineer",
                     "skill": "runs customer workshops, light scripting"},
            "query": ("Could we build a way to search everything that was said in "
                      "past customer workshops?"),
            "catalog": CATALOG,
        },
    },
]

# Both question sets travel with every scenario so the request panel can show
# exactly what goes out, and you can edit either one before sending.
for _s in SCENARIOS:
    _s["questions1"] = CALL1_QUESTIONS
    _s["questions2"] = CALL2_QUESTIONS
