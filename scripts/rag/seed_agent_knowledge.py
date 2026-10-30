"""Agent 独立知识库灌种脚本（计划 §P3'：agent_knowledge collection）。

从 ``D:\\VSCodeProject\\混合检索RAG`` 的既有产物（data/chunks 的 chunk json +
data/embeddings 的向量 npz 缓存）读取，灌入 Agent 专属 collection
``agent_knowledge``——**免重新编码**，直接复用 BGE-M3 dense/bge_sparse 缓存，
bm25 文档向量按 config 冻结常量现算（与 index_documents.stage_upload 同口径）。

与 annual_reports 的隔离：
- collection 名不同（agent_knowledge），schema 完全复刻 ensure_collection
  （单 segment + dense 1024 cosine + bm25 挂 Modifier.IDF + bge_sparse + 5 payload 索引）；
- point id 用 uuid5(NAMESPACE_URL, "agent:" + chunk_id)，与源库幂等空间隔离。

对 RAG 仓库零侵入：仅只读其 src/（sys.path 注入）与 data/，写入目标只有 Qdrant。
假 utils 补丁与 serve_mcp.py 同理（避免向 RAG 仓库 log/ 写文件 + 保 FlagEmbedding 导入顺序）。

用法：
    python scripts/rag/seed_agent_knowledge.py                # 默认 3 家公司全部年份
    python scripts/rag/seed_agent_knowledge.py --codes 600519 --years 2025
    python scripts/rag/seed_agent_knowledge.py --recreate     # 删旧重建
"""

from __future__ import annotations

import argparse
import logging
import sys
import types
import uuid
from collections import Counter
from pathlib import Path

RAG_ROOT = Path(r"D:\VSCodeProject\混合检索RAG")
sys.path.insert(0, str(RAG_ROOT / "src"))


def _install_console_log_patch() -> None:
    """向 sys.modules 注入纯 stdout 版 utils.Log（index_documents 导入时会调用）。"""

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

import index_documents as idx  # noqa: E402  内部保证 FlagEmbedding 最先
import numpy as np  # noqa: E402
from qdrant_client import models  # noqa: E402

logger = logging.getLogger("rag.seed")

DEFAULT_COLLECTION = "agent_knowledge"
DEFAULT_CODES = ["600519", "000001", "000651"]  # 贵州茅台/平安银行/格力电器
UPLOAD_BATCH = 256


def pick_files(codes: list[str], years: list[str] | None, chunks_dir: Path) -> list[Path]:
    """按股票代码 + 年份筛选 chunk json（文件名 <code>_<year>_<公司>_chunk.json）。"""
    files = idx.iter_chunk_files(codes, None, chunks_dir)
    if years:
        keep = set(years)
        files = [f for f in files if f.name.split("_")[1] in keep]
    return files


def seed(codes: list[str], years: list[str] | None, collection: str, recreate: bool) -> int:
    """建 collection（复刻 ensure_collection 口径）并灌种，返回入库点数。"""
    cfg = idx.load_config()
    idx.COLLECTION_NAME = collection  # ensure_collection/upsert 均读模块级变量

    client = idx.get_qdrant_client(cfg)
    try:
        if not client.collection_exists(collection) or recreate:
            idx.ensure_collection(client, recreate)
        else:
            logger.info("collection %s 已存在，增量覆盖灌种", collection)

        chunks_dir = idx.ROOT_PATH / cfg["paths"]["chunks"]
        cache_dir = idx.ROOT_PATH / cfg["paths"]["embeddings"]
        files = pick_files(codes, years, chunks_dir)
        if not files:
            raise SystemExit(f"未筛到任何 chunk 文件：codes={codes} years={years}")
        logger.info("待灌种 %d 份报告：%s", len(files), ", ".join(f.name for f in files))

        total = 0
        batch: list[models.PointStruct] = []

        def flush() -> None:
            nonlocal batch
            if not batch:
                return
            client.upsert(collection_name=collection, points=batch, wait=True)
            batch = []

        for i, f in enumerate(files, 1):
            chunks, meta = idx.load_chunks(f)
            npz_path = cache_dir / f"{f.stem}.npz"
            if not npz_path.exists():
                logger.warning("跳过 %s：缺向量缓存 %s", f.name, npz_path.name)
                continue
            with np.load(npz_path) as z:
                assert int(z["n"]) == len(chunks), f"缓存与 chunk 错位：{f}"
                dense = z["dense"]
                sp_idx, sp_val = z["sp_idx"], z["sp_val"]
                offsets = z["sp_offsets"]

            for j, c in enumerate(chunks):
                # "agent:" 前缀与 annual_reports 的幂等空间隔离
                pid = str(uuid.uuid5(idx.CHUNK_NS, "agent:" + c["chunk_id"]))
                lo, hi = int(offsets[j]), int(offsets[j + 1])
                bge_sparse = models.SparseVector(
                    indices=sp_idx[lo:hi].tolist(),
                    values=sp_val[lo:hi].astype(float).tolist(),
                )
                tokens = Counter(idx.tokenize(c["content"]))
                batch.append(
                    models.PointStruct(
                        id=pid,
                        vector={
                            idx.VECTOR_DENSE: dense[j].astype(float).tolist(),
                            idx.VECTOR_BM25: idx.bm25_sparse_vector(tokens, cfg["sparse"]),
                            idx.VECTOR_BGE_SPARSE: bge_sparse,
                        },
                        payload=idx.chunk_payload(c, meta),
                    )
                )
                if len(batch) >= UPLOAD_BATCH:
                    flush()
            total += len(chunks)
            logger.info(
                "(%d/%d) %s 灌种 %d chunks（累计 %d）", i, len(files), f.name, len(chunks), total
            )
        flush()
        have = client.count(collection, exact=True).count
        logger.info("灌种完成：%d 点入库，collection %s 现有 %d 点", total, collection, have)
        return total
    finally:
        client.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Agent 独立知识库灌种（agent_knowledge）")
    ap.add_argument(
        "--codes", nargs="*", default=DEFAULT_CODES, help="股票代码（缺省 3 家样本公司）"
    )
    ap.add_argument("--years", nargs="*", default=None, help="年份筛选（缺省全部年份）")
    ap.add_argument("--collection", default=DEFAULT_COLLECTION, help="目标 collection 名")
    ap.add_argument("--recreate", action="store_true", help="删旧 collection 重建")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    seed(args.codes, args.years, args.collection, args.recreate)


if __name__ == "__main__":
    main()
