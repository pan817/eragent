-- ============================================================
-- 时区改造:trace_runs / trace_spans 升级为 TIMESTAMPTZ
-- ------------------------------------------------------------
-- 对应 alembic 迁移:0008_timezone_normalization
--
-- 用法:直接在 DB 客户端 (psql / DBeaver / Navicat) 执行。
-- 幂等:重复执行不会报错或重复升级。
--
-- 范围:仅处理 schema 迁移 + alembic_version 登记。
--       应用代码、conninfo 的 TimeZone 注入仍需部署新代码。
-- ============================================================

BEGIN;

-- ------------------------------------------------------------
-- 1. trace_runs: started_at / finished_at -> TIMESTAMPTZ
--    历史值按 UTC 解释一次,绝对时刻保留不变。
-- ------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'trace_runs'
          AND column_name = 'started_at'
          AND data_type = 'timestamp without time zone'
    ) THEN
        EXECUTE '
            ALTER TABLE trace_runs
            ALTER COLUMN started_at TYPE TIMESTAMPTZ
                USING started_at AT TIME ZONE ''UTC'',
            ALTER COLUMN finished_at TYPE TIMESTAMPTZ
                USING finished_at AT TIME ZONE ''UTC''
        ';
        RAISE NOTICE 'trace_runs columns upgraded to TIMESTAMPTZ';
    ELSE
        RAISE NOTICE 'trace_runs columns already TIMESTAMPTZ, skip';
    END IF;
END $$;

-- ------------------------------------------------------------
-- 2. trace_spans: started_at / finished_at -> TIMESTAMPTZ
-- ------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'trace_spans'
          AND column_name = 'started_at'
          AND data_type = 'timestamp without time zone'
    ) THEN
        EXECUTE '
            ALTER TABLE trace_spans
            ALTER COLUMN started_at TYPE TIMESTAMPTZ
                USING started_at AT TIME ZONE ''UTC'',
            ALTER COLUMN finished_at TYPE TIMESTAMPTZ
                USING finished_at AT TIME ZONE ''UTC''
        ';
        RAISE NOTICE 'trace_spans columns upgraded to TIMESTAMPTZ';
    ELSE
        RAISE NOTICE 'trace_spans columns already TIMESTAMPTZ, skip';
    END IF;
END $$;

-- ------------------------------------------------------------
-- 3. alembic_version 登记到 0008_timezone_normalization
--    前提:alembic 之前已跑到 0007;若未跑 0007,先用 alembic
--         追齐,不要直接改这一行。
-- ------------------------------------------------------------
DO $$
DECLARE
    current_rev TEXT;
BEGIN
    SELECT version_num INTO current_rev FROM alembic_version LIMIT 1;
    IF current_rev IS NULL THEN
        RAISE EXCEPTION 'alembic_version 为空,请先 alembic stamp 到已知版本';
    ELSIF current_rev = '0007_trace_composite_indexes' THEN
        UPDATE alembic_version SET version_num = '0008_timezone_normalization';
        RAISE NOTICE 'alembic_version: 0007 -> 0008';
    ELSIF current_rev = '0008_timezone_normalization' THEN
        RAISE NOTICE 'alembic_version 已经是 0008,skip';
    ELSE
        RAISE EXCEPTION 'alembic_version=% 非预期,请先把版本对齐到 0007', current_rev;
    END IF;
END $$;

COMMIT;

-- ============================================================
-- 4. (可选,推荐) 设置数据库级默认时区 —— 双保险
--    应用层已经用 '-c TimeZone=Asia/Shanghai' 注入会话时区;
--    这里再给整个 database 设一次默认值,避免将来有人用
--    没注入 options 的工具(psql 直连 / 第三方报表)看到 UTC。
-- ============================================================
--
-- 需要超级用户权限。执行后,对"新开的连接"生效。
-- 把下面一行的 <your_db> 替换为实际库名后再执行:
--
--   ALTER DATABASE <your_db> SET timezone = 'Asia/Shanghai';
--
-- 校验:新开一个连接后 SHOW timezone; 应返回 Asia/Shanghai

-- ============================================================
-- 5. 校验查询 —— 请在执行完上面的事务后,手工跑一遍
-- ============================================================
--
-- 5.1 列类型已升级
--   SELECT column_name, data_type
--   FROM information_schema.columns
--   WHERE table_name IN ('trace_runs','trace_spans')
--     AND column_name IN ('started_at','finished_at');
--   -- 期望:data_type 全部为 'timestamp with time zone'
--
-- 5.2 alembic 已到 0008
--   SELECT version_num FROM alembic_version;
--   -- 期望:0008_timezone_normalization
--
-- 5.3 当前会话时区(连接时应用代码会注入;直接 psql 则看 DB 默认)
--   SHOW timezone;
--   SELECT now();
--
-- 5.4 写入一个瞬时 TIMESTAMPTZ 验证偏移
--   SELECT now(), EXTRACT(timezone_hour FROM now()) AS offset_hours;
--   -- 期望:offset_hours = 8
