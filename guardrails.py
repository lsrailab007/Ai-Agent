"""
guardrails.py - hardcoded SQL checks (STEP 8). No LLM is involved here.

prepare_sql : code adds the user's scope and the row limit to a query.
verify_sql  : fixed checks on the final query. An empty list means it may run.
The same checks apply to template SQL and to SQL written by the LLM.
"""
import sqlglot
from sqlglot import exp

from catalog import CATALOG, DIM_VALUES

MAX_ROWS = 500
ALLOWED_PARAMS = {"date_from", "date_to", "scope_value"}
ALLOWED_FUNCS = (exp.Sum, exp.Count, exp.Avg, exp.Min, exp.Max, exp.Nullif, exp.Coalesce, exp.Round, exp.Abs,
                 exp.Cast, exp.And, exp.Or, exp.Not)
FORBIDDEN = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Command, exp.Merge,
             exp.Union, exp.Subquery, exp.With, exp.Join, exp.Into, exp.Execute)


def _parse_one_select(sql: str):
    statements = sqlglot.parse(sql, read="tsql")
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise ValueError("exactly one SELECT statement is required")
    return statements[0]


def _conjuncts(node) -> list:
    """Splits a WHERE into its top-level AND parts."""
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.And):
        return _conjuncts(node.this) + _conjuncts(node.expression)
    return [node]


def scope_of(auth) -> tuple[str | None, str | None]:
    return ("branch", auth.branch) if auth.branch else (("region", auth.region) if auth.region else (None, None))


def prepare_sql(sql: str, auth) -> str:
    """Code, not the LLM, limits the query to the user's region or branch and caps the rows."""
    tree = _parse_one_select(sql)
    col, value = scope_of(auth)
    if value:
        already = any(isinstance(c, exp.EQ) and c.this.name == col and isinstance(c.expression, exp.Parameter)
                      and c.expression.name == "scope_value"
                      for c in _conjuncts(tree.args["where"].this)) if tree.args.get("where") else False
        if not already:
            tree = tree.where(f"{col} = @scope_value", dialect="tsql")
    limit = tree.args.get("limit")
    if not limit or not limit.expression.is_int or int(limit.expression.name) > MAX_ROWS:
        tree = tree.limit(MAX_ROWS)
    return tree.sql(dialect="tsql")


def verify_sql(sql: str, report_type: str, auth) -> list[str]:
    cat, bad = CATALOG[report_type], []
    if ";" in sql or "--" in sql or "/*" in sql:
        bad.append("G1 statement separators and comments are not allowed")
    try:
        tree = _parse_one_select(sql)
    except Exception as e:
        return bad + [f"G2 {str(e).splitlines()[0]}"]
    if any(tree.find(n) for n in FORBIDDEN):
        bad.append("G3 only a plain read-only SELECT on one view is allowed (no writes, joins, subqueries, CTEs or unions)")
    not_allowed = {type(f).__name__ for f in tree.find_all(exp.Func) if not isinstance(f, ALLOWED_FUNCS)}
    if not_allowed:
        bad.append(f"G3 functions not allowed: {sorted(not_allowed)}")
    tables = list(tree.find_all(exp.Table))
    if len(tables) != 1 or tables[0].name != cat["view"] or tables[0].db or tables[0].catalog:
        bad.append(f"G4 the query may read only the view {cat['view']}, without a schema or database prefix")
    if tree.find(exp.Star):
        bad.append("G5 * is not allowed; name the columns (use SUM, not COUNT(*))")
    unknown = {c.name for c in tree.find_all(exp.Column)} - cat["columns"]
    if unknown:
        bad.append(f"G5 columns not in the view: {sorted(unknown)}")
    group = tree.args.get("group")
    group_cols = {g.name for g in group.expressions} if group else set()
    if len(group_cols) > 3 or len(tree.expressions) > 8:
        bad.append("G6 at most 3 grouping columns and 8 output columns")
    for e in tree.expressions:
        if not e.find(exp.AggFunc) and not (isinstance(e, exp.Column) and e.name in group_cols):
            bad.append(f"G6 output '{e.sql()}' must be a total or a GROUP BY column; row-level data may not be returned")
    where = tree.args.get("where")
    parts = _conjuncts(where.this) if where else []
    if not any(isinstance(p, exp.Between) and p.this.name == "business_date"
               and getattr(p.args["low"], "name", "") == "date_from" and getattr(p.args["high"], "name", "") == "date_to"
               and isinstance(p.args["low"], exp.Parameter) for p in parts):
        bad.append("G7 WHERE must contain: business_date BETWEEN @date_from AND @date_to")
    for part in filter(None, [where, tree.args.get("having")]):
        for lit in part.find_all(exp.Literal):
            if not lit.is_string:
                continue
            parent = lit.parent
            col = parent.this.name if isinstance(parent, (exp.EQ, exp.In)) and isinstance(parent.this, exp.Column) else None
            if col not in DIM_VALUES or lit.name not in DIM_VALUES[col]:
                bad.append(f"G8 text value '{lit.name}' is not a known value of a dimension column")
    used = {p.name for p in tree.find_all(exp.Parameter)}
    if used - ALLOWED_PARAMS:
        bad.append(f"G9 unknown parameters: {sorted(used - ALLOWED_PARAMS)}")
    col, value = scope_of(auth)
    if value and not any(isinstance(p, exp.EQ) and isinstance(p.this, exp.Column) and p.this.name == col
                         and isinstance(p.expression, exp.Parameter) and p.expression.name == "scope_value" for p in parts):
        bad.append(f"G10 the query must be limited to the user's {col}")
    limit = tree.args.get("limit")
    if not limit or not limit.expression.is_int or int(limit.expression.name) > MAX_ROWS:
        bad.append(f"G11 TOP {MAX_ROWS} or lower is required")
    return bad
