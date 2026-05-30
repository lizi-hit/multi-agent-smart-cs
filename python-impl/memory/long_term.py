"""
长期记忆 — 基于向量数据库的持久化记忆
存储用户画像、历史工单、知识库文档等需要持久化的信息。
支持语义相似度检索，用于RAG知识检索Agent。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import numpy as np
from langchain_openai import OpenAIEmbeddings

try:
    import faiss
except ImportError:
    faiss = None


class LongTermMemory:
    """
    长期记忆：基于FAISS的向量检索。

    特点：
    - 向量化存储，支持语义相似度检索
    - 持久化到磁盘，跨会话保持
    - 支持增量更新和批量导入
    - 生产环境可切换为Milvus/Pinecone

    文档分块策略：
    - 固定长度分块 (512 tokens) + 重叠窗口 (128 tokens)
    - 按段落自然分割优先
    """

    def __init__(
        self,
        index_path: str = "./vector_store/faiss_index",
        embedding_dim: int = 1536,
        vector_store_type: str = "faiss",
        embedding_provider: str = "openai",
        embedding_model: str = "text-embedding-3-small",
    ):
        self.index_path = Path(index_path)
        self.embedding_dim = embedding_dim
        self.vector_store_type = vector_store_type.lower()
        self.embedding_provider = embedding_provider.lower()
        self._documents: list[dict[str, Any]] = []
        self._index = None
        self._embeddings_client = None
        self._embedding_cache: dict[str, np.ndarray] = {}

        if self.embedding_provider == "openai":
            try:
                self._embeddings_client = OpenAIEmbeddings(
                    model=embedding_model,
                    api_key=os.getenv("OPENAI_API_KEY"),
                    base_url=os.getenv("OPENAI_BASE_URL"),
                )
            except TypeError:
                # 兼容不同版本参数命名
                self._embeddings_client = OpenAIEmbeddings(
                    model=embedding_model,
                    api_key=os.getenv("OPENAI_API_KEY"),
                    openai_api_base=os.getenv("OPENAI_BASE_URL"),
                )
        self._init_index()

    def _init_index(self):
        """初始化FAISS索引"""
        if self.vector_store_type != "faiss":
            self._index = None
            return

        if faiss is None:
            self._index = None
            return

        metadata_path = self.index_path.with_suffix(".meta.json")
        if self.index_path.exists():
            try:
                self._index = faiss.read_index(str(self.index_path))
                if metadata_path.exists():
                    with open(metadata_path, "r", encoding="utf-8") as f:
                        self._documents = json.load(f)
            except Exception:
                self._index = faiss.IndexFlatIP(self.embedding_dim)
        else:
            self._index = faiss.IndexFlatIP(self.embedding_dim)

    def _simple_embedding(self, text: str) -> np.ndarray:
        """
        简易文本嵌入（演示用）。
        生产环境应替换为 OpenAI Embedding API 或本地模型。
        """
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        np.random.seed(int(text_hash[:8], 16) % (2**32))
        vec = np.random.randn(self.embedding_dim).astype(np.float32)
        vec /= np.linalg.norm(vec)
        return vec

    def _get_embedding(self, text: str) -> np.ndarray:
        if text in self._embedding_cache:
            return self._embedding_cache[text]

        vec = None
        if self.embedding_provider == "openai" and self._embeddings_client is not None:
            try:
                values = self._embeddings_client.embed_query(text)
                vec = np.array(values, dtype=np.float32)
                norm = np.linalg.norm(vec)
                if norm > 0:
                    vec = vec / norm
            except Exception:
                vec = None

        if vec is None:
            vec = self._simple_embedding(text)

        self._embedding_cache[text] = vec
        return vec

    def add_document(self, content: str, source: str = "", metadata: dict | None = None) -> str:
        """添加文档到向量库"""
        doc_id = hashlib.md5(content.encode()).hexdigest()[:12]

        doc = {
            "id": doc_id,
            "content": content,
            "source": source,
            "metadata": metadata or {},
        }
        self._documents.append(doc)

        if self._index is not None:
            embedding = self._get_embedding(content)
            self._index.add(embedding.reshape(1, -1))

        return doc_id

    def add_documents_batch(self, documents: list[dict]) -> list[str]:
        """批量添加文档"""
        doc_ids = []
        for doc in documents:
            doc_id = self.add_document(
                content=doc.get("content", ""),
                source=doc.get("source", ""),
                metadata=doc.get("metadata", {}),
            )
            doc_ids.append(doc_id)
        return doc_ids

    def list_documents(self) -> list[dict]:
        """返回当前知识库文档副本，供关键词检索/评估使用"""
        return [doc.copy() for doc in self._documents]

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """语义相似度检索"""
        if self._index is None or not self._documents:
            return self._fallback_search(query, top_k)

        query_vec = self._get_embedding(query).reshape(1, -1)
        scores, indices = self._index.search(query_vec, min(top_k, len(self._documents)))

        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or idx >= len(self._documents):
                continue
            doc = self._documents[idx].copy()
            doc["score"] = float(score)
            results.append(doc)

        return results

    def _fallback_search(self, query: str, top_k: int) -> list[dict]:
        """
        当FAISS不可用或切换到memory时，采用内存向量检索 + 关键词兜底。
        """
        if not self._documents:
            return []

        query_vec = self._get_embedding(query)
        scored = []
        query_terms = set(query.lower().split())

        for doc in self._documents:
            doc_vec = self._get_embedding(doc["content"])
            vector_score = float(np.dot(query_vec, doc_vec))
            keyword_score = sum(1 for term in query_terms if term in doc["content"].lower())
            total_score = vector_score * 3 + keyword_score
            scored.append((total_score, doc))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for score, doc in scored[:top_k]:
            copied = doc.copy()
            copied["score"] = float(score)
            results.append(copied)
        return results

    def save(self):
        """持久化索引到磁盘"""
        self.index_path.parent.mkdir(parents=True, exist_ok=True)

        if self._index is not None:
            faiss.write_index(self._index, str(self.index_path))

        metadata_path = self.index_path.with_suffix(".meta.json")
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(self._documents, f, ensure_ascii=False, indent=2)

    def load_knowledge_base(self, kb_dir: str) -> int:
        """从目录批量加载知识库文档"""
        kb_path = Path(kb_dir)
        if not kb_path.exists():
            return 0

        count = 0
        for file_path in kb_path.glob("**/*.txt"):
            content = file_path.read_text(encoding="utf-8")
            chunks = self._chunk_text(content)
            for chunk in chunks:
                self.add_document(
                    content=chunk,
                    source=str(file_path.name),
                    metadata={"file": str(file_path)},
                )
                count += 1

        return count

    @staticmethod
    def _chunk_text(text: str, chunk_size: int = 512, overlap: int = 128) -> list[str]:
        """
        文本分块：固定长度 + 重叠窗口。
        优先按段落分割，段落过长则按句子分割。
        """
        paragraphs = text.split("\n\n")
        chunks = []
        current_chunk = ""

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            if len(current_chunk) + len(para) <= chunk_size:
                current_chunk += para + "\n\n"
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                    overlap_text = current_chunk[-overlap:] if len(current_chunk) > overlap else current_chunk
                    current_chunk = overlap_text + para + "\n\n"
                else:
                    sentences = para.replace("。", "。\n").replace(".", ".\n").split("\n")
                    for sentence in sentences:
                        sentence = sentence.strip()
                        if not sentence:
                            continue
                        if len(current_chunk) + len(sentence) <= chunk_size:
                            current_chunk += sentence
                        else:
                            if current_chunk:
                                chunks.append(current_chunk.strip())
                            current_chunk = sentence

        if current_chunk.strip():
            chunks.append(current_chunk.strip())

        return chunks if chunks else [text[:chunk_size]]
