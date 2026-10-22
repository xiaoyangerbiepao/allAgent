# backend/db/migrations.py
#
# 启动时自动执行的 Schema 补丁（全部幂等，可重复运行）。
# 规则：
#   - 只写 ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS 等幂等 DDL
#   - 禁止写 DROP / TRUNCATE 等破坏性变更
#   - 每次 init_db.sql 新增字段，同步在 _MIGRATIONS 里追加一条

from sqlalchemy import text
from backend.dependencies import AsyncSessionLocal
from backend.core.logger import get_logger

logger = get_logger(__name__)

# ── 所有需要补丁的 DDL，按时间顺序追加。SQL 必须幂等（IF NOT EXISTS）──
_MIGRATIONS: list[tuple[str, str]] = [
    (
        "exam_submissions.weak_points",
        "ALTER TABLE exam_submissions ADD COLUMN IF NOT EXISTS weak_points JSONB",
    ),
    (
        "exam_reviews.knowledge_tag",
        "ALTER TABLE exam_reviews ADD COLUMN IF NOT EXISTS knowledge_tag VARCHAR(128)",
    ),
    (
        "exam_submissions.weak_points_summary",
        "ALTER TABLE exam_submissions ADD COLUMN IF NOT EXISTS weak_points_summary TEXT",
    ),
    (
        "idx_exam_submissions_student_created",
        "CREATE INDEX IF NOT EXISTS idx_exam_submissions_student_created "
        "ON exam_submissions (student_id, created_at DESC)",
    ),
    (
        "idx_resume_reviews_student_created",
        "CREATE INDEX IF NOT EXISTS idx_resume_reviews_student_created "
        "ON resume_reviews (student_id, created_at DESC)",
    ),
    (
        "idx_interview_sessions_student_created",
        "CREATE INDEX IF NOT EXISTS idx_interview_sessions_student_created "
        "ON interview_sessions (student_id, created_at DESC)",
    ),

    # ── 大模型配置（前端可切换模型）────────────────────────────────
    # 三张表的分工：
    #   llm_providers：厂商元数据，只存“密钥读哪个环境变量”，不存密钥本身
    #   llm_models   ：可选模型目录（按业务只保留 flash / pro 各一个，避免下拉框爆炸）
    #   llm_bindings ：业务 Agent → 具体模型 的绑定，前端保存的就是这张表
    (
        "llm_providers",
        """
        CREATE TABLE IF NOT EXISTS llm_providers (
            id               VARCHAR(32) PRIMARY KEY,
            display_name     VARCHAR(64)  NOT NULL,
            base_url         VARCHAR(256) NOT NULL,
            api_key_env      VARCHAR(64)  NOT NULL,
            thinking_payload JSONB,
            is_enabled       BOOLEAN NOT NULL DEFAULT TRUE,
            sort_order       INT NOT NULL DEFAULT 0,
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
    ),
    (
        "llm_models",
        """
        CREATE TABLE IF NOT EXISTS llm_models (
            provider_id    VARCHAR(32)  NOT NULL REFERENCES llm_providers(id) ON DELETE CASCADE,
            model_id       VARCHAR(128) NOT NULL,
            display_name   VARCHAR(128) NOT NULL,
            tier           VARCHAR(16)  NOT NULL DEFAULT 'pro'
                           CHECK (tier IN ('flash', 'pro')),
            context_window INT,
            supports_tools BOOLEAN NOT NULL DEFAULT FALSE,
            status         VARCHAR(16)  NOT NULL DEFAULT 'unknown'
                           CHECK (status IN ('available', 'not_activated', 'error', 'unknown')),
            status_detail  TEXT,
            last_checked   TIMESTAMPTZ,
            is_selectable  BOOLEAN NOT NULL DEFAULT TRUE,
            sort_order     INT NOT NULL DEFAULT 0,
            PRIMARY KEY (provider_id, model_id)
        )
        """,
    ),
    (
        "llm_bindings",
        """
        CREATE TABLE IF NOT EXISTS llm_bindings (
            agent_type  VARCHAR(32) PRIMARY KEY,
            provider_id VARCHAR(32) NOT NULL,
            model_id    VARCHAR(128) NOT NULL,
            temperature NUMERIC(3,2) NOT NULL DEFAULT 0,
            updated_by  UUID,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
    ),
    (
        "seed.llm_providers",
        """
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
        ON CONFLICT (id) DO NOTHING
        """,
    ),
    # 每家只保留两个：flash 省钱、pro 擅长推理。context_window 为 null 表示厂商未声明。
    (
        "seed.llm_models",
        """
        INSERT INTO llm_models
            (provider_id, model_id, display_name, tier, context_window,
             supports_tools, status, is_selectable, sort_order)
        VALUES
            ('deepseek', 'deepseek-flash',   'DeepSeek V4.1 Flash', 'flash', 1048576, TRUE, 'available', TRUE, 1),
            ('deepseek', 'deepseek-v4-pro',  'DeepSeek V4 Pro',     'pro',   1048576, TRUE, 'available', TRUE, 2),
            ('xiaomi',   'mimo-v2.6-flash',  'MiMo V2.6 Flash',     'flash', NULL,    TRUE, 'available', TRUE, 1),
            ('xiaomi',   'mimo-v2.6-pro',    'MiMo V2.6 Pro',       'pro',   NULL,    TRUE, 'available', TRUE, 2),
            ('bailian',  'qwen3.8-flash',    '通义千问 3.8 Flash',  'flash', NULL,    TRUE, 'available', TRUE, 1),
            ('bailian',  'qwen3.8-max',      '通义千问 3.8 Max',    'pro',   NULL,    TRUE, 'available', TRUE, 2)
        ON CONFLICT (provider_id, model_id) DO NOTHING
        """,
    ),
    # 自动切换开关：主模型不可用时是否允许切到备选（见 llm_router.build_chain）
    # 存量库用 ADD COLUMN IF NOT EXISTS 补列，默认 TRUE = 保持「有备就切」的行为
    (
        "llm_bindings.failover_enabled",
        "ALTER TABLE llm_bindings "
        "ADD COLUMN IF NOT EXISTS failover_enabled BOOLEAN NOT NULL DEFAULT TRUE",
    ),
    # 默认全部指向 DeepSeek：与改造前的行为保持一致，前端改动不影响存量调用
    (
        "seed.llm_bindings",
        """
        INSERT INTO llm_bindings (agent_type, provider_id, model_id, temperature)
        VALUES
            ('qa',              'deepseek', 'deepseek-v4-pro', 0.30),
            ('exam_subjective', 'deepseek', 'deepseek-v4-pro', 0.00),
            ('exam_code',       'deepseek', 'deepseek-v4-pro', 0.00),
            ('resume',          'deepseek', 'deepseek-v4-pro', 0.00),
            ('interview',       'deepseek', 'deepseek-v4-pro', 0.50),
            ('intent',          'deepseek', 'deepseek-flash',  0.00),
            ('summarize',       'deepseek', 'deepseek-flash',  0.00)
        ON CONFLICT (agent_type) DO NOTHING
        """,
    ),

    # ── 知识库文档登记表（前端上传 → RAG 入库）────────────────────────
    # id 同时就是 Milvus 侧的 document_id；chunk 本体存在 Milvus（knowledge_domain），
    # 这张表只登记「上传过哪些文档、处理到什么状态」，用于列表展示与删除联动。
    (
        "kb_documents",
        """
        CREATE TABLE IF NOT EXISTS kb_documents (
            id           UUID PRIMARY KEY,
            filename     VARCHAR(500) NOT NULL,
            course_id    VARCHAR(64),
            tenant_id    VARCHAR(64)  NOT NULL DEFAULT 'tenant_default',
            size_bytes   BIGINT       NOT NULL DEFAULT 0,
            chunk_count  INTEGER      NOT NULL DEFAULT 0,
            use_context  BOOLEAN      NOT NULL DEFAULT FALSE,
            status       VARCHAR(16)  NOT NULL DEFAULT 'processing'
                         CHECK (status IN ('processing', 'completed', 'failed')),
            error_msg    TEXT,
            uploaded_by  VARCHAR(64),
            created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            finished_at  TIMESTAMPTZ
        )
        """,
    ),
    (
        "idx_kb_documents_tenant_created",
        "CREATE INDEX IF NOT EXISTS idx_kb_documents_tenant_created "
        "ON kb_documents (tenant_id, created_at DESC)",
    ),
]


async def run_migrations() -> None:
    """
    在应用启动时执行所有 Schema 补丁。
    单条失败只记录警告，不阻断启动流程。
    """
    async with AsyncSessionLocal() as session:
        for desc, sql in _MIGRATIONS:
            try:
                await session.execute(text(sql))
                await session.commit()
                logger.debug("db.migration_applied", column=desc)
            except Exception as e:
                await session.rollback()
                err = str(e)
                if "already exists" not in err:
                    logger.warning("db.migration_failed", column=desc, error=err)

    logger.info("db.migrations_done", count=len(_MIGRATIONS))
