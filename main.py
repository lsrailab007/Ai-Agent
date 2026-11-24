"""
main.py - the web API the AI Report Portal calls.

Run (demo, no keys needed):
    pip install -r requirements.txt
    uvicorn main:app --reload

Environment
    LLM_PROVIDER   mock (default) | anthropic   (+ ANTHROPIC_API_KEY, LLM_MODEL)
    DB_BACKEND     sqlite (default) | sqlserver (+ SQLSERVER_CONN, see sqlserver_setup.sql)
    AUTH_MODE      demo (default, X-User-Id header) | jwt (+ OIDC_JWKS_URL, OIDC_ISSUER, OIDC_AUDIENCE)
"""
import os
import time
from collections import defaultdict, deque

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from pathlib import Path

import workflow as wf
from catalog import CATALOG, DIM_VALUES, PERIODS
from database import AppStore, make_database
from llm import make_llm

AUTH_MODE = os.environ.get("AUTH_MODE", "demo")
if AUTH_MODE == "demo" and os.environ.get("DB_BACKEND") == "sqlserver" and os.environ.get("ALLOW_DEMO_AUTH") != "1":
    raise RuntimeError("Demo sign-in must not be used with the real database. Set AUTH_MODE=jwt.")

store, db, llm = AppStore(), make_database(), make_llm()
app = FastAPI(title="AI Report Portal backend")
app.add_middleware(CORSMiddleware, allow_origins=os.environ.get("PORTAL_ORIGINS", "http://localhost:8000").split(","),
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type", "Authorization", "X-User-Id"])


# ---- STEP 1: who is calling ----------------------------------------------
def signed_in_user_id(authorization: str | None, x_user_id: str | None) -> str:
    if AUTH_MODE == "jwt":                             # NOT TESTED in the build environment
        import jwt
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "Sign in first")
        token = authorization[7:]
        try:
            key = jwt.PyJWKClient(os.environ["OIDC_JWKS_URL"]).get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=os.environ["OIDC_AUDIENCE"],
                                issuer=os.environ["OIDC_ISSUER"])
        except Exception:
            raise HTTPException(401, "Sign-in could not be verified")
        return str(claims[os.environ.get("OIDC_USER_CLAIM", "preferred_username")]).lower()
    if not x_user_id:                                  # demo only: anyone can type any id
        raise HTTPException(401, "Sign in first")
    return x_user_id.strip().lower()


def current_user(authorization: str | None = Header(None), x_user_id: str | None = Header(None)) -> wf.Authority:
    auth = wf.get_authority(store, signed_in_user_id(authorization, x_user_id))        # STEP 2
    if not auth:
        raise HTTPException(403, "You have no access to the report portal")
    return auth


_calls: dict[str, deque] = defaultdict(deque)


def rate_limit(auth: wf.Authority, per_minute: int = 20) -> None:
    q, now = _calls[auth.user_id], time.time()
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= per_minute:
        raise HTTPException(429, "Too many requests. Wait a minute and try again.")
    q.append(now)


@app.exception_handler(wf.WorkflowStop)
def workflow_stopped(_, e: wf.WorkflowStop):
    return JSONResponse(status_code=422, content={"message": str(e), "detail": e.detail})


class AskBody(BaseModel):
    question: str = Field(min_length=3, max_length=600)


class ReportBody(wf.ReportRequest):
    report_type: str


class DecisionBody(BaseModel):
    approve: bool
    comment: str = Field(default="", max_length=300)


def own_run(run_id: str, auth: wf.Authority, allow_owner: bool = False) -> dict:
    run = store.get_run(run_id)
    if not run or not (run["user_id"] == auth.user_id or (allow_owner and auth.is_owner)):
        raise HTTPException(404, "Run not found")
    if run["report_type"] not in auth.report_types:
        raise HTTPException(403, "You are no longer authorised for this report type")
    return run


# ---- endpoints -----------------------------------------------------------
@app.get("/api/health")
def health():
    return {"status": "ok", "llm": llm.name, "database": db.name, "auth": AUTH_MODE}


@app.get("/api/me")
def me(auth: wf.Authority = Depends(current_user)):
    return auth


@app.get("/api/catalog")
def catalog(auth: wf.Authority = Depends(current_user)):
    return {rt: {"label": CATALOG[rt]["label"], "kpis": {k: v["text"] for k, v in CATALOG[rt]["kpis"].items()},
                 "dimensions": {d: DIM_VALUES[d] for d in CATALOG[rt]["dims"]}, "periods": PERIODS}
            for rt in auth.report_types}


@app.post("/api/report")
def dashboard_report(body: ReportBody, auth: wf.Authority = Depends(current_user)):
    """STEP 3, dashboard route: a fixed template, no LLM reads the request."""
    rate_limit(auth)
    req = wf.ReportRequest(**body.model_dump(exclude={"report_type"}))
    return wf.execute_report(body.report_type, req, "certified", auth, llm, db, store, source="dashboard")


@app.post("/api/ask")
def ask(body: AskBody, auth: wf.Authority = Depends(current_user)):
    """STEPS 4-5: returns how the request was read. Nothing runs until the user confirms."""
    rate_limit(auth)
    return wf.start_ask(body.question, auth, llm, store)


@app.post("/api/ask/{request_id}/confirm")
def confirm(request_id: str, auth: wf.Authority = Depends(current_user)):
    """STEPS 6-14 for a confirmed request."""
    rate_limit(auth)
    return wf.run_confirmed(request_id, auth, llm, db, store)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, auth: wf.Authority = Depends(current_user)):
    return own_run(run_id, auth, allow_owner=True)


@app.get("/api/runs/{run_id}/csv")
def download_csv(run_id: str, auth: wf.Authority = Depends(current_user)):
    """STEP 15: CSV for the person who ran the report."""
    run = own_run(run_id, auth)
    store.audit(auth.user_id, "csv_download", {"run_id": run_id})
    return Response(wf.build_csv(run), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{run_id}.csv"'})


@app.post("/api/runs/{run_id}/approval")
def request_approval(run_id: str, auth: wf.Authority = Depends(current_user)):
    run = own_run(run_id, auth)
    if run["status"] != "completed":
        raise HTTPException(409, f"Run is already {run['status']}")
    store.set_status(run_id, "awaiting_approval")
    store.audit(auth.user_id, "approval_requested", {"run_id": run_id})
    return {"run_id": run_id, "status": "awaiting_approval"}


@app.post("/api/runs/{run_id}/decision")
def decide(run_id: str, body: DecisionBody, auth: wf.Authority = Depends(current_user)):
    """STEP 15: a report owner, who is not the requester, approves before anything is distributed."""
    run = own_run(run_id, auth, allow_owner=True)
    if not auth.is_owner or run["user_id"] == auth.user_id:
        raise HTTPException(403, "A report owner other than the requester must decide")
    if run["status"] != "awaiting_approval":
        raise HTTPException(409, f"Run is {run['status']}")
    status = "approved" if body.approve else "rejected"
    store.set_status(run_id, status, auth.user_id)
    store.audit(auth.user_id, "approval_" + status, {"run_id": run_id, "comment": body.comment})
    return {"run_id": run_id, "status": status}


@app.get("/")
def portal():
    page = Path(__file__).parent / "portal.html"
    if not page.exists():
        raise HTTPException(404, "Place the portal file next to main.py as portal.html")
    return FileResponse(page)
