# backend/api/v1/kb.py
#
# 知识库管理接口：教师上传文档 → 后台完成「解析分块 → Contextual RAG（可选）→
# BGE-M3 向量化 → 写入 Milvus」，前端轮询状态展示进度。
#
# 复用说明：整条建库流水线复用 scripts/build_knowledge_base.py 的既有函数
# （load_document / split_documents / add_context / embed_chunks / write_to_milvus），
# 本文件只做「HTTP 层 + 状态登记 + 事件循环保护」，不改算法逻辑。
#
# 三个端点：
#   POST   /kb/documents          上传文档并启动后台入库（立即返回 document_id）
#   GET    /kb/documents          当前租户的文档列表（前端轮询 processing 状态）
#   DELETE /kb/documents/{doc_id} 删除文档（先删 Milvus chunks，再删登记记录）
#
# 安全边界：全部接口要求登录，写操作（上传/删除）要求 teacher / admin 角色，
# 与前端路由 meta.requiresTeacher 双重校验。

import asyncio
import uuid
from pathlib import Path

from fastapi import (APIRouter, BackgroundTasks, Depends, File, Form,
                     HTTPException, UploadFile, status)
from sqlalchemy import text

from backend.core.knowledge_base import KnowledgeBaseClient
from backend.core.logger import get_logger
from backend.dependencies import AsyncSessionLocal, get_current_user
from scripts.build_knowledge_base import (add_context, embed_chunks,
                                          load_document, split_documents,
                                          write_to_milvus)

logger = get_logger(__name__)

router = APIRouter()

ALLOWED_EXTENSIONS = {".pdf", ".md", ".markdown"}
MAX_UPLOAD_BYTES = 50 * 1024 * 1024          # 50MB，防止误传超大文件
UPLOAD_DIR = Path(__file__).resolve().parents[3] / "uploads" / "kb"

# 与 resume.py 相同的 GC 保护：模块级集合持有后台任务强引用，防止被垃圾回收
_background_tasks: set = set()


def _require_operator(user: dict) -> None:
    """写操作的角色校验：teacher 与 admin 可以，student 一律拒绝。"""
    if user.get("role") not in ("teacher", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="只有教师或管理员可以管理知识库文档",
        )


async def _set_status(doc_id: str, new_status: str,
                      chunk_count: int | None = None,
                      error_msg: str | None = None) -> None:
    """更新登记表状态。后台任务没有请求级 session，这里新开一个。"""
    async with AsyncSessionLocal() as session:
        if new_status == "completed":
            await session.execute(text(
                "UPDATE kb_documents "
                "SET status = :s, chunk_count = :n, finished_at = NOW() "
                "WHERE id = :id"
            ), {"s": new_status, "n": chunk_count or 0, "id": doc_id})
        elif new_status == "failed":
            await session.execute(text(
                "UPDATE kb_documents "
                "SET status = :s, error_msg = :e, finished_at = NOW() "
                "WHERE id = :id"
            ), {"s": new_status, "e": (error_msg or "")[:2000], "id": doc_id})
        else:
            await session.execute(text(
                "UPDATE kb_documents SET status = :s WHERE id = :id"
            ), {"s": new_status, "id": doc_id})
        await session.commit()


async def _run_ingestion(doc_id: str, file_path: Path,
                         course_id: str | None, tenant_id: str,
                         use_context: bool) -> None:
    """后台入库流水线。CPU 密集段全部丢进线程池，避免卡死事件循环。

    步骤与 build_pipeline 一致（读取 → 分块 → 上下文增强（可选）→ 嵌入 → 写入），
    唯一区别是每一步的成败都会写回 kb_documents 状态。
    """
    try:
        loop = asyncio.get_running_loop()

        logger.info("kb.ingest_start | doc=%s file=%s use_context=%s",
                    doc_id, file_path.name, use_context)

        # Step 1/2：读取 + 分块（同步 CPU，丢线程池）
        docs = await loop.run_in_executor(None, load_document, str(file_path))
        chunks = await loop.run_in_executor(
            None, lambda: split_documents(docs, str(file_path)))

        # Step 2.5：Contextual RAG（async，内部自带并发限流与失败降级）
        if use_context and chunks:
            chunks = await add_context(chunks, docs)

        # Step 3/4：嵌入 + 写入 Milvus（同步 CPU/IO，丢线程池）
        doc_chunks = await loop.run_in_executor(
            None,
            lambda: embed_chunks(chunks, course_id=course_id or "",
                                 document_id=doc_id, tenant_id=tenant_id),
        )
        await loop.run_in_executor(None, write_to_milvus, doc_chunks)

        await _set_status(doc_id, "completed", chunk_count=len(doc_chunks))
        logger.info("kb.ingest_done | doc=%s chunks=%s", doc_id, len(doc_chunks))

    except Exception as e:
        logger.error("kb.ingest_failed | doc=%s error=%s", doc_id, e, exc_info=True)
        try:
            await _set_status(doc_id, "failed", error_msg=str(e))
        except Exception:
            logger.exception("kb.ingest_status_update_failed | doc=%s", doc_id)
    finally:
        # 临时文件用完即删，服务器不留用户上传的原文
        try:
            file_path.unlink(missing_ok=True)
        except OSError:
            pass


@router.post("/documents")
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="支持 .pdf / .md / .markdown"),
    course_id: str = Form(default="", description="可选，课程 ID，限定检索范围"),
    use_context: bool = Form(default=False,
                             description="是否启用 Contextual RAG（会消耗 LLM token，较慢）"),
    current_user: dict = Depends(get_current_user),
):
    """上传文档并启动后台向量化入库。立即返回 document_id，前端轮询状态。"""
    _require_operator(current_user)

    filename = file.filename or ""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的文件类型 {ext or '(无后缀)'}，仅支持 .pdf / .md / .markdown",
        )

    content = await file.read()
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="上传文件为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="文件超过 50MB 上限")

    doc_id = str(uuid.uuid4())
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    file_path = UPLOAD_DIR / f"{doc_id}{ext}"
    file_path.write_bytes(content)

    tenant_id = current_user["tenant_id"]

    # 先登记（status=processing），再派发后台任务：保证前端立刻能查到这条记录
    async with AsyncSessionLocal() as session:
        await session.execute(text(
            "INSERT INTO kb_documents "
            "  (id, filename, course_id, tenant_id, size_bytes, use_context, "
            "   status, uploaded_by) "
            "VALUES (:id, :fn, :cid, :tid, :size, :uc, 'processing', :by)"
        ), {"id": doc_id, "fn": filename, "cid": course_id or None,
            "tid": tenant_id, "size": len(content),
            "uc": use_context, "by": current_user["user_id"]})
        await session.commit()

    task = asyncio.create_task(
        _run_ingestion(doc_id, file_path, course_id or None, tenant_id, use_context))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    logger.info("kb.upload_accepted | doc=%s file=%s size=%s by=%s",
                doc_id, filename, len(content), current_user["user_id"])

    return {
        "document_id": doc_id,
        "filename": filename,
        "status": "processing",
        "message": "已开始后台入库，请稍后刷新列表查看结果",
    }


@router.get("/documents")
async def list_documents(current_user: dict = Depends(get_current_user)):
    """当前租户的知识库文档列表（新→旧，最多 200 条）。"""
    _require_operator(current_user)
    tenant_id = current_user["tenant_id"]

    async with AsyncSessionLocal() as session:
        result = await session.execute(text(
            "SELECT id, filename, course_id, size_bytes, chunk_count, "
            "       use_context, status, error_msg, uploaded_by, "
            "       created_at, finished_at "
            "FROM kb_documents WHERE tenant_id = :tid "
            "ORDER BY created_at DESC LIMIT 200"
        ), {"tid": tenant_id})
        rows = result.mappings().all()

    return {
        "total": len(rows),
        "documents": [
            {
                "id":           str(r["id"]),
                "filename":     r["filename"],
                "course_id":    r["course_id"],
                "size_bytes":   r["size_bytes"],
                "chunk_count":  r["chunk_count"],
                "use_context":  r["use_context"],
                "status":       r["status"],
                "error_msg":    r["error_msg"],
                "uploaded_by":  r["uploaded_by"],
                "created_at":   r["created_at"].isoformat() if r["created_at"] else None,
                "finished_at":  r["finished_at"].isoformat() if r["finished_at"] else None,
            }
            for r in rows
        ],
    }


@router.delete("/documents/{doc_id}")
async def delete_document(doc_id: str, current_user: dict = Depends(get_current_user)):
    """删除文档：先删 Milvus 里该 document_id 的全部 chunks，再删登记记录。"""
    _require_operator(current_user)
    tenant_id = current_user["tenant_id"]

    try:
        uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="无效的文档 ID")

    async with AsyncSessionLocal() as session:
        result = await session.execute(text(
            "SELECT id FROM kb_documents WHERE id = :id AND tenant_id = :tid"
        ), {"id": doc_id, "tid": tenant_id})
        if result.first() is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="文档不存在")
        await session.execute(
            text("DELETE FROM kb_documents WHERE id = :id"), {"id": doc_id})
        await session.commit()

    # Milvus 删除是同步 IO，丢线程池；失败只告警——登记记录已删，
    # 孤儿 chunks 可在 Milvus 侧按 document_id 手工清理
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None, lambda: KnowledgeBaseClient().delete_document_chunks(doc_id))
        logger.info("kb.delete_done | doc=%s by=%s", doc_id, current_user["user_id"])
    except Exception as e:
        logger.warning("kb.delete_milvus_failed | doc=%s error=%s", doc_id, e)
        return {"document_id": doc_id, "deleted": True,
                "warning": f"登记记录已删除，但 Milvus 清理失败：{e}"}

    return {"document_id": doc_id, "deleted": True}
