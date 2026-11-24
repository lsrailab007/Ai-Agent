"""
database.py - where the data lives.

ReportDatabase : runs a verified query. Two versions with the same methods:
                   SqliteDemo  - local file with synthetic data, for trying things out
                   SqlServer   - production (needs pyodbc and sqlserver_setup.sql)
AppStore       : the backend's own small store: user authority, pending requests,
                 finished runs and the audit log.
Queries are written in T-SQL with named parameters (@date_from, @date_to, @scope_value).
"""
import json
import os
import random
import re
import sqlite3
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import sqlglot

from catalog import CATALOG, DIM_VALUES, REGIONS

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent / "data"))
QUERY_TIMEOUT_S = int(os.environ.get("QUERY_TIMEOUT_S", "10"))
MAX_ROWS = 500


def to_positional(sql: str, values: dict) -> tuple[str, list]:
    """@name -> ? in text order, with the matching list of values. Unknown names raise KeyError."""
    params: list = []

    def swap(m):
        params.append(values[m.group(1)])
        return "?"
    return re.sub(r"@(\w+)", swap, sql), params


class SqliteDemo:
    name = "sqlite-demo"

    def __init__(self):
        self.path = DATA_DIR / "demo_reports.db"
        self._seed()

    def run(self, sql: str, values: dict, auth) -> tuple[list[str], list[list]]:
        sql_q, params = to_positional(sqlglot.transpile(sql, read="tsql", write="sqlite")[0], values)
        con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)          # read-only connection
        deadline = time.monotonic() + QUERY_TIMEOUT_S
        con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        try:
            cur = con.execute(sql_q, params)
            return [c[0] for c in cur.description], [list(r) for r in cur.fetchmany(MAX_ROWS)]
        finally:
            con.close()

    def estimate_cost(self, sql: str, values: dict, auth) -> float | None:
        return None      # SQLite has no cost estimate; the timeout and row limit still apply

    def _seed(self):
        if self.path.exists():
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        rnd, today, con = random.Random(42), date.today(), sqlite3.connect(self.path)
        days = [(today - timedelta(days=i)).isoformat() for i in range(150)]
        for rt, cat in CATALOG.items():
            extra = [d for d in cat["dims"] if d not in ("region", "branch")]
            cols = ["business_date", "region", "branch", *extra, *cat["measures"]]
            con.execute(f"CREATE TABLE {cat['view']} ({', '.join(cols)})")
            combos = [[]]
            for d in extra:
                combos = [c + [v] for c in combos for v in DIM_VALUES[d]]
            rows = []
            for day in days:
                for region, branches in REGIONS.items():
                    for branch in branches:
                        for combo in combos:
                            b = rnd.randint(0, 12)
                            m = {"client": [b, rnd.randint(0, 5)],
                                 "agent": [rnd.randint(0, 1), b, b * rnd.randint(900, 3000)],
                                 "proposal": [b, int(b * .7), rnd.randint(0, b - int(b * .7))],
                                 "new_business": [b, b * rnd.randint(900, 4000), b * rnd.randint(800, 3600)],
                                 "policy_servicing": [b, int(b * .85), int(b * .85) * rnd.randint(1, 6)],
                                 "claims": [b, int(b * .7), int(b * .7) * rnd.randint(2000, 20000)],
                                 "surrender": [b // 3, (b // 3) * rnd.randint(1500, 30000)]}[rt]
                            rows.append([day, region, branch, *combo, *m])
            con.executemany(f"INSERT INTO {cat['view']} VALUES ({', '.join('?' * len(cols))})", rows)
        con.commit()
        con.close()


class SqlServer:
    """Production database. NOT TESTED in the build environment: review with your DBA before use."""
    name = "sqlserver"

    def __init__(self):
        import pyodbc
        self.pyodbc = pyodbc
        # Example: Driver={ODBC Driver 18 for SQL Server};Server=...;Database=...;ApplicationIntent=ReadOnly;
        #          Authentication=ActiveDirectoryMsi;Encrypt=yes;   (a login that holds only the report_reader role)
        self.conn_str = os.environ["SQLSERVER_CONN"]

    def _connect(self, auth):
        con = self.pyodbc.connect(self.conn_str, autocommit=True, readonly=True)
        con.timeout = QUERY_TIMEOUT_S
        cur = con.cursor()
        # Row-level security in the database reads these values (see sqlserver_setup.sql).
        cur.execute("EXEC sp_set_session_context @key=N'scope_region', @value=?, @read_only=1", auth.region or "ALL")
        cur.execute("EXEC sp_set_session_context @key=N'scope_branch', @value=?, @read_only=1", auth.branch or "ALL")
        return con, cur

    def run(self, sql: str, values: dict, auth):
        sql_q, params = to_positional(sql, values)
        con, cur = self._connect(auth)
        try:
            cur.execute(sql_q, params)
            return [c[0] for c in cur.description], [list(r) for r in cur.fetchmany(MAX_ROWS)]
        finally:
            con.close()

    def estimate_cost(self, sql: str, values: dict, auth) -> float | None:
        """Asks SQL Server for the estimated plan without running the query. Needs SHOWPLAN permission."""
        for v in values.values():                      # values here come from code, never from the user or LLM
            if not re.fullmatch(r"[A-Za-z0-9 \-]{1,40}", str(v)):
                raise ValueError("unexpected parameter value")
        literal_sql = re.sub(r"@(\w+)", lambda m: "'" + str(values[m.group(1)]) + "'", sql)
        con, cur = self._connect(auth)
        try:
            cur.execute("SET SHOWPLAN_XML ON")
            cur.execute(literal_sql)
            plan = cur.fetchone()[0]
            cur.execute("SET SHOWPLAN_XML OFF")
            costs = [float(x) for x in re.findall(r'StatementSubTreeCost="([0-9.eE+\-]+)"', plan)]
            return max(costs) if costs else None
        finally:
            con.close()


def make_database():
    return SqlServer() if os.environ.get("DB_BACKEND", "sqlite") == "sqlserver" else SqliteDemo()


class AppStore:
    """Authority, pending requests, runs and audit. A small SQLite file; swap for your app database later."""

    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.path = DATA_DIR / "app.db"
        con = self._con()
        con.executescript("""
            CREATE TABLE IF NOT EXISTS user_authority (user_id TEXT PRIMARY KEY, name, role, is_owner, report_types, region, branch);
            CREATE TABLE IF NOT EXISTS pending (request_id TEXT PRIMARY KEY, user_id, created, body);
            CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, user_id, created, status, approver, body);
        """)
        if not con.execute("SELECT 1 FROM user_authority").fetchone():
            everything = ",".join(CATALOG)
            con.executemany("INSERT INTO user_authority VALUES (?,?,?,?,?,?,?)", [
                ("priya", "Priya Nair", "Report owner", 1, everything, None, None),
                ("daniel", "Daniel Okafor", "Report owner (deputy)", 1, everything, None, None),
                ("marco", "Marco Rossi", "Regional head", 0, "client,agent,proposal,new_business", "North", None),
                ("lena", "Lena Fischer", "Branch manager", 0, "proposal,new_business", "North", "N-02")])
        con.commit()
        con.close()

    def _con(self):
        return sqlite3.connect(self.path)

    def authority_row(self, user_id: str):
        con = self._con()
        row = con.execute("SELECT user_id, name, role, is_owner, report_types, region, branch FROM user_authority "
                          "WHERE user_id = ?", (user_id,)).fetchone()
        con.close()
        return row

    def save_pending(self, user_id: str, body: dict) -> str:
        rid = "Q-" + uuid.uuid4().hex[:10]
        con = self._con()
        con.execute("INSERT INTO pending VALUES (?,?,?,?)", (rid, user_id, time.time(), json.dumps(body)))
        con.commit(); con.close()
        return rid

    def take_pending(self, request_id: str, user_id: str, max_age_s: int = 1800) -> dict | None:
        con = self._con()
        row = con.execute("SELECT created, body FROM pending WHERE request_id = ? AND user_id = ?",
                          (request_id, user_id)).fetchone()
        con.execute("DELETE FROM pending WHERE request_id = ? AND user_id = ?", (request_id, user_id))
        con.commit(); con.close()
        return json.loads(row[1]) if row and time.time() - row[0] <= max_age_s else None

    def save_run(self, user_id: str, body: dict) -> str:
        run_id = "R-" + uuid.uuid4().hex[:10]
        body["run_id"] = run_id
        con = self._con()
        con.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)", (run_id, user_id, time.time(), "completed", None, json.dumps(body)))
        con.commit(); con.close()
        return run_id

    def get_run(self, run_id: str) -> dict | None:
        con = self._con()
        row = con.execute("SELECT user_id, status, approver, body FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        con.close()
        return {"user_id": row[0], "status": row[1], "approver": row[2], **json.loads(row[3])} if row else None

    def set_status(self, run_id: str, status: str, approver: str | None = None):
        con = self._con()
        con.execute("UPDATE runs SET status = ?, approver = ? WHERE run_id = ?", (status, approver, run_id))
        con.commit(); con.close()

    def audit(self, user_id: str, event: str, detail: dict) -> None:
        """One line per event. Figures are never written to the audit log."""
        line = {"at": datetime.now(timezone.utc).isoformat(), "user": user_id, "event": event, **detail}
        with open(DATA_DIR / "audit.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
