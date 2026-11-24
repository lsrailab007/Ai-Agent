-- sqlserver_setup.sql  -  TEMPLATE, NOT TESTED. Review and adapt with your DBA.
-- Purpose: the database itself keeps the backend read-only and limited to the user's
-- region or branch, even if every check in the Python program were to fail.

-- 1. A schema that holds only the report views. Each view exposes the columns in catalog.py
--    and nothing else (no names, identifiers, medical or free-text columns).
CREATE SCHEMA rpt;
GO
CREATE VIEW rpt.v_nb_daily AS
    SELECT business_date, region, branch, channel, product, policies, premium, ape
    FROM dbo.nb_daily;            -- replace dbo.nb_daily with your source table
GO
-- Repeat for: v_client_daily, v_agent_daily, v_proposal_daily, v_servicing_daily,
--             v_claims_daily, v_surrender_daily

-- 2. A login for the backend that can only read those views.
CREATE ROLE report_reader;
GRANT SELECT ON SCHEMA::rpt TO report_reader;
GRANT SHOWPLAN TO report_reader;                  -- needed for the cost check (step 9)
-- CREATE USER ai_report_svc FROM EXTERNAL PROVIDER WITH DEFAULT_SCHEMA = rpt;
-- ALTER ROLE report_reader ADD MEMBER ai_report_svc;
-- Do not grant db_datareader, db_datawriter or any right on dbo to this user.

-- 3. Row-level security. The backend sets scope_region and scope_branch on each connection
--    (see SqlServer._connect in database.py). 'ALL' means no limit; a missing value shows nothing.
CREATE SCHEMA sec;
GO
CREATE FUNCTION sec.fn_report_scope (@region AS nvarchar(50), @branch AS nvarchar(50))
RETURNS TABLE WITH SCHEMABINDING AS
RETURN SELECT 1 AS allowed
       WHERE CAST(SESSION_CONTEXT(N'scope_region') AS nvarchar(50)) IN (N'ALL', @region)
         AND CAST(SESSION_CONTEXT(N'scope_branch') AS nvarchar(50)) IN (N'ALL', @branch);
GO
CREATE SECURITY POLICY sec.report_scope_policy
    ADD FILTER PREDICATE sec.fn_report_scope(region, branch) ON dbo.nb_daily
    -- add one line per source table behind the report views
    WITH (STATE = ON);
GO

-- 4. Recommended: point the backend at a read-only replica (ApplicationIntent=ReadOnly)
--    and cap its CPU and memory with Resource Governor.
