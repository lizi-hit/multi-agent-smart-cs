"""
知识检索Agent — RAG知识库问答
负责从向量数据库中检索相关文档，结合上下文生成准确回答。
实现完整的RAG流程：Query改写 → 混合检索(向量+关键词) → 加权融合 → 重排序 → 上下文注入 → 生成回答。
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from memory.long_term import LongTermMemory
from tracing.otel_config import trace_agent_call


RAG_SYSTEM_PROMPT = """你是一个专业的知识库问答Agent，负责根据检索到的文档回答用户问题。

回答规则：
1. 严格基于检索到的文档内容回答，不要编造信息
2. 如果文档中没有相关信息，明确告知用户并建议转人工
3. 回答要简洁专业，适合客服场景
4. 对于金融产品信息，必须标注"以上信息仅供参考，具体以合同条款为准"
5. 在回答末尾标注引用的文档来源

回答格式：
- 先直接回答用户问题
- 如有必要补充相关信息
- 金融场景需添加风险提示
"""

QUERY_REWRITE_PROMPT = """请将用户的口语化问题改写为更适合向量检索的查询语句。
保留核心语义，去除口语化表达，补充专业术语。
只返回改写后的查询，不要其他内容。

用户原始问题: {query}
"""


class KnowledgeRAGAgent:
    """知识检索Agent - 实现可落地混合RAG流程"""

    def __init__(self, llm: ChatOpenAI, long_term_memory: LongTermMemory | None = None):
        self.llm = llm
        self.long_term_memory = long_term_memory or LongTermMemory()
        self.vector_weight = 0.7
        self.keyword_weight = 0.3
        self.hybrid_candidate_k = 8
        self.rerank_k = 3
        self.rrf_k = 60
        self.rrf_weight = 0.4
        self.weighted_fusion_weight = 0.6
        self.min_retrieve_confidence = 0.12
        self.max_context_docs = 3
        self.max_context_chars = 2200

    @trace_agent_call("rag_query_rewrite")
    async def rewrite_query(self, original_query: str) -> str:
        """Query改写：将口语化问题转为检索友好的查询，失败降级为原query"""
        try:
            messages = [
                HumanMessage(content=QUERY_REWRITE_PROMPT.format(query=original_query)),
            ]
            response = await self.llm.ainvoke(messages)
            rewritten = response.content.strip()
            return rewritten or original_query
        except Exception:
            return original_query

    @staticmethod
    def _tokenize_for_keyword(text: str) -> list[str]:
        # 中英文混合场景的轻量token切分：中文词块 + 英文单词/数字
        tokens = re.findall(r"[\u4e00-\u9fff]{1,8}|[a-zA-Z0-9_]+", text.lower())
        return [t for t in tokens if t.strip()]

    @staticmethod
    def _deduplicate_documents(documents: list[dict]) -> list[dict]:
        seen = set()
        results = []
        for doc in documents:
            key = f"{doc.get('source', '')}:{doc.get('content', '')[:120]}"
            if key in seen:
                continue
            seen.add(key)
            results.append(doc)
        return results

    @staticmethod
    def _normalize_scores(items: list[dict], key: str) -> None:
        if not items:
            return
        values = [float(i.get(key, 0.0)) for i in items]
        min_v = min(values)
        max_v = max(values)
        if math.isclose(min_v, max_v):
            for i in items:
                i[f"{key}_norm"] = 1.0 if max_v > 0 else 0.0
            return
        gap = max_v - min_v
        for i in items:
            i[f"{key}_norm"] = (float(i.get(key, 0.0)) - min_v) / gap

    @trace_agent_call("rag_retrieve")
    async def retrieve_vector_documents(self, query: str, top_k: int = 8) -> list[dict]:
        """向量相似度检索"""
        docs = self.long_term_memory.search(query, top_k=top_k)
        for d in docs:
            d["vector_score"] = float(d.get("score", 0.0))
        return docs

    @trace_agent_call("rag_keyword_retrieve")
    async def retrieve_keyword_documents(self, query: str, top_k: int = 8) -> list[dict]:
        """关键词检索（BM25-like），作为混合检索通道"""
        corpus = self.long_term_memory.list_documents()
        if not corpus:
            return []

        query_tokens = self._tokenize_for_keyword(query)
        if not query_tokens:
            return []

        # 预计算文档长度和DF，做轻量BM25-like打分
        tokenized_docs = []
        df: dict[str, int] = {}
        for doc in corpus:
            tokens = self._tokenize_for_keyword(str(doc.get("content", "")))
            tokenized_docs.append(tokens)
            unique_tokens = set(tokens)
            for t in unique_tokens:
                df[t] = df.get(t, 0) + 1

        doc_count = len(corpus)
        avg_doc_len = sum(len(toks) for toks in tokenized_docs) / max(doc_count, 1)
        k1 = 1.5
        b = 0.75

        scored = []
        for idx, doc in enumerate(corpus):
            content = str(doc.get("content", ""))
            content_lower = content.lower()
            tokens = tokenized_docs[idx]
            doc_len = max(len(tokens), 1)
            tf_map: dict[str, int] = {}
            for t in tokens:
                tf_map[t] = tf_map.get(t, 0) + 1

            bm25_score = 0.0
            for token in query_tokens:
                tf = tf_map.get(token, 0)
                if tf <= 0:
                    continue
                token_df = df.get(token, 0)
                idf = math.log(1 + (doc_count - token_df + 0.5) / (token_df + 0.5))
                denom = tf + k1 * (1 - b + b * (doc_len / max(avg_doc_len, 1)))
                bm25_score += idf * ((tf * (k1 + 1)) / max(denom, 1e-6))

            phrase_bonus = 0.0
            if query.lower() in content_lower:
                phrase_bonus = 2.0

            keyword_score = bm25_score + phrase_bonus
            if keyword_score <= 0:
                continue

            item = doc.copy()
            item["keyword_score"] = keyword_score
            scored.append(item)

        scored.sort(key=lambda x: x.get("keyword_score", 0.0), reverse=True)
        return scored[:top_k]

    @trace_agent_call("rag_rrf_fuse")
    async def _rrf_fuse(self, vector_docs: list[dict], keyword_docs: list[dict]) -> dict[str, float]:
        """
        Reciprocal Rank Fusion:
        score(d) = 1/(k + rank_vector) + 1/(k + rank_keyword)
        """
        scores: dict[str, float] = {}

        def doc_key(doc: dict) -> str:
            return str(doc.get("id") or f"{doc.get('source', '')}:{hash(doc.get('content', ''))}")

        for rank, doc in enumerate(vector_docs, 1):
            key = doc_key(doc)
            scores[key] = scores.get(key, 0.0) + 1.0 / (self.rrf_k + rank)

        for rank, doc in enumerate(keyword_docs, 1):
            key = doc_key(doc)
            scores[key] = scores.get(key, 0.0) + 1.0 / (self.rrf_k + rank)

        return scores

    @trace_agent_call("rag_hybrid_fuse")
    async def hybrid_retrieve(self, query: str, top_k: int = 8) -> list[dict]:
        """混合检索：向量检索 + BM25-like关键词检索 + 加权融合 + RRF融合"""
        vector_docs = await self.retrieve_vector_documents(query, top_k=top_k)
        keyword_docs = await self.retrieve_keyword_documents(query, top_k=top_k)

        # 先各自归一化，再做加权融合
        self._normalize_scores(vector_docs, "vector_score")
        self._normalize_scores(keyword_docs, "keyword_score")
        rrf_scores = await self._rrf_fuse(vector_docs, keyword_docs)

        merged: dict[str, dict] = {}

        def doc_key(doc: dict) -> str:
            return str(doc.get("id") or f"{doc.get('source', '')}:{hash(doc.get('content', ''))}")

        for doc in vector_docs:
            key = doc_key(doc)
            merged[key] = {**doc}
            merged[key].setdefault("keyword_score_norm", 0.0)
            merged[key].setdefault("vector_score_norm", doc.get("vector_score_norm", 0.0))

        for doc in keyword_docs:
            key = doc_key(doc)
            if key not in merged:
                merged[key] = {**doc}
                merged[key].setdefault("vector_score_norm", 0.0)
            else:
                merged[key]["keyword_score"] = doc.get("keyword_score", 0.0)
                merged[key]["keyword_score_norm"] = doc.get("keyword_score_norm", 0.0)

        fused = []
        for item in merged.values():
            v_norm = float(item.get("vector_score_norm", 0.0))
            k_norm = float(item.get("keyword_score_norm", 0.0))
            weighted_score = self.vector_weight * v_norm + self.keyword_weight * k_norm
            rrf_score = float(rrf_scores.get(doc_key(item), 0.0))
            item["weighted_score"] = weighted_score
            item["rrf_score"] = rrf_score
            item["fused_score"] = (
                self.weighted_fusion_weight * weighted_score
                + self.rrf_weight * rrf_score
            )
            fused.append(item)

        fused.sort(key=lambda x: x.get("fused_score", 0.0), reverse=True)
        return self._deduplicate_documents(fused[:top_k])

    @trace_agent_call("rag_rerank")
    async def rerank_documents(
        self, query: str, documents: list[dict], top_k: int = 3
    ) -> list[dict]:
        """对检索结果重排序，提升相关性"""
        if not documents:
            return []

        doc_summaries = "\n".join(
            f"[{i}] {doc.get('content', '')[:200]}"
            for i, doc in enumerate(documents)
        )

        messages = [
            SystemMessage(content="你是一个文档相关性排序专家。"),
            HumanMessage(content=(
                f"用户查询: {query}\n\n"
                f"候选文档:\n{doc_summaries}\n\n"
                f"请返回最相关的{top_k}个文档的索引号，用逗号分隔，如: 0,2,4"
            )),
        ]

        response = await self.llm.ainvoke(messages)

        try:
            raw = response.content.strip()
            if raw.startswith("["):
                parsed = json.loads(raw)
                indices = [int(i) for i in parsed]
            else:
                indices = [int(i) for i in re.findall(r"\d+", raw)]
            reranked = [documents[i] for i in indices if i < len(documents)]
        except (ValueError, IndexError):
            reranked = documents[:top_k]

        if not reranked:
            return documents[:top_k]
        return reranked[:top_k]

    @trace_agent_call("rag_confidence_gate")
    async def apply_confidence_gate(self, documents: list[dict]) -> tuple[list[dict], bool, float]:
        """
        低置信度门控：
        - 若融合分数过低，主动拒答并建议转人工，降低幻觉风险
        """
        if not documents:
            return [], True, 0.0

        scores = [float(d.get("fused_score", 0.0)) for d in documents[: self.rerank_k]]
        confidence = sum(scores) / max(len(scores), 1)
        blocked = confidence < self.min_retrieve_confidence
        return ([] if blocked else documents), blocked, confidence

    @trace_agent_call("rag_generate")
    async def generate_answer(self, query: str, context_docs: list[dict]) -> str:
        """基于检索文档生成回答"""
        if not context_docs:
            return "抱歉，知识库中暂未找到与您问题相关的信息。建议您联系人工客服获取帮助。"

        selected_docs = []
        used_chars = 0
        for doc in context_docs:
            content = str(doc.get("content", ""))
            if not content:
                continue
            if len(selected_docs) >= self.max_context_docs:
                break
            if used_chars + len(content) > self.max_context_chars:
                break
            selected_docs.append(doc)
            used_chars += len(content)

        if not selected_docs:
            selected_docs = context_docs[:1]

        citation_lines = []
        for idx, doc in enumerate(selected_docs, 1):
            citation_lines.append(f"[{idx}] {doc.get('source', '未知')}#{doc.get('id', 'na')}")

        context = "\n\n---\n\n".join(
            f"来源: {doc.get('source', '未知')}\n内容: {doc.get('content', '')}"
            for doc in selected_docs
        )

        messages = [
            SystemMessage(content=RAG_SYSTEM_PROMPT),
            HumanMessage(content=(
                f"用户问题: {query}\n\n"
                f"检索到的参考文档:\n{context}\n\n"
                f"可引用来源编号:\n" + "\n".join(citation_lines) + "\n\n"
                f"请在回答结尾附上引用编号，例如：[1][2]。"
            )),
        ]

        response = await self.llm.ainvoke(messages)
        return response.content

    @trace_agent_call("knowledge_rag_process")
    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        """
        完整RAG流程（作为Graph节点）：
        1. Query改写
        2. 混合检索（向量+关键词）
        3. 加权融合
        4. 重排序
        5. 生成回答
        """
        messages = state.get("messages", [])
        if not messages:
            return state

        original_query = messages[-1].content

        rewritten_query = await self.rewrite_query(original_query)

        raw_docs = await self.hybrid_retrieve(rewritten_query, top_k=self.hybrid_candidate_k)

        reranked_docs = await self.rerank_documents(rewritten_query, raw_docs, top_k=self.rerank_k)

        gated_docs, blocked, confidence = await self.apply_confidence_gate(reranked_docs)
        answer = await self.generate_answer(original_query, gated_docs)
        if blocked:
            answer = (
                "抱歉，当前检索到的信息相关性不足，为避免误导已转人工客服跟进。"
                "请稍后由人工为您提供准确答复。"
            )

        return {
            **state,
            "sub_results": {
                **state.get("sub_results", {}),
                "knowledge_rag": answer,
                "knowledge_rag_debug": {
                    "rewritten_query": rewritten_query,
                    "retrieved_count": len(raw_docs),
                    "reranked_count": len(reranked_docs),
                    "confidence": confidence,
                    "confidence_blocked": blocked,
                    "top_sources": [d.get("source", "未知") for d in reranked_docs],
                },
            },
        }
