"""The hostile battery: statements the SQL tab must refuse, per engine.

Used twice: by test_sqlguard.py (the guard alone, no database) and by test_live_sql.py (through
the runner, as an account that CAN write, on the four Docker databases).

{x} is the third schema of the test databases (dx_extra / DX_EXTRA) and {t} the demo schema.
Every statement is something its engine would really run - most of them would change data, take
a lock or run code if the guard let them through.

"call without brackets" (the fix round after session 3's audit): Oracle calls a function that
takes no argument without brackets, and PostgreSQL calls f(t) for t.f. ORACLE_CALLS and
POSTGRES_CALLS hold those statements with a word on each; scripts/call_proof.py runs them on the
raw driver and reports which ones the server really executes.
"""

# what each group is about, in the order of the session brief
GROUPS = ['write', 'ddl', 'select into', 'lock', 'sequence', 'dblink', 'function', 'call without brackets',
          'quote and comment trick', 'several statements', 'other']
NO_BRACKET_CALLS = ('oracle', 'postgresql')         # the engines that have the group "call without brackets"

# The test databases hold x_ping, status, one and x_pkg.ping: functions without arguments that write a row
# into x_calls through an autonomous transaction, each with a public synonym (scripts/live_engines.py).
# STATUS is also a column of the demo schema - but of no table these statements read.
ORACLE_CALLS = [
    # the eight shapes of the audit: all of them passed the guard of session 3
    ('1 a table alias used as a bare name', "SELECT x_ping FROM {x}.x_parents x_ping"),
    ('2 an alias from another branch of a UNION', "SELECT 1 x_ping FROM dual UNION ALL SELECT x_ping FROM dual"),
    ('3 a column alias in its own select list', "SELECT 1 AS x_ping, x_ping FROM dual"),
    ('4 an alias declared in a subquery, used outside it',
     "SELECT code FROM {x}.x_parents WHERE parent_id = x_ping AND 1 = (SELECT COUNT(*) x_ping FROM dual)"),
    ('5 a CTE name used as a bare name', "WITH x_ping AS (SELECT 1 a FROM dual) SELECT x_ping FROM dual"),
    ('6 a table alias named like a schema', "SELECT {x}.x_ping FROM {x}.x_parents {x}"),
    ('7 a table alias named like a package', "SELECT x_pkg.ping FROM {x}.x_parents x_pkg"),
    ('7b the same with a package of the database', "SELECT dbms_random.value FROM {x}.x_parents dbms_random"),
    ('8 a known column name where no table has that column', "SELECT status FROM dual"),
    # variants
    ('9 a quoted name', 'SELECT "X_PING" FROM dual'),
    ('10 a quoted schema and name', 'SELECT "{x}"."X_PING" FROM dual'),
    ('11 a quoted package and function', 'SELECT "X_PKG"."PING" FROM dual'),
    ('12 a quoted column alias, then the bare name', 'SELECT 1 "X_PING", x_ping FROM dual'),
    ('13 the alias of a subquery in FROM as a bare name', "SELECT x_ping FROM (SELECT 1 a FROM dual) x_ping"),
    ('14 an alias inside a scalar subquery, used beside it', "SELECT (SELECT 1 x_ping FROM dual) a, x_ping FROM dual"),
    ('15 an alias of the outer query, used in a subquery', "SELECT 1 x_ping, (SELECT x_ping FROM dual) b FROM dual"),
    ('16 a column alias in the WHERE of its own block', "SELECT 1 x_ping FROM dual WHERE x_ping = 1"),
    ('17 a column alias in HAVING', "SELECT COUNT(*) x_ping FROM dual HAVING x_ping > 0"),
    ('18 a column alias in GROUP BY', "SELECT 1 x_ping FROM dual GROUP BY x_ping"),
    ('19 a column alias in the ORDER BY of a window', "SELECT 1 x_ping, ROW_NUMBER() OVER (ORDER BY x_ping) rn FROM dual"),
    ('20 a column alias in a subquery of its ORDER BY', "SELECT 1 x_ping FROM dual ORDER BY (SELECT x_ping FROM dual)"),
    ('21 a column of a CTE that is not in FROM', "WITH c (x_ping) AS (SELECT 1 FROM dual) SELECT x_ping FROM dual"),
    ('22 a CTE in FROM, and its name as a bare name', "WITH x_ping AS (SELECT 1 a FROM dual) SELECT x_ping FROM x_ping"),
    ('23 a function inside the expression that carries its name', "SELECT NVL(x_ping, 0) x_ping FROM dual"),
    ('24 a known column name in WHERE, no table has it', "SELECT p.code FROM {x}.x_parents p WHERE status = 1"),
    ('25 a known column name in ORDER BY, no table has it', "SELECT code FROM {x}.x_parents ORDER BY status"),
    ('26 a known column name in a subquery in FROM, beside its table',
     "SELECT s.subscription_id, v.n FROM {t}.subscriptions s, (SELECT status n FROM dual) v"),
    ('27 a known column name two subqueries below its table',
     "SELECT (SELECT v.n FROM (SELECT status n FROM dual) v) FROM {t}.subscriptions"),
    ('28 an alias of the last branch in the ORDER BY of a UNION',
     "SELECT 2 a FROM dual UNION ALL SELECT 1 x_ping FROM dual ORDER BY x_ping"),
    ('30 a package without a schema', "SELECT x_pkg.ping FROM dual"),
    # ONE is a word of the language (ONE ROW PER MATCH) but not a reserved one: a function can have that name
    ('31 a function named like a word of the language', "SELECT one FROM dual"),
    ('32 the same word, in a place where a value stands', "SELECT code FROM {x}.x_parents WHERE parent_id = one"),
]

# Asked of the server too, but not hostile: the guard lets it through, and Oracle answers ORA-00904. A real
# alias in front of a dot is a column of that table or an error - never a call, whatever the name behind it.
ORACLE_NOT_CALLS = [
    ('29 a real alias in front of a function name', "SELECT p.x_ping FROM {x}.x_parents p"),
]

# public.x_peek(dx_extra.x_parents) writes a row into x_calls. PostgreSQL calls it for p.x_peek.
POSTGRES_CALLS = [
    ('1 alias.function', "SELECT p.x_peek FROM {x}.x_parents p"),
    ('2 (alias).function', "SELECT (p).x_peek FROM {x}.x_parents p"),
    ('3 table.function', "SELECT x_parents.x_peek FROM {x}.x_parents"),
    ('4 schema.table.function', "SELECT {x}.x_parents.x_peek FROM {x}.x_parents"),
    ('5 (table).function', "SELECT (x_parents).x_peek FROM {x}.x_parents"),
    ('6 a quoted function name', 'SELECT p."x_peek" FROM {x}.x_parents p'),
    ('7 in WHERE', "SELECT p.parent_id FROM {x}.x_parents p WHERE p.x_peek = 1"),
    ('8 in ORDER BY', "SELECT p.parent_id FROM {x}.x_parents p ORDER BY p.x_peek"),
    ('9 the name is a column of another table of the statement',
     "SELECT p.x_peek FROM {x}.x_parents p, (SELECT 1 AS x_peek) q"),
    ('10 in a subquery, on the outer alias', "SELECT (SELECT p.x_peek) FROM {x}.x_parents p"),
    ('11 on a subquery in FROM', "SELECT p.x_peek FROM (SELECT * FROM {x}.x_parents) p"),
    ('12 on a CTE', "WITH p AS (SELECT * FROM {x}.x_parents) SELECT p.x_peek FROM p"),
    ('13 (alias.*).function', "SELECT (p.*).x_peek FROM {x}.x_parents p"),
    # since 13 Oct 2026 the reader goes by PostgreSQL's own reserved words: these aliases are names now
    ('14 an alias called like a word that only Oracle reserves', "SELECT session.x_peek FROM {x}.x_parents session"),
    ('15 the same behind AS, and in brackets', "SELECT (level).x_peek FROM {x}.x_parents AS level"),
    ('16 on a subquery under such a name', "SELECT size.x_peek FROM (SELECT * FROM {x}.x_parents) size"),
]

COMMON = [
    ('write', "INSERT INTO {x}.x_audit (id, note) VALUES (9, 'hostile')"),
    ('write', "UPDATE {x}.x_parents SET label = 'hostile'"),
    ('write', "DELETE FROM {x}.x_parents"),
    ('write', "MERGE INTO {x}.x_audit a USING {x}.x_parents p ON (a.id = p.parent_id) WHEN NOT MATCHED THEN INSERT (id) VALUES (p.parent_id)"),
    ('ddl', "CREATE TABLE {x}.x_hostile (a INT)"),
    ('ddl', "DROP TABLE {x}.x_parents"),
    ('ddl', "ALTER TABLE {x}.x_parents ADD hostile INT"),
    ('ddl', "TRUNCATE TABLE {x}.x_audit"),
    ('ddl', "CREATE INDEX ix_hostile ON {x}.x_parents (label)"),
    ('ddl', "GRANT ALL ON {x}.x_parents TO PUBLIC"),
    ('several statements', "SELECT 1 FROM {x}.x_parents; DELETE FROM {x}.x_parents"),
    ('several statements', "SELECT 1 FROM {x}.x_parents; SELECT 2 FROM {x}.x_parents"),
    ('several statements', "SELECT COUNT(*) FROM {x}.x_parents; DROP TABLE {x}.x_audit;"),
    ('other', "COMMIT"),
    ('other', "ROLLBACK"),
    ('other', "(DELETE FROM {x}.x_parents)"),
    ('other', ""),
    ('other', ";"),
]

ENGINE = {
    'oracle': [
        ('write', "INSERT INTO {x}.x_audit SELECT parent_id, label FROM {x}.x_parents"),
        ('ddl', "CREATE TABLE {x}.x_hostile AS SELECT * FROM {x}.x_parents"),
        ('select into', "SELECT label INTO v_label FROM {x}.x_parents WHERE parent_id = 1"),
        ('lock', "SELECT * FROM {x}.x_parents FOR UPDATE"),
        ('lock', "SELECT * FROM {x}.x_parents WHERE parent_id = 1 FOR UPDATE NOWAIT"),
        ('lock', "SELECT * FROM {x}.x_parents FOR UPDATE SKIP LOCKED"),
        ('lock', "SELECT p.label FROM {x}.x_parents p FOR UPDATE OF p.label"),
        ('lock', "LOCK TABLE {x}.x_parents IN EXCLUSIVE MODE"),
        ('sequence', "SELECT {x}.x_seq.NEXTVAL FROM dual"),
        ('sequence', "SELECT x_seq.nextval FROM dual"),
        ('sequence', "SELECT {x}.x_seq.CURRVAL FROM dual"),
        ('dblink', "SELECT * FROM {x}.x_parents@loopback"),
        ('dblink', "SELECT {x}.x_touch@loopback FROM dual"),
        # a user-defined function that writes through an autonomous transaction - in four spellings
        ('function', "SELECT x_touch FROM dual"),
        ('function', "SELECT {x}.x_touch FROM dual"),
        ('function', "SELECT x_touch() FROM dual"),
        ('function', "SELECT {x}.x_touch() FROM dual"),
        ('function', "SELECT parent_id FROM {x}.x_parents WHERE x_touch = 1"),
        ('function', "SELECT parent_id FROM {x}.x_parents ORDER BY {x}.x_touch"),
        ('function', "SELECT EXTRACT(YEAR FROM x_touch) FROM dual"),
        ('function', "SELECT NVL(x_touch, 0) FROM dual"),
        ('function', "SELECT dbms_lock.sleep(5) FROM dual"),
        ('function', "SELECT sys.dbms_random.value FROM dual"),
        ('function', "SELECT utl_http.request('http://127.0.0.1/') FROM dual"),
        ('function', "SELECT dbms_xmlgen.getxml('select 1 from dual') FROM dual"),
        ('function', "SELECT dbms_java.runjava('x') FROM dual"),
        ('function', "SELECT \"X_TOUCH\"() FROM dual"),
        # the end of a q'[...]' string is not a plain quote: a checker that thinks so loses track of the text
        ('quote and comment trick', "SELECT q'[x]' FROM dual; DELETE FROM {x}.x_parents"),
        ('quote and comment trick', "SELECT q'[ ' ]' || x_touch || q'[ ' ]' FROM dual"),
        ('quote and comment trick', "SELECT q'{it's}', {x}.x_touch FROM dual"),
        ('quote and comment trick', "SELECT nq'!a'b!' FROM dual WHERE x_touch = 1"),
        ('quote and comment trick', "SELECT Q'<'>' FROM dual FOR UPDATE"),
        ('quote and comment trick', "SELECT 1 FROM dual /* a comment */ ; DELETE FROM {x}.x_parents"),
        ('quote and comment trick', "SELECT /*+ APPEND */ 1 FROM dual"),
        ('quote and comment trick', "SELECT 1 FROM dual -- a comment\n; DELETE FROM {x}.x_parents"),
        ('quote and comment trick', "SELECT 1 /* /* */ FROM dual */ FOR UPDATE"),
        ('several statements', "BEGIN DELETE FROM {x}.x_parents; COMMIT; END;"),
        ('other', "CALL dbms_stats.gather_table_stats('{x}', 'X_PARENTS')"),
        ('other', "EXECUTE IMMEDIATE 'DROP TABLE {x}.x_parents'"),
        ('other', "EXPLAIN PLAN FOR SELECT * FROM {x}.x_parents"),
        ('other', "ALTER SESSION SET CURRENT_SCHEMA = {x}"),
        ('other', "SET TRANSACTION READ WRITE"),
    ],
    'mssql': [
        ('write', "WITH c AS (SELECT * FROM {x}.x_parents) DELETE FROM c"),
        ('write', "UPDATE p SET label = 'hostile' FROM {x}.x_parents p"),
        ('select into', "SELECT * INTO {x}.x_hostile FROM {x}.x_parents"),
        ('select into', "SELECT parent_id, label INTO #hostile FROM {x}.x_parents"),
        ('lock', "SELECT * FROM {x}.x_parents WITH (UPDLOCK)"),
        ('lock', "SELECT * FROM {x}.x_parents WITH (XLOCK)"),
        ('lock', "SELECT * FROM {x}.x_parents WITH (HOLDLOCK)"),
        ('lock', "SELECT * FROM {x}.x_parents WITH (TABLOCK)"),
        ('lock', "SELECT * FROM {x}.x_parents WITH (TABLOCKX)"),
        ('lock', "SELECT * FROM {x}.x_parents WITH (ROWLOCK, XLOCK)"),
        ('lock', "SELECT * FROM {x}.x_parents WITH (PAGLOCK)"),
        ('lock', "SELECT * FROM {x}.x_parents WITH (SERIALIZABLE)"),
        ('lock', "SELECT * FROM {x}.x_parents WITH (REPEATABLEREAD)"),
        ('lock', "SELECT * FROM {x}.x_parents (TABLOCKX)"),
        ('lock', "SELECT * FROM {x}.x_parents p HOLDLOCK"),
        ('lock', "SELECT * FROM {x}.x_parents AS p WITH (NOLOCK, updlock)"),
        ('lock', "SELECT * FROM {x}.x_parents FOR BROWSE"),
        ('sequence', "SELECT NEXT VALUE FOR {x}.x_seq"),
        ('sequence', "SELECT parent_id, NEXT VALUE FOR {x}.x_seq OVER (ORDER BY parent_id) FROM {x}.x_parents"),
        ('dblink', "SELECT * FROM OPENROWSET('MSOLEDBSQL', 'Server=.;Trusted_Connection=yes;', 'DELETE FROM telecom.{x}.x_parents; SELECT 1')"),
        ('dblink', "SELECT * FROM OPENQUERY(loopback, 'DELETE FROM telecom.{x}.x_parents; SELECT 1')"),
        ('dblink', "SELECT * FROM OPENDATASOURCE('MSOLEDBSQL', 'Server=.').telecom.{x}.x_parents"),
        ('function', "SELECT dbo.x_touch()"),
        ('function', "SELECT {x}.x_touch(parent_id) FROM {x}.x_parents"),
        ('function', "SELECT * FROM sys.fn_my_permissions(NULL, 'SERVER')"),
        ('function', "SELECT * FROM sys.dm_exec_sql_text(0x01)"),
        ('function', "SELECT * FROM OPENROWSET(BULK 'C:\\Windows\\win.ini', SINGLE_CLOB) AS x"),
        ('quote and comment trick', "SELECT [a]]b] FROM {x}.x_parents; DROP TABLE {x}.x_audit"),
        ('quote and comment trick', "SELECT 1 /* a comment */ DELETE FROM {x}.x_parents"),
        ('quote and comment trick', "SELECT 1 -- a comment\nDELETE FROM {x}.x_parents"),
        ('quote and comment trick', "SELECT 'a''b' AS [x] DROP TABLE {x}.x_audit"),
        ('quote and comment trick', "SELECT 1 /* /* */ DROP TABLE {x}.x_audit -- */"),
        # SQL Server needs no semicolon between two statements
        ('several statements', "SELECT 1 DELETE FROM {x}.x_parents"),
        ('several statements', "SELECT 1 INSERT INTO {x}.x_audit (id) VALUES (1)"),
        ('several statements', "SELECT 1 DROP TABLE {x}.x_audit"),
        ('several statements', "SELECT 1 EXEC sp_who"),
        ('several statements', "SELECT 1 EXEC('DELETE FROM {x}.x_parents')"),
        ('several statements', "SELECT 1 EXECUTE sp_executesql N'DELETE FROM {x}.x_parents'"),
        ('several statements', "SELECT 1 WAITFOR DELAY '00:00:05'"),
        ('several statements', "SELECT 1 SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"),
        ('several statements', "SELECT 1 BEGIN TRAN DELETE FROM {x}.x_parents"),
        ('several statements', "SELECT 1 IF 1 = 1 DROP TABLE {x}.x_audit"),
        ('several statements', "SELECT 1 DECLARE @x INT"),
        ('several statements', "SELECT 1 USE master"),
        ('several statements', "SELECT 1 KILL 55"),
        ('several statements', "SELECT 1 SHUTDOWN"),
        ('several statements', "SELECT 1 DBCC FREEPROCCACHE"),
        ('several statements', "SELECT 1 BACKUP DATABASE telecom TO DISK = 'x.bak'"),
        ('other', "EXEC xp_cmdshell 'dir'"),
        ('other', "SET NOCOUNT ON"),
        ('other', "USE master"),
        ('other', "BULK INSERT {x}.x_audit FROM 'x.csv'"),
        ('other', "DENY SELECT ON {x}.x_parents TO PUBLIC"),
    ],
    'postgresql': [
        ('write', "WITH d AS (DELETE FROM {x}.x_parents RETURNING *) SELECT * FROM d"),
        ('write', "WITH u AS (UPDATE {x}.x_parents SET label = 'hostile' RETURNING 1) SELECT COUNT(*) FROM u"),
        ('write', "WITH i AS (INSERT INTO {x}.x_audit (id) VALUES (9) RETURNING id) SELECT * FROM i"),
        ('ddl', "CREATE TABLE {x}.x_hostile AS SELECT * FROM {x}.x_parents"),
        ('select into', "SELECT * INTO {x}.x_hostile FROM {x}.x_parents"),
        ('select into', "SELECT * INTO TEMP x_hostile FROM {x}.x_parents"),
        ('lock', "SELECT * FROM {x}.x_parents FOR UPDATE"),
        ('lock', "SELECT * FROM {x}.x_parents FOR SHARE"),
        ('lock', "SELECT * FROM {x}.x_parents FOR NO KEY UPDATE"),
        ('lock', "SELECT * FROM {x}.x_parents FOR KEY SHARE"),
        ('lock', "SELECT * FROM {x}.x_parents FOR UPDATE SKIP LOCKED"),
        ('lock', "SELECT * FROM {x}.x_parents p FOR UPDATE OF p NOWAIT"),
        ('lock', "LOCK TABLE {x}.x_parents IN ACCESS EXCLUSIVE MODE"),
        ('lock', "SELECT pg_advisory_lock(42)"),
        ('lock', "SELECT pg_advisory_xact_lock_shared(42)"),
        ('lock', "SELECT pg_try_advisory_lock(42)"),
        ('sequence', "SELECT nextval('{x}.x_seq')"),
        ('sequence', "SELECT setval('{x}.x_seq', 500)"),
        ('sequence', "SELECT parent_id, nextval('{x}.x_seq') FROM {x}.x_parents"),
        ('sequence', "SELECT currval('{x}.x_seq')"),
        ('dblink', "SELECT dblink_exec('dbname=telecom', 'DELETE FROM {x}.x_parents')"),
        ('dblink', "SELECT * FROM dblink('dbname=telecom', 'DELETE FROM {x}.x_parents RETURNING 1') AS t(a int)"),
        ('dblink', "SELECT dblink_connect('c', 'dbname=telecom')"),
        ('dblink', "SELECT dblink_send_query('c', 'DROP TABLE {x}.x_audit')"),
        ('function', "SELECT {x}.x_touch()"),
        ('function', "SELECT x_touch() FROM {x}.x_parents"),
        ('function', "SELECT * FROM {x}.x_touch() AS t(a)"),
        ('function', "SELECT * FROM {x}.x_touch() AS (a int)"),
        ('function', "SELECT pg_sleep(5)"),
        ('function', "SELECT pg_sleep_for('5 seconds')"),
        ('function', "SELECT set_config('statement_timeout', '0', false)"),
        ('function', "SELECT set_config('transaction_read_only', 'off', true)"),
        ('function', "SELECT lo_import('/etc/passwd')"),
        ('function', "SELECT lo_creat(-1)"),
        ('function', "SELECT pg_read_file('postgresql.conf')"),
        ('function', "SELECT pg_ls_dir('.')"),
        ('function', "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"),
        ('function', "SELECT pg_cancel_backend(1)"),
        ('function', "SELECT pg_reload_conf()"),
        ('function', "SELECT txid_current()"),
        ('function', "SELECT pg_notify('channel', 'x')"),
        ('function', "SELECT query_to_xml('delete from {x}.x_parents', true, true, '')"),
        ('function', "SELECT pg_catalog.pg_sleep(5)"),
        ('function', "SELECT \"pg_sleep\"(5)"),
        # "public" in front of a table is a schema now - in front of a function it is still a function with a schema
        ('function', "SELECT public.x_peek(p) FROM {x}.x_parents p"),
        ('function', "SELECT x_peek(public) FROM {x}.x_parents public"),
        ('function', "SELECT * FROM public.x_peek(NULL) AS session"),
        # on PostgreSQL [ ] is an array subscript: what stands inside is code, not a quoted name
        ('quote and comment trick', "SELECT (ARRAY[1, 2])[{x}.x_touch()]"),
        ('quote and comment trick', "SELECT (ARRAY[1, 2])[nextval('{x}.x_seq')::int]"),
        ('quote and comment trick', "SELECT (ARRAY[1])[pg_sleep(5)::text::int]"),
        ('quote and comment trick', "SELECT label FROM {x}.x_parents WHERE parent_id = (ARRAY[1])[1] FOR UPDATE"),
        ('quote and comment trick', "SELECT $$ ' $$; DROP TABLE {x}.x_audit; --'"),
        ('quote and comment trick', "SELECT $x$;$x$; DELETE FROM {x}.x_parents"),
        ('quote and comment trick', "SELECT E'\\''; DELETE FROM {x}.x_parents; --'"),
        ('quote and comment trick', "SELECT U&'\\0027'; DELETE FROM {x}.x_parents; --'"),
        ('quote and comment trick', "SELECT 1 /* /* */ ; DELETE FROM {x}.x_parents; -- */"),
        ('quote and comment trick', "SELECT 1 -- a comment\n; DELETE FROM {x}.x_parents"),
        ('quote and comment trick', "SELECT 1 /* a comment */ ; DELETE FROM {x}.x_parents"),
        ('other', "COPY {x}.x_parents TO PROGRAM 'cat'"),
        ('other', "COPY (SELECT 1) TO '/tmp/x'"),
        ('other', "DO $$ BEGIN DELETE FROM {x}.x_parents; END $$"),
        ('other', "EXPLAIN ANALYZE DELETE FROM {x}.x_parents"),
        ('other', "SET ROLE postgres"),
        ('other', "SET default_transaction_read_only = off"),
        ('other', "VACUUM {x}.x_parents"),
        ('other', "CALL {x}.x_touch()"),
        ('other', "TABLE {x}.x_parents"),
        ('other', "VALUES ({x}.x_touch())"),
        ('other', "SHOW server_version"),
        ('other', "NOTIFY channel"),
        ('other', "REFRESH MATERIALIZED VIEW {x}.mv"),
    ],
    'mysql': [
        ('write', "REPLACE INTO {x}.x_audit (id, note) VALUES (9, 'hostile')"),
        ('write', "INSERT INTO {x}.x_audit SELECT parent_id, label FROM {x}.x_parents"),
        ('write', "UPDATE {x}.x_parents p JOIN {x}.x_children c ON c.parent_id = p.parent_id SET p.label = 'hostile'"),
        ('ddl', "CREATE TABLE {x}.x_hostile AS SELECT * FROM {x}.x_parents"),
        ('ddl', "RENAME TABLE {x}.x_audit TO {x}.x_hostile"),
        ('select into', "SELECT * FROM {x}.x_parents INTO OUTFILE '/tmp/dx_hostile.csv'"),
        ('select into', "SELECT label INTO OUTFILE '/tmp/dx_hostile.csv' FROM {x}.x_parents"),
        ('select into', "SELECT label FROM {x}.x_parents LIMIT 1 INTO DUMPFILE '/tmp/dx_hostile.bin'"),
        ('select into', "SELECT COUNT(*) INTO @hostile FROM {x}.x_parents"),
        ('lock', "SELECT * FROM {x}.x_parents FOR UPDATE"),
        ('lock', "SELECT * FROM {x}.x_parents FOR SHARE"),
        ('lock', "SELECT * FROM {x}.x_parents LOCK IN SHARE MODE"),
        ('lock', "SELECT * FROM {x}.x_parents FOR UPDATE SKIP LOCKED"),
        ('lock', "SELECT * FROM {x}.x_parents FOR SHARE NOWAIT"),
        ('lock', "LOCK TABLES {x}.x_parents WRITE"),
        ('lock', "SELECT GET_LOCK('dx_hostile', 5)"),
        ('lock', "SELECT RELEASE_ALL_LOCKS()"),
        ('function', "SELECT {x}.x_touch()"),
        ('function', "SELECT {x}.x_touch() FROM {x}.x_parents LIMIT 1"),
        ('function', "SELECT SLEEP(5)"),
        ('function', "SELECT BENCHMARK(100000000, MD5('x'))"),
        ('function', "SELECT LOAD_FILE('/etc/passwd')"),
        ('function', "SELECT LAST_INSERT_ID(500)"),
        ('function', "SELECT `x_touch`()"),
        ('function', "SELECT * FROM {x}.x_parents PROCEDURE ANALYSE()"),
        ('quote and comment trick', "SELECT 1 /*!50000 , SLEEP(5) */"),
        ('quote and comment trick', "SELECT 1 /*! INTO OUTFILE '/tmp/dx_hostile.csv' */ FROM {x}.x_parents"),
        ('quote and comment trick', "SELECT /*+ SET_VAR(max_execution_time = 0) */ 1"),
        # "--" only starts a comment on MySQL when a space follows: 5--1 is arithmetic, and the rest runs
        ('quote and comment trick', "SELECT 5 --1 INTO OUTFILE '/tmp/dx_hostile.csv'"),
        ('quote and comment trick', "SELECT 1 # a comment\n INTO OUTFILE '/tmp/dx_hostile.csv'"),
        ('quote and comment trick', "SELECT '\\'' INTO OUTFILE '/tmp/dx_hostile.csv' -- '"),
        ('quote and comment trick', "SELECT \"a\\\" INTO OUTFILE '/tmp/dx_hostile.csv' -- \""),
        ('quote and comment trick', "SELECT 1 -- a comment\n; DELETE FROM {x}.x_parents"),
        ('quote and comment trick', "SELECT 1 /* a comment */ ; DELETE FROM {x}.x_parents"),
        ('quote and comment trick', "SELECT @a := (SELECT COUNT(*) FROM {x}.x_parents)"),
        ('other', "CALL {x}.x_touch()"),
        ('other', "DO SLEEP(5)"),
        ('other', "HANDLER {x}.x_parents OPEN"),
        ('other', "SET GLOBAL general_log = 1"),
        ('other', "SET SESSION TRANSACTION READ WRITE"),
        ('other', "LOAD DATA INFILE '/etc/passwd' INTO TABLE {x}.x_audit"),
        ('other', "SHOW GRANTS"),
        ('other', "FLUSH TABLES"),
        ('other', "KILL 55"),
        ('other', "USE mysql"),
    ],
}

# Statements that hold the same words as the ones above, and are plain reads: they must run.
# (A guard that refused these would only be refusing words.)
ALLOWED = {
    'oracle': [
        "SELECT COUNT(*) AS n FROM {x}.x_parents",
        "SELECT q'[it's; DELETE FROM x -- FOR UPDATE]' AS note, COUNT(*) AS n FROM {x}.x_parents GROUP BY q'[x]'",
        "SELECT p.parent_id, UPPER(p.label) AS label FROM {x}.x_parents p WHERE p.label LIKE 'parent 1%' ORDER BY p.parent_id FETCH FIRST 3 ROWS ONLY",
        "SELECT 'DROP TABLE; INSERT INTO; nextval' AS words, SYSDATE AS today FROM dual -- a comment; DELETE",
        "SELECT c.parent_id, COUNT(*) AS n FROM {x}.x_children c, {x}.x_parents p WHERE c.parent_id = p.parent_id(+) GROUP BY c.parent_id HAVING COUNT(*) > 4",
        "WITH big (grp, n) AS (SELECT grp, COUNT(*) FROM {x}.x_parents GROUP BY grp) SELECT grp, n FROM big ORDER BY grp",
        # what the name rules must keep working: a column alias in ORDER BY, alias.column, schema.table.column,
        # a WITH query in FROM and as a qualifier, a subquery's alias, LEVEL / ROWNUM / SYSDATE, a column of the block around
        "SELECT p.grp AS g, COUNT(*) AS n FROM {x}.x_parents p GROUP BY p.grp ORDER BY n DESC, g",
        "SELECT {x}.x_parents.code, x_parents.label FROM {x}.x_parents WHERE {x}.x_parents.grp = 1",
        "WITH big AS (SELECT grp, COUNT(*) AS n FROM {x}.x_parents GROUP BY grp) SELECT big.grp, b.n FROM big, big b WHERE big.grp = b.grp ORDER BY big.grp",
        "SELECT v.g, v.n FROM (SELECT grp g, COUNT(*) n FROM {x}.x_parents GROUP BY grp) v WHERE v.n > 1",
        "SELECT LEVEL AS lvl, ROWNUM AS rn, SYSDATE AS today FROM dual CONNECT BY LEVEL <= 3",
        "SELECT p.code FROM {x}.x_parents p WHERE EXISTS (SELECT 1 FROM {x}.x_children c WHERE c.parent_id = p.parent_id AND label IS NOT NULL)",
    ],
    'mssql': [
        "SELECT COUNT(*) AS n FROM {x}.x_parents",
        "SELECT TOP (3) [parent_id], [label] FROM {x}.x_parents WITH (NOLOCK) ORDER BY [parent_id]",
        "SELECT 'DROP TABLE; EXEC xp_cmdshell; UPDLOCK' AS words, LEN(label) AS n FROM {x}.x_parents WHERE parent_id = 1 -- DELETE",
        "SELECT grp, COUNT(*) AS n FROM {x}.x_parents /* DELETE FROM x */ GROUP BY grp ORDER BY grp",
        "SELECT CONVERT(VARCHAR(10), parent_id) + '-' + label AS tag FROM {x}.x_parents WHERE label LIKE 'parent 1%'",
    ],
    'postgresql': [
        "SELECT COUNT(*) AS n FROM {x}.x_parents",
        "SELECT (ARRAY[10, 20, 30])[2] AS second, 'a[1]; DELETE FROM x; nextval' AS words",
        "SELECT p.parent_id, upper(p.label) AS label FROM {x}.x_parents p WHERE p.label LIKE 'parent 1%' ORDER BY 1 LIMIT 3 -- FOR UPDATE",
        "SELECT grp, count(*) FILTER (WHERE parent_id > 100) AS high FROM {x}.x_parents GROUP BY grp ORDER BY grp",
        "WITH big AS (SELECT grp, count(*) AS n FROM {x}.x_parents GROUP BY grp) SELECT * FROM big WHERE n::int >= 20",
        "SELECT g FROM generate_series(1, 3) AS g",
        # table.column where the column is one: alias, (alias), schema.table, a subquery's alias
        "SELECT (p).label, p.*, p.parent_id FROM {x}.x_parents p WHERE p.grp = 1 ORDER BY p.parent_id LIMIT 3",
        "SELECT {x}.x_parents.code, x_parents.label FROM {x}.x_parents WHERE x_parents.grp = 1",
        "SELECT v.g, v.n FROM (SELECT grp AS g, count(*) AS n FROM {x}.x_parents GROUP BY grp) v WHERE v.n > 1",
        "WITH big AS (SELECT * FROM {x}.x_parents) SELECT big.code, c.child_id FROM big JOIN {x}.x_children c ON c.parent_id = big.parent_id LIMIT 5",
    ],
    'mysql': [
        "SELECT COUNT(*) AS n FROM {x}.x_parents",
        "SELECT `parent_id`, UPPER(`label`) AS label FROM {x}.x_parents WHERE label LIKE 'parent 1%' ORDER BY 1 LIMIT 3",
        "SELECT 'INTO OUTFILE; SLEEP(5); LOCK IN SHARE MODE' AS words, TRUNCATE(parent_id / 3, 1) AS third FROM {x}.x_parents WHERE parent_id = 7 -- DELETE",
        "SELECT grp, COUNT(*) AS n FROM {x}.x_parents /* DELETE FROM x */ GROUP BY grp ORDER BY grp",
        "SELECT IFNULL(label, '-') AS label, CHAR_LENGTH(label) AS n FROM {x}.x_parents ORDER BY parent_id LIMIT 2",
    ],
}


CALLS = {'oracle': ORACLE_CALLS, 'postgresql': POSTGRES_CALLS}


def calls(engine, x, t, proof=False):
    """[(what it is, statement)]: the calls without brackets of one engine (none on the others).
    proof: with the statements that are only asked of the server (ORACLE_NOT_CALLS)."""
    listed = CALLS.get(engine, []) + (ORACLE_NOT_CALLS if proof and engine == 'oracle' else [])
    return [(what, sql.replace('{x}', x).replace('{t}', t)) for what, sql in listed]


def battery(engine, x, t=None):
    """[(group, statement)] for one engine, with the schema names filled in."""
    listed = COMMON + ENGINE[engine] + [('call without brackets', sql) for _, sql in CALLS.get(engine, [])]
    return [(group, sql.replace('{x}', x).replace('{t}', t or x)) for group, sql in listed]


def allowed(engine, x):
    return [sql.replace('{x}', x) for sql in ALLOWED[engine]]
