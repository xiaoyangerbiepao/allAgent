-- ============================================================
-- EduAgent PostgreSQL 数据库初始化脚本
-- Docker 启动时自动执行（挂载到 /docker-entrypoint-initdb.d/）
-- ============================================================

-- 启用 UUID 自动生成扩展
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================
-- 用户与权限表
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
    username        VARCHAR(64) NOT NULL,
    email           VARCHAR(128) NOT NULL,
    password_hash   VARCHAR(256) NOT NULL,
    role            VARCHAR(16) NOT NULL CHECK (role IN ('student', 'teacher', 'admin')),
    class_id        UUID,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, email)
);
CREATE INDEX idx_users_tenant_id ON users (tenant_id);
CREATE INDEX idx_users_role ON users (role);
CREATE INDEX idx_users_class_id ON users (class_id);

-- ============================================================
-- 知识库待补充队列
-- ============================================================
CREATE TABLE IF NOT EXISTS knowledge_pending_queue (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
    question        TEXT NOT NULL,
    student_id      UUID REFERENCES users(id),
    confidence      FLOAT NOT NULL,
    status          VARCHAR(16) NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'resolved', 'dismissed')),
    resolved_by     UUID REFERENCES users(id),
    resolved_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_knowledge_pending_queue_tenant_id ON knowledge_pending_queue (tenant_id);
CREATE INDEX idx_knowledge_pending_queue_status ON knowledge_pending_queue (status);

-- ============================================================
-- 试卷批改相关表
-- ============================================================
CREATE TABLE IF NOT EXISTS exams (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
    title           VARCHAR(256) NOT NULL,
    description     TEXT,
    due_date        TIMESTAMPTZ,
    created_by      UUID REFERENCES users(id),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_exams_tenant_id ON exams (tenant_id);

CREATE TABLE IF NOT EXISTS questions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
    exam_id         UUID REFERENCES exams(id) ON DELETE CASCADE,
    question_no     INT NOT NULL,
    question_type   VARCHAR(16) NOT NULL
                    CHECK (question_type IN ('single_choice', 'multi_choice', 'judge', 'short_answer', 'code')),
    content         TEXT NOT NULL,
    correct_answer  TEXT,
    score           INT NOT NULL DEFAULT 10,
    knowledge_tag   VARCHAR(128),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_questions_exam_id ON questions (exam_id);
CREATE INDEX idx_questions_knowledge_tag ON questions (knowledge_tag);

CREATE TABLE IF NOT EXISTS scoring_points (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    question_id     UUID REFERENCES questions(id) ON DELETE CASCADE,
    point_desc      TEXT NOT NULL,
    point_score     INT NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    confirmed_by    UUID REFERENCES users(id),
    confirmed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_scoring_points_question_id ON scoring_points (question_id);

CREATE TABLE IF NOT EXISTS exam_submissions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
    exam_id         UUID REFERENCES exams(id),
    student_id      UUID REFERENCES users(id),
    source          VARCHAR(16) NOT NULL DEFAULT 'word'
                    CHECK (source IN ('word', 'online', 'miniapp')),
    word_minio_path VARCHAR(512),
    status          VARCHAR(16) NOT NULL DEFAULT 'submitted'
                    CHECK (status IN ('submitted', 'ai_processing', 'pending_review', 'reviewed', 'published')),
    submitted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at    TIMESTAMPTZ,
    weak_points          JSONB,
    weak_points_summary  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (exam_id, student_id)
);
CREATE INDEX idx_exam_submissions_tenant_id ON exam_submissions (tenant_id);
CREATE INDEX idx_exam_submissions_exam_id ON exam_submissions (exam_id);
CREATE INDEX idx_exam_submissions_student_id ON exam_submissions (student_id);
CREATE INDEX idx_exam_submissions_status ON exam_submissions (status);

CREATE TABLE IF NOT EXISTS exam_reviews (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    submission_id   UUID REFERENCES exam_submissions(id) ON DELETE CASCADE,
    question_id     UUID REFERENCES questions(id),
    question_type   VARCHAR(16) NOT NULL,
    knowledge_tag   VARCHAR(128),
    student_answer  TEXT,
    ai_score        INT,
    ai_feedback     TEXT,
    ai_raw_result   JSONB,
    teacher_score   INT,
    teacher_comment TEXT,
    final_score     INT,
    needs_review    BOOLEAN NOT NULL DEFAULT FALSE,
    reviewed_by     UUID REFERENCES users(id),
    reviewed_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_exam_reviews_submission_id ON exam_reviews (submission_id);
CREATE INDEX idx_exam_reviews_needs_review ON exam_reviews (needs_review);
CREATE INDEX idx_exam_reviews_knowledge_tag ON exam_reviews (knowledge_tag);

-- ============================================================
-- 简历审查相关表
-- ============================================================
CREATE TABLE IF NOT EXISTS resume_reviews (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
    student_id      UUID REFERENCES users(id),
    pdf_minio_path  VARCHAR(512) NOT NULL,
    structured_data JSONB,
    scores          JSONB,
    issues          JSONB,
    summary         JSONB,
    status          VARCHAR(16) NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'processing', 'done', 'failed')),
    error_msg       TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_resume_reviews_tenant_id ON resume_reviews (tenant_id);
CREATE INDEX idx_resume_reviews_student_id ON resume_reviews (student_id);
CREATE INDEX idx_resume_reviews_status ON resume_reviews (status);

-- ============================================================
-- 模拟面试相关表
-- ============================================================
CREATE TABLE IF NOT EXISTS interview_questions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
    content         TEXT NOT NULL,
    difficulty      VARCHAR(8) NOT NULL DEFAULT 'medium'
                    CHECK (difficulty IN ('easy', 'medium', 'hard')),
    tags            JSONB NOT NULL DEFAULT '[]',
    target_position VARCHAR(128) NOT NULL DEFAULT 'general',
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_by      UUID REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_interview_questions_tenant_id       ON interview_questions (tenant_id);
CREATE INDEX idx_interview_questions_target_position ON interview_questions (target_position);
CREATE INDEX idx_interview_questions_difficulty      ON interview_questions (difficulty);
CREATE INDEX idx_interview_questions_is_active       ON interview_questions (is_active);

CREATE TABLE IF NOT EXISTS interview_sessions (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id        VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
    student_id       UUID REFERENCES users(id),
    session_id       VARCHAR(128) NOT NULL,
    thread_id        VARCHAR(128) NOT NULL UNIQUE,
    target_position  VARCHAR(128) NOT NULL DEFAULT '',
    resume_review_id UUID REFERENCES resume_reviews(id),
    summary          TEXT,
    report           JSONB,
    overall_score    INT,
    status           VARCHAR(16) NOT NULL DEFAULT 'in_progress'
                     CHECK (status IN ('in_progress', 'finished')),
    finished_at      TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_interview_sessions_tenant_id  ON interview_sessions (tenant_id);
CREATE INDEX idx_interview_sessions_student_id ON interview_sessions (student_id);
CREATE INDEX idx_interview_sessions_session_id ON interview_sessions (session_id);
CREATE INDEX idx_interview_sessions_status     ON interview_sessions (status);

-- ============================================================
-- 问答会话表
-- ============================================================
CREATE TABLE IF NOT EXISTS qa_sessions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
    student_id      UUID REFERENCES users(id),
    thread_id       VARCHAR(128) NOT NULL UNIQUE,
    summary         TEXT,
    summary_version INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_qa_sessions_tenant_id ON qa_sessions (tenant_id);
CREATE INDEX idx_qa_sessions_student_id ON qa_sessions (student_id);
CREATE INDEX idx_qa_sessions_thread_id ON qa_sessions (thread_id);

-- ============================================================
-- 大模型配置表（前端可切换模型）
-- 密钥不入库，只记录“该厂商的 Key 从哪个环境变量读”
-- ============================================================
CREATE TABLE IF NOT EXISTS llm_providers (
    id               VARCHAR(32) PRIMARY KEY,
    display_name     VARCHAR(64)  NOT NULL,
    base_url         VARCHAR(256) NOT NULL,
    api_key_env      VARCHAR(64)  NOT NULL,
    thinking_payload JSONB,
    is_enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order       INT NOT NULL DEFAULT 0,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

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
);

CREATE TABLE IF NOT EXISTS llm_bindings (
    agent_type       VARCHAR(32) PRIMARY KEY,
    provider_id      VARCHAR(32) NOT NULL,
    model_id         VARCHAR(128) NOT NULL,
    temperature      NUMERIC(3,2) NOT NULL DEFAULT 0,
    -- 主模型不可用时是否自动切到备选（llm_router.build_chain 读这个开关）
    failover_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    updated_by       UUID,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 存量库补列（与 backend/db/migrations.py 里的同名条目保持一致）
ALTER TABLE llm_bindings
    ADD COLUMN IF NOT EXISTS failover_enabled BOOLEAN NOT NULL DEFAULT TRUE;

-- 种子数据（3 家厂商 / 每家 flash + pro 两个模型 / 7 条默认绑定）不写在这里。
-- 全新环境请在本脚本之后，再执行一次独立的迁移脚本（幂等，可重复跑）：
--     psql "连接串" -f scripts/migrations/2026-10-20_01_llm_model_config.sql
-- 该脚本同时负责给存量库补 failover_enabled 列。
-- 应用启动时 backend/db/migrations.py 也会自动执行同样的变更，两条路等效。

-- ============================================================
-- 自动更新 updated_at 触发器
-- ============================================================
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'users',
        'exams', 'exam_submissions', 'exam_reviews',
        'resume_reviews', 'interview_sessions',
        'interview_questions', 'qa_sessions', 'llm_providers'
    ]
    LOOP
        EXECUTE format('
            CREATE TRIGGER trg_%s_updated_at
            BEFORE UPDATE ON %s
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
        ', t, t);
    END LOOP;
END;
$$;
