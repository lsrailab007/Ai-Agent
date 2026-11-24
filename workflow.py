"""
workflow.py - the 15-step workflow. The program is in charge; the LLM is asked
for help at four fixed points and never touches the database itself.

   1 sign in                 main.py
   2 authority (code)        get_authority
   3 route                   main.py: /api/report (dashboard) or /api/ask
   4 understand (LLM)        start_ask
   5 confirm (user)          start_ask returns the reading; /confirm continues
   6 get context (tool)      catalog.report_context
   7 produce the query       build_template_sql (certified) or llm.draft_sql (custom)
   8 guardrails (code)       guardrails.verify_sql
   9 cost check (code)       db.estimate_cost
  10 execute (code)          db.run
  11 check the result        check_result
  12 review the meaning      llm.review_sql (custom queries, before they run)
  13 render                  display + commentary with figures inserted by code
  14 label and log           label, store.save_run, store.audit
  15 download / distribute   build_csv, approval in main.py
"""
import csv
import io
import os
import re
from datetime import date, timedelta

from pydantic import BaseModel, Field

import llm as llm_calls
from catalog import CATALOG, DIM_VALUES, MIN_GROUP_SIZE, PERIODS, report_context
from guardrails import MAX_ROWS, prepare_sql, scope_of, verify_sql

MAX_SQL_ATTEMPTS = 3                                   # first draft + two retries
MAX_QUERY_COST = float(os.environ.get("MAX_QUERY_COST", "50"))   # SQL Server estimated cost units


class Authority(BaseModel):
    user_id: str
    name: str
    role: str
    is_owner: bool
    report_types: list[str]
    region: str | None = None
    branch: str | None = None


class ReportRequest(BaseModel):
    metrics: list[str] = Field(default_factory=list, max_length=6)
    group_by: str | None = None
    filters: dict[str, list[str]] = Field(default_factory=dict)
    period: str = "mtd"
    date_from: str | None = None
    date_to: str | None = None


class WorkflowStop(Exception):
    """The workflow stopped on purpose. The message is safe to show to the user."""
    def __init__(self, message: str, detail: list[str] | None = None):
        super().__init__(message)
        self.detail = detail or []


# ---- STEP 2: authority, decided by code from the signed-in user ----------
def get_authority(store, user_id: str) -> Authority | None:
    row = store.authority_row(user_id)
    if not row:
        return None
    return Authority(user_id=row[0], name=row[1], role=row[2], is_owner=bool(row[3]),
                     report_types=[t for t in (row[4] or "").split(",") if t in CATALOG], region=row[5], branch=row[6])


def require_report_type(report_type: str, auth: Authority) -> None:
    if report_type not in CATALOG:
        raise WorkflowStop(f"Unknown report type '{report_type}'.")
    if report_type not in auth.report_types:
        raise WorkflowStop(f"You are not authorised for {CATALOG[report_type]['label']} reports.")


# ---- request checks and the certified SQL template -----------------------
def resolve_period(req: ReportRequest) -> tuple[str, str]:
    today = date.today()
    if req.date_from and req.date_to:
        a, b = date.fromisoformat(req.date_from), date.fromisoformat(req.date_to)
        if a > b:
            raise ValueError("date_from is after date_to")
        return a.isoformat(), b.isoformat()
    spans = {"today": (today, today), "yesterday": (today - timedelta(days=1),) * 2,
             "last_7_days": (today - timedelta(days=6), today), "mtd": (today.replace(day=1), today),
             "ytd": (today.replace(month=1, day=1), today)}
    if req.period == "last_month":
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1).isoformat(), end.isoformat()
    if req.period not in spans:
        raise ValueError(f"Unknown period '{req.period}'. Use one of {PERIODS}")
    a, b = spans[req.period]
    return a.isoformat(), b.isoformat()


def validate_request(report_type: str, req: ReportRequest, need_metrics: bool = True) -> None:
    cat = CATALOG[report_type]
    if need_metrics and not req.metrics:
        raise ValueError("Choose at least one KPI")
    unknown = [m for m in req.metrics if m not in cat["kpis"]]
    if unknown:
        raise ValueError(f"Unknown KPI {unknown} for {cat['label']}. Available: {list(cat['kpis'])}")
    if req.group_by and req.group_by not in cat["dims"]:
        raise ValueError(f"{cat['label']} cannot be split by '{req.group_by}'. Available: {cat['dims']}")
    for dim, values in req.filters.items():
        if dim not in cat["dims"]:
            raise ValueError(f"{cat['label']} cannot be filtered by '{dim}'. Available: {cat['dims']}")
        if not values or any(v not in DIM_VALUES[dim] for v in values):
            raise ValueError(f"Unknown {dim} value in {values}. Available: {DIM_VALUES[dim]}")
    resolve_period(req)


def build_template_sql(report_type: str, req: ReportRequest) -> str:
    """STEP 7, certified path: SQL from approved KPI definitions. Filter values were checked against fixed lists."""
    cat = CATALOG[report_type]
    select = ([req.group_by] if req.group_by else []) + [f"{cat['kpis'][m]['expr']} AS {m}" for m in req.metrics]
    where = ["business_date BETWEEN @date_from AND @date_to"]
    for dim, values in req.filters.items():
        where.append(f"{dim} IN ({', '.join(chr(39) + v + chr(39) for v in values)})")
    sql = f"SELECT TOP {MAX_ROWS} {', '.join(select)} FROM {cat['view']} WHERE {' AND '.join(where)}"
    return sql + (f" GROUP BY {req.group_by} ORDER BY {req.group_by}" if req.group_by else "")


def describe(report_type: str, req: ReportRequest, mode: str, custom_need: str, auth: Authority) -> str:
    """Plain-language reading shown to the user for confirmation. Built by code from catalog labels."""
    cat, (d_from, d_to) = CATALOG[report_type], resolve_period(req)
    what = ", ".join(m.replace("_", " ") for m in req.metrics) if mode == "certified" else f"a custom calculation ({custom_need})"
    parts = [f"{cat['label']} report: {what}"]
    if req.group_by:
        parts.append(f"split by {req.group_by.replace('_', ' ')}")
    for dim, values in req.filters.items():
        parts.append(f"{dim.replace('_', ' ')} = {', '.join(values)}")
    parts.append(f"from {d_from} to {d_to}")
    parts.append(f"limited to your access ({auth.branch or auth.region or 'all regions'})")
    return "; ".join(parts) + "."


# ---- STEPS 4-5: understand the question, return the reading for confirmation
def start_ask(question: str, auth: Authority, llm, store) -> dict:
    try:
        raw = llm_calls.interpret(llm, question, auth)
    except Exception:                                  # fallback: dashboards keep working without the LLM
        raise WorkflowStop("The AI service is not available right now. Dashboard reports still work.")
    outcome = raw.get("outcome")
    if outcome in ("clarification", "decline"):
        store.audit(auth.user_id, "ask_" + outcome, {"question": question})
        return {"type": outcome, "message": str(raw.get("message") or "I could not read that request.")[:400]}
    report_type = raw.get("report_type")
    try:                                               # everything the LLM returned is re-checked by code
        require_report_type(report_type, auth)
        req = ReportRequest(metrics=raw.get("metrics") or [], group_by=raw.get("group_by") or None,
                            filters=raw.get("filters") or {}, period=raw.get("period") or "mtd",
                            date_from=raw.get("date_from"), date_to=raw.get("date_to"))
        mode = "custom" if raw.get("needs_custom_sql") else "certified"
        if mode == "custom" and not CATALOG[report_type]["custom_sql"]:
            raise WorkflowStop(f"{CATALOG[report_type]['label']} reports use certified KPIs only, to protect personal data.")
        if mode == "custom":
            req.metrics = []
        validate_request(report_type, req, need_metrics=(mode == "certified"))
    except (WorkflowStop, ValueError) as e:
        store.audit(auth.user_id, "ask_decline", {"question": question, "reason": str(e)})
        return {"type": "decline", "message": str(e)}
    pending = {"question": question, "report_type": report_type, "request": req.model_dump(), "mode": mode,
               "custom_need": str(raw.get("custom_need") or "")[:200]}
    request_id = store.save_pending(auth.user_id, pending)
    store.audit(auth.user_id, "ask_read", {"request_id": request_id, "question": question, "report_type": report_type, "mode": mode})
    return {"type": "spec", "request_id": request_id, "mode": mode, "report_type": report_type,
            "request": req.model_dump(), "summary": describe(report_type, req, mode, pending["custom_need"], auth)}


def run_confirmed(request_id: str, auth: Authority, llm, db, store) -> dict:
    pending = store.take_pending(request_id, auth.user_id)
    if not pending:
        raise WorkflowStop("This request has expired or was already run. Ask again.")
    return execute_report(pending["report_type"], ReportRequest(**pending["request"]), pending["mode"], auth, llm, db,
                          store, source="ask", question=pending["question"], custom_need=pending["custom_need"])


# ---- STEPS 6-14: produce, verify, run, check, render, label, log ---------
def execute_report(report_type: str, req: ReportRequest, mode: str, auth: Authority, llm, db, store, source: str,
                   question: str = "", custom_need: str = "") -> dict:
    require_report_type(report_type, auth)             # checked again at run time
    try:
        validate_request(report_type, req, need_metrics=(mode == "certified"))
    except ValueError as e:
        raise WorkflowStop(str(e))
    cat, (d_from, d_to) = CATALOG[report_type], resolve_period(req)
    if mode == "custom" and not cat["custom_sql"]:
        raise WorkflowStop(f"{cat['label']} reports use certified KPIs only, to protect personal data.")
    values = {"date_from": d_from, "date_to": d_to, "scope_value": scope_of(auth)[1]}
    context = report_context(report_type)              # STEP 6
    attempts: list[dict] = []
    explanation = ""

    if mode == "certified":                            # STEP 7 + 8
        sql = prepare_sql(build_template_sql(report_type, req), auth)
        violations = verify_sql(sql, report_type, auth)
        if violations:
            store.audit(auth.user_id, "guardrail_block", {"report_type": report_type, "sql": sql, "violations": violations})
            raise WorkflowStop("The report query was blocked by the safety checks.", violations)
    else:
        sql, feedback = None, []
        for n in range(1, MAX_SQL_ATTEMPTS + 1):
            try:
                draft = llm_calls.draft_sql(llm, question, {**req.model_dump(), "custom_need": custom_need}, context, feedback)
            except Exception:
                raise WorkflowStop("The AI service is not available right now. Dashboard reports still work.")
            try:
                candidate = prepare_sql(str(draft.get("sql", "")), auth)        # code adds scope and row limit
                feedback = verify_sql(candidate, report_type, auth)             # STEP 8
            except Exception as e:
                candidate, feedback = str(draft.get("sql", "")), [f"G2 {str(e).splitlines()[0]}"]
            if not feedback:                                                    # STEP 12, before anything runs
                try:
                    review = llm_calls.review_sql(llm, question, candidate, context)
                except Exception:
                    review = {"answers_question": False, "concern": "the review could not be completed"}
                if not review.get("answers_question"):
                    feedback = ["Reviewer: " + str(review.get("concern") or "the query does not answer the question")[:300]]
            attempts.append({"attempt": n, "sql": candidate, "rejected_for": feedback})
            if not feedback:
                sql, explanation = candidate, str(draft.get("explanation", ""))[:300]
                break
        if sql is None:
            store.audit(auth.user_id, "custom_sql_failed", {"question": question, "attempts": attempts})
            raise WorkflowStop("I could not produce a safe query for this request. Try a certified KPI, or rephrase.",
                               attempts[-1]["rejected_for"])

    cost = db.estimate_cost(sql, values, auth)         # STEP 9
    if cost is not None and cost > MAX_QUERY_COST:
        store.audit(auth.user_id, "cost_block", {"sql": sql, "cost": cost})
        raise WorkflowStop("This query would be too heavy for the database. Narrow the period or the filters.")

    columns, rows = db.run(sql, values, auth)          # STEP 10
    totals, warnings = check_result(report_type, req, mode, columns, rows, values, auth, db)   # STEP 11

    commentary = None                                  # STEP 13
    if mode == "certified" and totals and not any("hidden" in w for w in warnings):
        commentary = make_commentary(llm, cat["label"], req, totals, d_from, d_to, auth)
    kpi_meta = {m: cat["kpis"][m] for m in req.metrics} if mode == "certified" else {}
    body = {
        "report_type": report_type, "source": source, "mode": mode, "question": question,
        "period": [d_from, d_to], "scope": auth.branch or auth.region or "all regions",
        "columns": columns, "rows": rows, "totals": totals, "warnings": warnings, "commentary": commentary,
        "display": {"title": f"{cat['label']} report",
                    "columns": [{"key": c, "label": c.replace("_", " ").capitalize(),
                                 "format": kpi_meta[c]["format"] if c in kpi_meta else ("text" if c in cat["dims"] else "number")}
                                for c in columns],
                    "chart": "bar" if req.group_by and rows else "none",
                    "definitions": {m: v["text"] for m, v in kpi_meta.items()}},
        "label": {"certified": mode == "certified",                                       # STEP 14
                  "text": ("Certified KPIs. Query built from approved definitions."
                           if mode == "certified" else
                           "Uncertified. Query written by AI and passed the safety checks. Verify before relying on it.")
                          + (" Request read by AI." if source == "ask" else "")},
        "lineage": {"sql": sql, "explanation": explanation, "parameters": {k: v for k, v in values.items() if v},
                    "estimated_cost": cost, "attempts": len(attempts) or 1, "database": db.name,
                    "model": getattr(llm, "model", llm.name)},
    }
    run_id = store.save_run(auth.user_id, body)
    store.audit(auth.user_id, "run", {"run_id": run_id, "report_type": report_type, "mode": mode, "source": source,
                                      "sql": sql, "parameters": body["lineage"]["parameters"], "rows": len(rows),
                                      "attempts": attempts, "warnings": warnings})
    return body


def check_result(report_type, req, mode, columns, rows, values, auth, db):
    """STEP 11: empty result, totals that must add up, and small groups of people."""
    cat, warnings, totals = CATALOG[report_type], [], {}
    if not rows or all(v is None for r in rows for v in r):
        return totals, ["No data for this selection."]
    if len(rows) >= MAX_ROWS:
        warnings.append(f"Only the first {MAX_ROWS} rows are shown.")
    if mode != "certified":
        return totals, warnings
    if req.group_by:                                   # control total: one extra query without the split
        total_req = req.model_copy(update={"group_by": None})
        total_sql = prepare_sql(build_template_sql(report_type, total_req), auth)
        if verify_sql(total_sql, report_type, auth):
            raise WorkflowStop("The control-total query was blocked by the safety checks.")
        t_cols, t_rows = db.run(total_sql, values, auth)
        totals = dict(zip(t_cols, t_rows[0])) if t_rows else {}
        for m in req.metrics:
            if cat["kpis"][m]["additive"] and len(rows) < MAX_ROWS:
                i = columns.index(m)
                if abs(sum(r[i] or 0 for r in rows) - (totals.get(m) or 0)) > 0.5:
                    raise WorkflowStop("The figures did not add up to the control total, so the report was not released.")
    else:
        totals = dict(zip(columns, rows[0]))
    hidden = 0
    for m in req.metrics:                              # small groups of people are hidden
        if cat["kpis"][m]["people"] and req.group_by:
            i = columns.index(m)
            for r in rows:
                if r[i] is not None and 0 < r[i] < MIN_GROUP_SIZE:
                    r[i], hidden = None, hidden + 1
    if hidden:
        warnings.append(f"{hidden} value(s) below {MIN_GROUP_SIZE} people are hidden to protect individuals.")
    return totals, warnings


def _fmt(value, fmt: str) -> str:
    if value is None:
        return "not available"
    return f"{value * 100:.1f}%" if fmt == "percent" else (f"{value:,.1f}" if fmt == "decimal" else f"{value:,.0f}")


def make_commentary(llm, label, req, totals, d_from, d_to, auth) -> dict | None:
    """STEP 13: the LLM writes with {{ids}}; code checks the text and inserts the figures."""
    cat = next(c for c in CATALOG.values() if c["label"] == label)
    facts = [{"id": "f1", "label": "Report, scope and period",
              "value": f"{label}, {auth.branch or auth.region or 'all regions'}, {d_from} to {d_to}"}]
    for m in req.metrics:
        facts.append({"id": f"f{len(facts) + 1}", "label": cat["kpis"][m]["text"],
                      "value": _fmt(totals.get(m), cat["kpis"][m]["format"])})
    try:
        text = str(llm_calls.write_commentary(llm, facts).get("text", "")).strip()
    except Exception:
        return None
    ids = {f["id"] for f in facts}
    refs = re.findall(r"\{\{(\w+)\}\}", text)
    if not refs or set(refs) - ids or len(text) > 500 or re.search(r"\d", re.sub(r"\{\{\w+\}\}", "", text)):
        return None                                    # failed the check: the report goes out without commentary
    by_id = {f["id"]: f["value"] for f in facts}
    return {"text": re.sub(r"\{\{(\w+)\}\}", lambda m: by_id[m.group(1)], text),
            "note": "Commentary drafted by AI. Every figure was inserted and checked by code."}


# ---- STEP 15: CSV built on the server from the stored run ----------------
def build_csv(run: dict) -> str:
    def safe(v):                                       # stops spreadsheet formulas hidden in text cells
        return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([c["label"] for c in run["display"]["columns"]])
    for r in run["rows"]:
        w.writerow([safe(v) if v is not None else "" for v in r])
    w.writerow([])
    w.writerow([f"Run {run['run_id']}", f"Period {run['period'][0]} to {run['period'][1]}", f"Scope {run['scope']}"])
    w.writerow([run["label"]["text"]])
    return out.getvalue()
