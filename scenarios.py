"""
Demo scenarios for jevdemo.py. Edit freely — nothing else needs to change.

Each scenario is a name, a one-line note for the picker button, a `state`
object and a `questions` object. `state` and `questions` are sent to the
API exactly as written here.

CLEARANCE_RANK is used by the gate in jevdemo.py to compare a person's
clearance against the classification the model says the query needs.
"""

CATALOG = [
    {
        "id": "eu-finance-db",
        "what": "EU entity financial records: revenue, cost, headcount",
        "classification": "restricted",
        "region": "FRA",
    },
    {
        "id": "global-finance-summary",
        "what": "Published quarterly summaries, all regions",
        "classification": "public",
        "region": "SJC",
    },
    {
        "id": "incident-db",
        "what": "Network and platform incident reports",
        "classification": "internal",
        "region": "FRA",
    },
    {
        "id": "hr-records",
        "what": "Employee records, compensation, performance",
        "classification": "restricted",
        "region": "FRA",
    },
    {
        "id": "product-docs",
        "what": "Public product documentation and specs",
        "classification": "public",
        "region": "SJC",
    },
]

GATE_QUESTIONS = {
    "confidentiality": {
        "type": "score",
        "instructions": "What is the highest data classification this query would need to touch?",
        "criteria": ["public", "internal", "restricted"],
    },
    "data_source": {
        "type": "choice",
        "instructions": "Which catalog entry is the best source for answering this query?",
        "criteria": {
            "eu-finance-db": {
                "what": "EU entity internal financial detail",
                "not_for": "figures already published externally",
            },
            "global-finance-summary": {
                "what": "published quarterly summaries",
                "not_for": "entity-level internal detail",
            },
            "incident-db": {
                "what": "network and platform incident reports",
                "not_for": "financial impact of an incident",
            },
            "hr-records": {"what": "employee records, pay, performance"},
            "product-docs": {"what": "public product documentation and specs"},
        },
    },
    "capability_needed": {
        "type": "score",
        "instructions": "What level of model capability does answering this query require?",
        "criteria": [
            "lookup only, no generation needed",
            "extraction or summarization from retrieved text",
            "reasoning across multiple sources",
            "open-ended analysis",
        ],
    },
    "needs_live_data": {
        "type": "noul",
        "instructions": "Does this require fetching live or real-time data, as opposed to stored records?",
    },
    "compound": {
        "type": "noul",
        "instructions": "Does this query contain more than one distinct request that would need to be split?",
    },
}

CLEARANCE_RANK = {"public": 0, "internal": 1, "restricted": 2}

SCENARIOS = [
    {
        "name": "Restricted financials",
        "note": "clearance too low",
        "state": {
            "user": {"id": "analyst@corp", "region": "FRA", "clearance": "internal",
                     "role": "financial analyst"},
            "query": "Show me Q3 revenue breakdown for the EU entity",
            "catalog": CATALOG,
        },
        "questions": GATE_QUESTIONS,
    },
    {
        "name": "Incident summary",
        "note": "cleared, routes locally",
        "state": {
            "user": {"id": "engineer@corp", "region": "FRA", "clearance": "internal",
                     "role": "platform engineer"},
            "query": "Summarize the latest network incident report",
            "catalog": CATALOG,
        },
        "questions": GATE_QUESTIONS,
    },
    {
        "name": "Two questions in one",
        "note": "compound, source splits",
        "state": {
            "user": {"id": "engineer@corp", "region": "FRA", "clearance": "internal",
                     "role": "platform engineer"},
            "query": "What happened with the EU platform issue last quarter and did it affect revenue?",
            "catalog": CATALOG,
        },
        "questions": GATE_QUESTIONS,
    },
    {
        "name": "Public lookup",
        "note": "should need no generation",
        "state": {
            "user": {"id": "sales@corp", "region": "SJC", "clearance": "public",
                     "role": "account executive"},
            "query": "What is the maximum port speed on the standard cross connect?",
            "catalog": CATALOG,
        },
        "questions": GATE_QUESTIONS,
    },
    {
        "name": "Cleared for restricted",
        "note": "same query as #1, higher clearance",
        "state": {
            "user": {"id": "cfo@corp", "region": "FRA", "clearance": "restricted",
                     "role": "finance director"},
            "query": "Show me Q3 revenue breakdown for the EU entity",
            "catalog": CATALOG,
        },
        "questions": GATE_QUESTIONS,
    },
]

