# ruff: noqa: E402  # 本文件有硬性 import 顺序约束（假 utils 注入必须先于 import retrieve）
"""混合检索 MCP 服务（Agent 解耦接入，计划 §P3'）。

职责：把 ``D:\\VSCodeProject\\混合检索RAG`` 的检索能力经 MCP over streamable-http
暴露给 Agent，唯一工具 ``search_knowledge_base``，出参对齐计划 §7.2 的
KnowledgeChunk 契约（11 字段，None 归一为 0/0.0）。

工程处理（两处）：
1. 控制台日志补丁——原 utils.Log 依赖文件轮转（会在工作区外写日志文件），
   且 concurrent_log_handler 的导入会破坏 FlagEmbedding 最先的导入顺序约束，
   故在 import retrieve 之前向 sys.modules 预注入纯 stdout 版假 utils 模块。
2. Retriever 单例 + 进程级锁——BGE-M3 模型加载约 7s，缓存复用；
   检索时临时改写 final_top_n/rerank_top_n（finally 还原），串行化防状态漂移。

部署形态说明：本文件寄放在 Agent 仓库（Multi_Agent/scripts/rag/）而非 RAG 仓库，
原因是 RAG 仓库对其部署环境零侵入（只经 sys.path 只读引用其 src/），
且 Agent 侧 .venv312 已具备全部依赖链（fastmcp/FlagEmbedding/torch/jieba/qdrant_client）。

导入顺序硬约束：必须在 import retrieve 之前完成假 utils 注入；
retrieve 模块内部保证 FlagEmbedding 链最先，业务代码不得抢先导入 numpy 等。

启动：
    python scripts/rag/serve_mcp.py
环境变量：
    KB_COLLECTION        默认 collection 名（缺省 agent_knowledge）
    RAG_MCP_HOST/RAG_MCP_PORT  监听地址（缺省 127.0.0.1:8100）
    SILICONFLOW_API_KEY  rerank 开启时必需（config.yaml 的 api_key_env）
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import types
from pathlib import Path

RAG_ROOT = Path(r"D:\VSCodeProject\混合检索RAG")
sys.path.insert(0, str(RAG_ROOT / "src"))


# ── 控制台日志补丁（必须在 import retrieve 之前）──
def _install_console_log_patch() -> None:
    """向 sys.modules 注入纯 stdout 版 utils.Log，替换文件轮转日志。"""

    class _Log:
        @staticmethod
        def init_logger(
            log_path: str | None = None,
            log_level: int = logging.INFO,
            **_ignored: object,
        ) -> logging.Logger:
            name = Path(log_path).stem if log_path else "rag"
            _logger = logging.getLogger(f"rag.{name}")
            if not _logger.handlers:
                handler = logging.StreamHandler()
                handler.setFormatter(
                    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
                )
                _logger.addHandler(handler)
            _logger.setLevel(log_level)
            _logger.propagate = False
            return _logger

    fake = types.ModuleType("utils")
    fake.Log = _Log  # type: ignore[attr-defined]
    sys.modules["utils"] = fake


_install_console_log_patch()

import retrieve  # 必须最先！其内部保证 FlagEmbedding 链顺序

idx = retrieve.idx

from fastmcp import FastMCP

logger = logging.getLogger("rag.serve_mcp")


# ── openai 3.x 兼容补丁（RAG 仓库只读，只能在服务层修）──
def _patch_rerank_cast_to() -> None:
    """修复 retrieve.Retriever._rerank 与 openai>=3 的兼容缺陷。

    原实现 client.post("/rerank", cast_to=dict)：openai 3.3.0 的
    construct_type 对裸 dict 执行 ``_, items_type = get_args(type_)``，
    裸 dict 无类型参数 → ValueError: not enough values to unpack。
    此处以同语义实现整体替换（仅 cast_to 改为 dict[str, Any]，
    错误消息/不重试不降级口径与原实现逐行一致）。
    """
    from typing import Any

    def _rerank(self: retrieve.Retriever, query: str, cands: list[dict]) -> list[dict]:
        rr = self._rerank_cfg
        key_env = str(rr.get("api_key_env", "SILICONFLOW_API_KEY"))
        api_key = os.environ.get(key_env, "").strip()
        if not api_key:
            raise RuntimeError(f"环境变量 {key_env} 未设置，无法调用重排 API")
        if self._client is None:  # 惰性创建，生命周期同 Retriever
            from openai import OpenAI

            self._client = OpenAI(
                base_url=rr["base_url"],
                api_key=api_key,
                timeout=60.0,
                max_retries=0,  # 失败立即抛错（用户定版：不重试不降级）
            )
        payload = {
            "model": rr["model"],
            "query": query,
            "documents": [c["text"] for c in cands],
            "top_n": len(cands),  # 全量回传打分，截断留给 retrieve()
            "return_documents": False,
        }
        try:
            resp = self._client.post("/rerank", body=payload, cast_to=dict[str, Any])
            results = resp["results"]
        except Exception as exc:
            raise RuntimeError(f"重排 API 调用失败（{rr['base_url']}/rerank）：{exc}") from exc
        if not results:
            raise RuntimeError(f"重排 API 返回空 results：{str(resp)[:200]}")
        out: list[dict] = []
        for item in results:  # API 保证按 relevance_score 降序
            c = dict(cands[int(item["index"])])
            c["rerank_score"] = float(item["relevance_score"])
            out.append(c)
        return out

    retrieve.Retriever._rerank = _rerank


_patch_rerank_cast_to()

# ── Langfuse 可观测：给检索打 span（与主 Agent 跨进程关联，见注释）──
# 本服务是独立进程，trace 通过 session_id 与主 Agent 的对话会话关联，
# 使 Langfuse 面板能按 session 回放「主 Agent 对话 → 触发 RAG 检索」的完整链路。
# 复用 Agent 仓库的 app.observability（读 config/agent.yaml 的 langfuse 段）；
# 未启用时优雅降级为 no-op，不影响检索功能。
_AGENT_BACKEND = Path(__file__).resolve().parents[2] / "backend"
if str(_AGENT_BACKEND) not in sys.path:
    sys.path.insert(0, str(_AGENT_BACKEND))

try:
    from langfuse import observe

    from app.observability import get_langfuse as _get_langfuse  # 初始化全局客户端用

    _LANGFUSE_AVAILABLE = True
except Exception:  # pragma: no cover - langfuse 未安装时降级
    _LANGFUSE_AVAILABLE = False

    def observe(func=None, **kwargs):  # type: ignore[no-redef]
        if func is None:
            return lambda f: f
        return func


def _init_langfuse() -> None:
    """显式初始化 Langfuse 全局客户端（读 agent.yaml 配置），使 @observe 能复用。"""
    if _LANGFUSE_AVAILABLE:
        try:
            _get_langfuse()
        except Exception:  # pragma: no cover - 失败静默降级
            pass


DEFAULT_COLLECTION = os.environ.get("KB_COLLECTION", "agent_knowledge").strip() or "agent_knowledge"
HOST = os.environ.get("RAG_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("RAG_MCP_PORT", "8100"))
MAX_TOP_K = 20

_LOCK = threading.Lock()  # idx.COLLECTION_NAME/final_top_n 是进程级可变状态，检索串行化
_RETRIEVERS: dict[str, retrieve.Retriever] = {}


def get_retriever(collection: str) -> retrieve.Retriever:
    """按 collection 缓存 Retriever（首次构造会加载 BGE-M3，约 7s）。"""
    cached = _RETRIEVERS.get(collection)
    if cached is None:
        logger.info("初始化 Retriever（collection=%s，加载 BGE-M3 约 7s）...", collection)
        idx.COLLECTION_NAME = collection  # 构造校验读它，必须前置注值
        cached = retrieve.Retriever()
        _RETRIEVERS[collection] = cached
        logger.info("Retriever 就绪（collection=%s）", collection)
    return cached


def to_knowledge_chunk(hit: dict) -> dict:
    """§7.2 映射：None→0/0.0；doc_id←point_id；title←公司+年；领域字段进 extra。"""
    src = hit.get("source") or {}
    company = str(src.get("company_name") or "")
    raw_year = src.get("fiscal_year")
    year = int(raw_year) if raw_year else 0
    title = f"{company} {year}年报" if company and year else company
    point_id = str(hit.get("point_id", ""))
    return {
        "point_id": point_id,
        "text": str(hit.get("text", "")),
        "source": {
            "doc_id": point_id,
            "title": title,
            "section": "",
            "page": int(src.get("page_num") or 0),
            "uri": "",
            "extra": {
                "company_name": company,
                "stock_code": str(src.get("stock_code") or ""),
                "fiscal_year": year,
                "chunk_type": str(src.get("chunk_type") or ""),
            },
        },
        "rrf_score": float(hit.get("rrf_score") or 0.0),
        "score_dense": float(hit.get("score_dense") or 0.0),
        "score_sparse": float(hit.get("score_sparse") or 0.0),
        "rank_dense": int(hit.get("rank_dense") or 0),
        "rank_sparse": int(hit.get("rank_sparse") or 0),
        "rerank_score": float(hit.get("rerank_score") or 0.0),
        "rerank_rank": int(hit.get("rerank_rank") or 0),
    }


mcp = FastMCP(
    name="rag-knowledge",
    instructions=(
        "年报知识库混合检索服务（BGE-M3 dense + BM25 双路召回，RRF 融合，硅基流动重排）。"
        "唯一工具 search_knowledge_base：输入自然语言问题，返回最相关的资料片段列表，"
        "每条含原文 text、来源 source（公司/年报年份/页码）与相关性分数。"
        "回答事实性问题时应先检索并引用来源，检索为空时如实告知。"
    ),
)


@mcp.tool
@observe(name="rag_retrieve", as_type="retriever", capture_input=True, capture_output=True)
def search_knowledge_base(
    query: str, top_k: int = 5, collection: str = DEFAULT_COLLECTION
) -> list[dict]:
    """在知识库中混合检索与 query 最相关的资料片段（带来源与相关性分数）。

    Args:
        query: 自然语言检索问题。
        top_k: 返回条数上限（1~20，缺省 5）。
        collection: Qdrant collection 名（缺省 agent_knowledge）。
    """
    text = (query or "").strip()
    if not text:
        return []
    limit = max(1, min(int(top_k or 5), MAX_TOP_K))
    name = (collection or "").strip() or DEFAULT_COLLECTION
    with _LOCK:
        r = get_retriever(name)
        idx.COLLECTION_NAME = name  # _route 检索时实时读模块级变量
        saved_top_n, saved_rerank_n = r.final_top_n, r.rerank_top_n
        try:
            r.final_top_n = limit
            if limit > r.rerank_top_n:
                r.rerank_top_n = limit
            hits = r.retrieve(text)
        finally:
            r.final_top_n, r.rerank_top_n = saved_top_n, saved_rerank_n
    return [to_knowledge_chunk(h) for h in hits[:limit]]


def warmup() -> None:
    """构造默认 Retriever + 哑编码（不发真查询，省 rerank API 配额）；失败只告警。"""
    _init_langfuse()
    try:
        r = get_retriever(DEFAULT_COLLECTION)
        r._encode("预热")
        logger.info("预热完成（collection=%s）", DEFAULT_COLLECTION)
    except Exception as exc:
        logger.warning("预热失败（不影响启动，首个请求将重试）：%s", exc)
    if not os.environ.get("SILICONFLOW_API_KEY", "").strip():
        logger.warning("环境变量 SILICONFLOW_API_KEY 未设置：config.rerank 开启时检索将报错")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    warmup()
    logger.info("MCP 服务启动：http://%s:%d/mcp（streamable-http）", HOST, PORT)
    mcp.run(transport="http", host=HOST, port=PORT, path="/mcp")
