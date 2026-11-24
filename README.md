# AI Report Portal backend (version 2)

A Python backend for the AI Report Portal. The program controls a fixed 15-step workflow
and asks an LLM for help at four points. It runs out of the box on a local demo database
with synthetic data; SQL Server, company sign-in and the real LLM are switched on by settings.

## Files

| File | What it holds |
|---|---|
| `main.py` | The web API the portal calls; sign-in; rate limit; CSV and approval endpoints |
| `workflow.py` | The 15 steps, in order |
| `catalog.py` | Report types, views, columns and certified KPIs. Add a KPI here |
| `guardrails.py` | The hardcoded SQL checks |
| `llm.py` | The four LLM calls and their prompts; a keyword mock for offline testing |
| `database.py` | Demo database, SQL Server connection, and the backend's own store and audit log |
| `sqlserver_setup.sql` | Template for views, read-only role and row-level security |

## Run the demo

    pip install fastapi uvicorn sqlglot pydantic
    uvicorn main:app --reload

    curl -H "X-User-Id: priya" localhost:8000/api/me
    curl -H "X-User-Id: marco" -H "Content-Type: application/json" \
         -d '{"question":"first year premium by channel last month"}' localhost:8000/api/ask
    # take request_id from the answer, then:
    curl -X POST -H "X-User-Id: marco" localhost:8000/api/ask/<request_id>/confirm
    curl -H "X-User-Id: marco" localhost:8000/api/runs/<run_id>/csv

Demo users: priya and daniel (report owners, everything), marco (North region), lena (branch N-02).

## Switch on the real parts

| Setting | Value | Also needed |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` | `ANTHROPIC_API_KEY`, `LLM_MODEL` (the model your organisation approved) |
| `DB_BACKEND` | `sqlserver` | `SQLSERVER_CONN`, `pip install pyodbc`, and `sqlserver_setup.sql` applied |
| `AUTH_MODE` | `jwt` | `OIDC_JWKS_URL`, `OIDC_ISSUER`, `OIDC_AUDIENCE`, `pip install "PyJWT[crypto]"` |

The backend refuses to start with demo sign-in and SQL Server together.

## Tested and not tested

Tested on the demo database with the mock LLM: both routes, the confirm step, guardrails,
the retry loop, scope limits, small-group hiding, control totals, CSV and approval.

Not tested: the Anthropic connection, the SQL Server connection and cost check, the
row-level security script and JWT sign-in. Treat these as written to specification and
test them in your own environment before use.
