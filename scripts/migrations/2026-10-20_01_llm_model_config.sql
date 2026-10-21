-- ============================================================================
--  EduAgent 数据库迁移脚本
--  版本：2026-10-20-01
--  主题：大模型可配置化（前端自由切换模型）+ 模型故障自动切换
--  目标库：PostgreSQL 12 及以上
--
--  ── 本脚本包含的全部变更 ────────────────────────────────────────────────
--    1. 新建 llm_providers          厂商元数据（不含密钥）
--    2. 新建 llm_models             可选模型目录（每家 flash / pro 各一个）
--    3. 新建 llm_bindings           业务 Agent → 模型的绑定
--    4. 补列 llm_bindings.failover_enabled   故障自动切换开关
--    5. 种子数据：3 家厂商 / 6 个模型 / 7 条默认绑定
--
--  ── 执行方式 ──────────────────────────────────────────────────────────
--    psql "postgresql://用户:密码@主机:5432/eduagent" \
--         -f scripts/migrations/2026-10-20_01_llm_model_config.sql
--    或在 DBeaver / Navicat / DataGrip 里打开整段执行。
--
--  ── 安全性 ────────────────────────────────────────────────────────────
--    · 全部 DDL 幂等（IF NOT EXISTS），可重复执行，跑几遍结果都一样
--    · 种子数据一律 ON CONFLICT DO NOTHING：绝不覆盖已在页面上改过的绑定
--    · 整个脚本包在单个事务里，任何一步失败自动整体回滚，不会留半成品
--    · 不包含任何 DROP / TRUNCATE，不会破坏存量数据
--
--  ── 换环境时必须改的地方 ──────────────────────────────────────────────
--    llm_providers 里百炼的 base_url 是「专属 MaaS 端点」，含账号专属实例 ID
--    （llm-n4uunzelwrrg9l26）。换账号或换环境时请替换成该环境自己的地址，
--    否则百炼的所有模型都会 404 / 401。DeepSeek、小米是公共地址，无需修改。
--    另外三个 Key 只写在 .env.local，本脚本不含也不应含任何密钥。
-- ============================================================================

SET client_encoding = 'UTF8';

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. llm_providers：厂商元数据
--    设计要点：只存「密钥从哪个环境变量读」，密钥本身永远不入库
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS llm_providers (
    id               VARCHAR(32) PRIMARY KEY,              -- deepseek / xiaomi / bailian
    display_name     VARCHAR(64)  NOT NULL,
    base_url         VARCHAR(256) NOT NULL,                -- OpenAI 兼容端点
    api_key_env      VARCHAR(64)  NOT NULL,                -- 环境变量名，如 DEEPSEEK_API_KEY
    thinking_payload JSONB,                                -- 关闭思维链的 extra_body，不支持的厂商为 NULL
    is_enabled       BOOLEAN      NOT NULL DEFAULT TRUE,
    sort_order       INT          NOT NULL DEFAULT 0,
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  llm_providers                  IS '大模型厂商元数据，密钥不入库，只记录读取哪个环境变量';
COMMENT ON COLUMN llm_providers.api_key_env      IS '存放该厂商 API Key 的环境变量名';
COMMENT ON COLUMN llm_providers.thinking_payload IS 'extra_body 片段，用于关闭思维链；NULL 表示该厂商无此参数';

-- ---------------------------------------------------------------------------
-- 2. llm_models：可选模型目录
--    status 由 /api/v1/llm/sync 实测回写，前端下拉框据此置灰不可用项
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS llm_models (
    provider_id    VARCHAR(32)  NOT NULL REFERENCES llm_providers(id) ON DELETE CASCADE,
    model_id       VARCHAR(128) NOT NULL,                  -- 厂商真实模型名，如 qwen3.8-max
    display_name   VARCHAR(128) NOT NULL,
    tier           VARCHAR(16)  NOT NULL DEFAULT 'pro'
                   CHECK (tier IN ('flash', 'pro')),       -- flash 省钱 / pro 擅长推理
    context_window INT,
    supports_tools BOOLEAN      NOT NULL DEFAULT FALSE,    -- 评分类 Agent 依赖，必须为 TRUE
    status         VARCHAR(16)  NOT NULL DEFAULT 'unknown'
                   CHECK (status IN ('available', 'not_activated', 'error', 'unknown')),
    status_detail  TEXT,
    last_checked   TIMESTAMPTZ,
    is_selectable  BOOLEAN      NOT NULL DEFAULT TRUE,     -- 人工上架开关
    sort_order     INT          NOT NULL DEFAULT 0,
    PRIMARY KEY (provider_id, model_id)
);

COMMENT ON TABLE  llm_models               IS '可选模型目录，按厂商分组，每家保留 flash / pro 各一个';
COMMENT ON COLUMN llm_models.status        IS '实测状态：available 可用 / not_activated 未开通 / error 异常 / unknown 未探测';
COMMENT ON COLUMN llm_models.supports_tools IS '是否支持 function calling，结构化输出（批改、简历）要求为 TRUE';

-- ---------------------------------------------------------------------------
-- 3. llm_bindings：业务 Agent → 模型 的绑定（前端“模型配置”页保存的就是这张表）
--    failover_enabled 在全新库里随建表一起创建；存量库见第 4 步补列
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS llm_bindings (
    agent_type       VARCHAR(32)  PRIMARY KEY,             -- qa / exam_code / resume / ...
    provider_id      VARCHAR(32)  NOT NULL,
    model_id         VARCHAR(128) NOT NULL,
    temperature      NUMERIC(3,2) NOT NULL DEFAULT 0,
    failover_enabled BOOLEAN      NOT NULL DEFAULT TRUE,   -- 主模型不可用时是否自动切备选
    updated_by       UUID,
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  llm_bindings                  IS '业务 Agent 与模型的绑定关系，页面保存后即时生效';
COMMENT ON COLUMN llm_bindings.failover_enabled IS '自动故障切换开关；关闭后只用主模型，失败直接走降级';

-- ---------------------------------------------------------------------------
-- 4. 存量库补列：老库建表时没有 failover_enabled，这里补上
--    （全新库执行到这里是空操作，因为第 3 步已经建好了这列）
-- ---------------------------------------------------------------------------
ALTER TABLE llm_bindings
    ADD COLUMN IF NOT EXISTS failover_enabled BOOLEAN NOT NULL DEFAULT TRUE;

-- ---------------------------------------------------------------------------
-- 5. updated_at 自动维护触发器（仅 llm_providers）
--    写在前面的 CREATE OR REPLACE 是为了兼容没跑过 init_db.sql 的新库
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 先删后建，保证这段可以重复执行（CREATE TRIGGER 本身不支持 IF NOT EXISTS）
DROP TRIGGER IF EXISTS trg_llm_providers_updated_at ON llm_providers;
CREATE TRIGGER trg_llm_providers_updated_at
    BEFORE UPDATE ON llm_providers
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ---------------------------------------------------------------------------
-- 6. 种子数据
--    统一使用 ON CONFLICT DO NOTHING：重复执行不会覆盖线上已修改的配置
-- ---------------------------------------------------------------------------

-- 6.1 三家厂商（百炼的 base_url 为账号专属端点，换环境务必替换）
INSERT INTO llm_providers
    (id, display_name, base_url, api_key_env, thinking_payload, sort_order)
VALUES
    ('deepseek', 'DeepSeek', 'https://api.deepseek.com/v1',
     'DEEPSEEK_API_KEY', '{"thinking": {"type": "disabled"}}'::jsonb, 1),
    ('xiaomi', '小米 MiMo', 'https://api.xiaomimimo.com/v1',
     'XIAOMI_API_KEY', NULL, 2),
    ('bailian', '阿里百炼',
     'https://llm-n4uunzelwrrg9l26.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
     'BAILIAN_API_KEY', NULL, 3)
ON CONFLICT (id) DO NOTHING;

-- 6.2 每家两个模型：flash 省钱、pro 擅长推理
INSERT INTO llm_models
    (provider_id, model_id, display_name, tier, context_window,
     supports_tools, status, is_selectable, sort_order)
VALUES
    ('deepseek', 'deepseek-flash',  'DeepSeek V4.1 Flash', 'flash', 1048576, TRUE, 'available', TRUE, 1),
    ('deepseek', 'deepseek-v4-pro', 'DeepSeek V4 Pro',     'pro',   1048576, TRUE, 'available', TRUE, 2),
    ('xiaomi',   'mimo-v2.6-flash', 'MiMo V2.6 Flash',     'flash', NULL,    TRUE, 'available', TRUE, 1),
    ('xiaomi',   'mimo-v2.6-pro',   'MiMo V2.6 Pro',       'pro',   NULL,    TRUE, 'available', TRUE, 2),
    ('bailian',  'qwen3.8-flash',   '通义千问 3.8 Flash',  'flash', NULL,    TRUE, 'available', TRUE, 1),
    ('bailian',  'qwen3.8-max',     '通义千问 3.8 Max',    'pro',   NULL,    TRUE, 'available', TRUE, 2)
ON CONFLICT (provider_id, model_id) DO NOTHING;

-- 6.3 七个业务的默认绑定：全部指向 DeepSeek，与改造前行为一致
--     如需改初始值，改这里之前请先确认目标环境的意图，DO NOTHING 不会覆盖已有行
INSERT INTO llm_bindings (agent_type, provider_id, model_id, temperature, failover_enabled)
VALUES
    ('qa',              'deepseek', 'deepseek-v4-pro', 0.30, TRUE),
    ('exam_subjective', 'deepseek', 'deepseek-v4-pro', 0.00, TRUE),
    ('exam_code',       'deepseek', 'deepseek-v4-pro', 0.00, TRUE),
    ('resume',          'deepseek', 'deepseek-v4-pro', 0.00, TRUE),
    ('interview',       'deepseek', 'deepseek-v4-pro', 0.50, TRUE),
    ('intent',          'deepseek', 'deepseek-flash',  0.00, TRUE),
    ('summarize',       'deepseek', 'deepseek-flash',  0.00, TRUE)
ON CONFLICT (agent_type) DO NOTHING;

COMMIT;

-- ============================================================================
--  执行结果自检（只读查询，不影响数据）
--  期望：providers=3  models=6  bindings=7，且 bindings 全部 failover=true
-- ============================================================================

SELECT 'llm_providers' AS table_name, COUNT(*) AS rows FROM llm_providers
UNION ALL
SELECT 'llm_models',   COUNT(*) FROM llm_models
UNION ALL
SELECT 'llm_bindings', COUNT(*) FROM llm_bindings
ORDER BY table_name;

-- 绑定明细：确认每个业务指向哪个模型
SELECT agent_type,
       provider_id,
       model_id,
       temperature,
       failover_enabled
FROM   llm_bindings
ORDER  BY agent_type;

-- 模型目录：确认 status 与 tools 能力
SELECT provider_id, model_id, tier, status, supports_tools
FROM   llm_models
ORDER  BY provider_id, sort_order;

-- ============================================================================
--  回滚（默认注释，确需撤销时手工执行）
--  注意：会连同页面上配置的绑定一起删除，执行前请先导出 llm_bindings
--
--  BEGIN;
--  DROP TABLE IF EXISTS llm_bindings;
--  DROP TABLE IF EXISTS llm_models;      -- 有外键依赖 llm_providers，必须先删
--  DROP TABLE IF EXISTS llm_providers;
--  COMMIT;
--
--  只想关掉自动切换、保留表结构（更温和）：
--  UPDATE llm_bindings SET failover_enabled = FALSE;
-- ============================================================================
