-- ============================================================================
--  EduAgent 数据库迁移脚本
--  版本：2026-10-21-01
--  主题：知识库文档登记表（前端上传文档 → RAG 向量化入库 Milvus）
--  目标库：PostgreSQL 12 及以上
--
--  ── 本脚本包含的全部变更 ────────────────────────────────────────────────
--    1. 新建 kb_documents   知识库文档登记表（状态流转：processing → completed/failed）
--    2. 新建索引 idx_kb_documents_tenant_created（租户内按上传时间倒序查列表）
--    3. 补 updated_at 自动更新触发器 trg_kb_documents_updated_at
--
--  ── 设计说明 ──────────────────────────────────────────────────────────
--    kb_documents.id 同时就是 Milvus 侧的 document_id。
--    chunk 本体（含向量）存在 Milvus 的 knowledge_domain 集合里，本表只登记
--    「上传过哪些文档、处理到什么状态」，用于前端列表展示、状态轮询与删除联动：
--    删除文档时先按 id 删 Milvus 的 chunks，再删本表记录。
--
--  ── 执行方式 ──────────────────────────────────────────────────────────
--    psql "postgresql://用户:密码@主机:5433/eduagent" \
--         -f scripts/migrations/2026-10-21_01_kb_documents.sql
--    或在 DBeaver / Navicat / DataGrip 里打开整段执行。
--
--  ── 安全性 ────────────────────────────────────────────────────────────
--    · 全部 DDL 幂等（IF NOT EXISTS / DROP TRIGGER IF EXISTS 后再建），
--      可重复执行，跑几遍结果都一样
--    · 不含种子数据，不触碰任何存量业务表
--    · 整个脚本包在单个事务里，任何一步失败自动整体回滚，不会留半成品
--
--  ── 回滚方式（如需撤销，整段执行）────────────────────────────────────
--    BEGIN;
--    DROP TRIGGER IF EXISTS trg_kb_documents_updated_at ON kb_documents;
--    DROP INDEX  IF EXISTS idx_kb_documents_tenant_created;
--    DROP TABLE  IF EXISTS kb_documents;
--    COMMIT;
--    注意：回滚只删登记表，Milvus 里的 chunk 数据需要另行按 document_id 清理。
-- ============================================================================

SET client_encoding = 'UTF8';

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. kb_documents：知识库文档登记表
--    status 流转：processing（后台任务处理中）→ completed / failed
-- ---------------------------------------------------------------------------
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
);

-- ---------------------------------------------------------------------------
-- 2. 索引：文档列表页固定按租户 + 上传时间倒序查询
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_kb_documents_tenant_created
    ON kb_documents (tenant_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- 3. updated_at 自动更新触发器（与 init_db.sql 中其他表保持同一机制）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_kb_documents_updated_at ON kb_documents;
CREATE TRIGGER trg_kb_documents_updated_at
    BEFORE UPDATE ON kb_documents
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ---------------------------------------------------------------------------
-- 4. 只读自检（跑完直接看结果确认）
--    预期：table_exists = t，trigger_exists = t，index_exists = t
-- ---------------------------------------------------------------------------
SELECT
    (SELECT count(*) FROM information_schema.tables
      WHERE table_schema = 'public' AND table_name = 'kb_documents'
    )                                                        AS table_exists,
    (SELECT count(*) FROM pg_trigger
      WHERE tgrelid = 'kb_documents'::regclass AND tgname = 'trg_kb_documents_updated_at'
    )                                                        AS trigger_exists,
    (SELECT count(*) FROM pg_indexes
      WHERE tablename = 'kb_documents'
        AND indexname  = 'idx_kb_documents_tenant_created'
    )                                                        AS index_exists;

COMMIT;
