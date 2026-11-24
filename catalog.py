"""
catalog.py - what can be reported.

One entry per report type. Everything else (context tool, SQL builder, guardrails,
portal display) reads from here, so a new KPI or report type is added in one place.
"""
REGIONS = {"North": ["N-01", "N-02", "N-03"], "South": ["S-01", "S-02", "S-03"],
           "East": ["E-01", "E-02"], "West": ["W-01", "W-02"]}
DIM_VALUES = {
    "region": list(REGIONS), "branch": [b for bs in REGIONS.values() for b in bs],
    "channel": ["Agency", "Bancassurance", "Broker", "Direct"],
    "product": ["Protection", "Savings", "Unit-linked", "Annuity"],
    "age_band": ["18-30", "31-45", "46-60", "61+"],
    "request_type": ["Address change", "Nominee change", "Premium mode change", "Duplicate policy"],
    "surrender_type": ["FULL", "PART"],
}
PERIODS = ["today", "yesterday", "last_7_days", "mtd", "last_month", "ytd"]
MIN_GROUP_SIZE = 5      # people counts below this are hidden in client and agent reports


def _kpi(expr, text, fmt="int", additive=True, people=False):
    return {"expr": expr, "text": text, "format": fmt, "additive": additive, "people": people}


# view      : the read-only database view for this report type (the only object a query may read)
# measures  : numeric columns in the view, with a plain description for the LLM
# kpis      : certified measures. expr is T-SQL. additive=False for ratios.
# custom_sql: may the LLM write its own query for this type? Off for people data.
CATALOG = {
    "client": {
        "label": "Client", "view": "v_client_daily", "dims": ["region", "branch", "age_band"], "custom_sql": False,
        "measures": {"new_clients": "clients with a first policy issued that day",
                     "existing_clients": "existing clients who bought another policy that day"},
        "kpis": {"new_clients": _kpi("SUM(new_clients)", "Clients whose first policy was issued in the period", people=True),
                 "existing_clients": _kpi("SUM(existing_clients)", "Existing clients who bought another policy", people=True)}},
    "agent": {
        "label": "Agent", "view": "v_agent_daily", "dims": ["region", "branch", "channel"], "custom_sql": False,
        "measures": {"new_recruits": "agents licensed that day", "policies_sold": "policies issued through agents",
                     "premium_sold": "first-year premium through agents"},
        "kpis": {"new_recruits": _kpi("SUM(new_recruits)", "Agents licensed in the period", people=True),
                 "policies_sold": _kpi("SUM(policies_sold)", "Policies issued through agents, branch level and above"),
                 "premium_sold": _kpi("SUM(premium_sold)", "First-year premium through agents", "money")}},
    "proposal": {
        "label": "Proposal", "view": "v_proposal_daily", "dims": ["region", "branch", "channel", "product"], "custom_sql": True,
        "measures": {"received": "proposals logged", "issued": "proposals that became policies",
                     "not_taken_up": "proposals accepted but not completed by the client"},
        "kpis": {"proposals_received": _kpi("SUM(received)", "Proposals logged in the period"),
                 "proposals_issued": _kpi("SUM(issued)", "Proposals that became policies"),
                 "not_taken_up": _kpi("SUM(not_taken_up)", "Proposals accepted but not completed by the client"),
                 "issuance_ratio": _kpi("1.0 * SUM(issued) / NULLIF(SUM(received), 0)", "Issued divided by received", "percent", False)}},
    "new_business": {
        "label": "New business", "view": "v_nb_daily", "dims": ["region", "branch", "channel", "product"], "custom_sql": True,
        "measures": {"policies": "policies issued", "premium": "first-year premium", "ape": "annualised premium equivalent"},
        "kpis": {"policies_issued": _kpi("SUM(policies)", "Policies issued in the period"),
                 "first_year_premium": _kpi("SUM(premium)", "First-year premium on policies issued", "money"),
                 "ape": _kpi("SUM(ape)", "Annualised premium equivalent", "money"),
                 "avg_premium": _kpi("1.0 * SUM(premium) / NULLIF(SUM(policies), 0)", "Premium divided by policies issued", "money", False)}},
    "policy_servicing": {
        "label": "Policy servicing", "view": "v_servicing_daily", "dims": ["region", "branch", "request_type"], "custom_sql": True,
        "measures": {"received": "service requests received", "completed": "service requests completed",
                     "tat_days_total": "total days taken by completed requests"},
        "kpis": {"requests_received": _kpi("SUM(received)", "Service requests received"),
                 "requests_completed": _kpi("SUM(completed)", "Service requests completed"),
                 "avg_turnaround_days": _kpi("1.0 * SUM(tat_days_total) / NULLIF(SUM(completed), 0)", "Average days to complete", "decimal", False)}},
    "claims": {
        "label": "Claims", "view": "v_claims_daily", "dims": ["region", "branch", "product"], "custom_sql": True,
        "measures": {"registered": "claims registered", "settled": "claims settled", "paid_amount": "amount paid on settled claims"},
        "kpis": {"claims_registered": _kpi("SUM(registered)", "Claims registered"),
                 "claims_settled": _kpi("SUM(settled)", "Claims settled"),
                 "paid_amount": _kpi("SUM(paid_amount)", "Amount paid on settled claims", "money")}},
    "surrender": {
        "label": "Surrender", "view": "v_surrender_daily", "dims": ["region", "branch", "product", "surrender_type"], "custom_sql": True,
        "measures": {"surrenders": "surrender transactions", "amount": "surrender value paid"},
        "kpis": {"surrenders": _kpi("SUM(surrenders)", "Surrender transactions; filter surrender_type FULL or PART"),
                 "surrender_amount": _kpi("SUM(amount)", "Surrender value paid", "money")}},
}
for _c in CATALOG.values():
    _c["columns"] = {"business_date", *_c["dims"], *_c["measures"]}


def report_context(report_type: str) -> dict:
    """STEP 6 tool: the view, fields and KPI definitions the LLM may use for one report type."""
    c = CATALOG[report_type]
    return {"report_type": report_type, "view": c["view"],
            "columns": {"business_date": "date of the business day (DATE)",
                        **{d: "one of " + ", ".join(DIM_VALUES[d]) for d in c["dims"]}, **c["measures"]},
            "certified_kpis": {k: {"definition": v["text"], "sql": v["expr"]} for k, v in c["kpis"].items()},
            "custom_sql_allowed": c["custom_sql"]}
