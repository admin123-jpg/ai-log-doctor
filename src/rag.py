"""
RAG 检索模块（Retrieval-Augmented Generation，检索增强生成）
==========================================================
作用：让 AI 回答问题时能「翻资料」，而不是凭空瞎编。

RAG 分两步：
  1. 建库（离线）：把 knowledge/*.md 切成小段 → 每段转成向量 → 存进向量库
  2. 检索（在线）：把异常日志转成向量 → 在库里找最相似的几段 → 作为参考资料给 AI

为什么需要它？
  大模型只知道训练时见过的通用知识，不知道你公司的具体情况。
  把运维手册喂给它，它就能给出贴合实际的建议，还能告诉你「依据是哪一条」。

本模块的两个「可降级」设计（重要）：
  - 向量库后端：优先 Chroma（真实向量数据库），装不上就自动用内存版（纯 Python）
  - 向量化方式：优先调 API（真实 embedding），没配 Key 就用本地算法（字符特征）
  这样保证你在任何环境下都能跑通，不会因为缺依赖或没 Key 而卡住。
"""

import hashlib
import json
import math
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import requests

from .config import Config


# 稠密向量维度：本地向量化时用「特征哈希」把稀疏词频折叠到这么多维
# 为什么是 4096？知识库只有几十个片段、词表也就几千个词。
# 维度越低冲突越多（会把不相干的词算成相似），维度太高只会浪费内存。
# 实测：1024 维时有 2/5 的检索结果被哈希冲突挤错，4096 维 + 带符号哈希后 5/5 一致。
DENSE_DIM = 4096


def to_dense(vec, dim: int = DENSE_DIM) -> list:
    """
    把稀疏词频字典转成固定维度的稠密向量（带符号的特征哈希 / hashing trick）。

    为什么需要这个？
      本地的 SimpleEmbedder 产出的是稀疏字典（{'connection': 1, '连': 1}），
      内存向量库能直接算余弦相似度，但 **Chroma 只接受稠密 float 列表**。

    原理：
      给每个词算一个哈希值 —— 一部分位决定「落在哪个桶」，另一位决定「正负号」，
      把词频按符号累加到对应桶里。

    为什么必须带符号？
      如果全是正数，两个无关的词撞进同一个桶会「凭空增加相似度」，
      把本来正确的检索结果挤掉（实测 5 个查询里有 2 个被挤错）。
      加上正负号后，冲突项正负相消，期望为 0，效果与稀疏版本基本一致。

    参数：
      vec - 稀疏字典 {词: 词频}，或已经是稠密列表（原样返回）
      dim - 目标维度
    """
    if isinstance(vec, list):
        return vec                      # 本来就稠密，不用转
    out = [0.0] * dim
    for term, weight in vec.items():
        h = hashlib.md5(str(term).encode("utf-8")).digest()
        idx = int.from_bytes(h[:4], "big") % dim     # 前 4 字节 → 桶号
        sign = 1.0 if (h[4] & 1) else -1.0           # 第 5 字节最低位 → 正负号
        out[idx] += sign * float(weight)
    return out



# ============================ 数据结构 ============================

@dataclass
class Chunk:
    """知识库里的一个片段"""
    cid: str        # 片段唯一 ID，如 docker-errors.md#3
    source: str     # 来自哪个文件
    title: str      # 所属小节标题
    text: str       # 片段正文


# ============================ 1. 文档加载与切片 ============================

def load_knowledge_docs(knowledge_dir: Optional[Path] = None) -> list:
    """
    读取 knowledge 目录下所有 .md 文件。
    返回 [(文件名, 文件内容), ...]
    """
    kdir = Path(knowledge_dir) if knowledge_dir else Config.KNOWLEDGE_DIR
    if not kdir.exists():
        return []
    docs = []
    for path in sorted(kdir.glob("*.md")):
        # 跳过历史案例文件（它由系统运行时生成，单独处理）
        content = path.read_text(encoding="utf-8", errors="replace")
        docs.append((path.name, content))
    return docs


def split_markdown(filename: str, content: str) -> list:
    """
    把 Markdown 按二级标题（## ）切成片段。

    为什么按 ## 切？
      我们的知识库每个 ## 小节就是「一个错误类型」，语义完整。
      检索时整段返回给 AI，上下文才完整，不会半句话。
    """
    chunks = []
    # 按行扫描，遇到 ## 就开启一个新片段
    current_title = "概述"
    current_lines = []
    index = 0

    def flush():
        """把累积的行保存成一个片段"""
        nonlocal current_lines, index
        text = "\n".join(current_lines).strip()
        if len(text) >= 30:  # 太短的片段没意义，丢弃
            index += 1
            chunks.append(Chunk(
                cid=f"{filename}#{index}",
                source=filename,
                title=current_title,
                text=text,
            ))
        current_lines = []

    for line in content.splitlines():
        if re.match(r"^##\s+", line):
            flush()
            current_title = line.lstrip("#").strip()
        else:
            current_lines.append(line)
    flush()

    return chunks


def build_chunks(knowledge_dir: Optional[Path] = None) -> list:
    """加载所有文档并切片，返回 Chunk 列表"""
    chunks = []
    for filename, content in load_knowledge_docs(knowledge_dir):
        chunks.extend(split_markdown(filename, content))
    return chunks


# ============================ 2. 向量化（Embedding）============================

def _vector_norm(vec) -> float:
    """计算向量模长（支持 dict 稀疏向量和 list 稠密向量）"""
    values = vec.values() if isinstance(vec, dict) else vec
    return math.sqrt(sum(v * v for v in values))


def cosine_similarity(a, b) -> float:
    """
    余弦相似度：值域 -1~1，越接近 1 表示越相似。
    简单理解：把两段文字转成两个向量，看它们的「方向」有多接近。
    """
    if isinstance(a, dict) and isinstance(b, dict):
        common = set(a) & set(b)
        if not common:
            return 0.0
        num = sum(a[k] * b[k] for k in common)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return 0.0
        num = sum(x * y for x, y in zip(a, b))
    else:
        return 0.0

    na, nb = _vector_norm(a), _vector_norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return num / (na * nb)


class SimpleEmbedder:
    """
    本地向量化（零依赖，不需要 API Key）

    原理：统计文本里的「特征词」出现次数，形成一个词频向量。
      - 英文/数字：按单词切分，如 connection、refused、502
      - 中文：按单字 + 相邻两字（bigram）切分，如「连接」「接失」「失败」
    然后用余弦相似度比大小。虽然比不上专业模型，但对关键词型日志匹配效果不错。
    """

    name = "simple"

    def embed(self, text: str) -> dict:
        text = text.lower()
        words = re.findall(r"[a-z0-9_]+", text)                 # 英文单词/数字
        chinese = re.findall(r"[\u4e00-\u9fff]", text)          # 中文单字
        grams = [chinese[i] + chinese[i + 1] for i in range(len(chinese) - 1)]

        vec = {}
        for token in words + chinese + grams:
            vec[token] = vec.get(token, 0) + 1
        return vec


class APIEmbedder:
    """
    调用在线 embedding 接口做向量化（效果更好，需要 API Key）

    兼容 OpenAI 格式的 /embeddings 接口，DeepSeek、通义千问等都能用。
    """

    name = "api"

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    def embed(self, text: str) -> list:
        url = f"{self.base_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {"model": self.model, "input": text[:2000]}
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data["data"][0]["embedding"]


def make_embedder():
    """
    工厂函数：根据配置自动选择向量化方式。
    先尝试 API，失败或没配 Key 就降级到本地算法。
    """
    if Config.EMBED_API_KEY:
        try:
            e = APIEmbedder(Config.EMBED_API_KEY, Config.EMBED_BASE_URL, Config.EMBED_MODEL)
            e.embed("测试连通性")  # 先试一次，确认 Key 有效
            return e
        except Exception as err:
            print(f"[RAG] Embedding API 不可用（{err}），已降级为本地算法")
    return SimpleEmbedder()


# ============================ 3. 向量存储 ============================

class MemoryStore:
    """
    内存向量库（纯 Python 实现，零依赖）

    原理：把所有片段的向量放在一个列表里，检索时逐个算余弦相似度，取最高的几个。
    数据量小（几百条）时速度完全够用，而且能持久化到 JSON 文件，不用每次重建。
    """

    name = "memory"

    def __init__(self, embedder):
        self.embedder = embedder
        self.chunks: list = []
        self.vectors: list = []

    def add(self, chunks: list):
        """批量添加片段（会逐个向量化）"""
        for chunk in chunks:
            self.chunks.append(chunk)
            self.vectors.append(self.embedder.embed(chunk.title + "\n" + chunk.text))

    def search(self, query: str, top_k: int = 3) -> list:
        """检索最相似的 top_k 个片段，返回 [(chunk, 相似度), ...]"""
        if not self.chunks:
            return []
        qv = self.embedder.embed(query)
        scored = [
            (chunk, cosine_similarity(qv, vec))
            for chunk, vec in zip(self.chunks, self.vectors)
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def save(self, path: Path):
        """持久化到 JSON 文件"""
        Config.ensure_dirs()
        data = {
            "chunks": [asdict(c) for c in self.chunks],
            "vectors": self.vectors,
        }
        Path(path).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    def load(self, path: Path) -> bool:
        """从 JSON 文件恢复。成功返回 True，文件不存在或损坏返回 False"""
        p = Path(path)
        if not p.exists():
            return False
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            self.chunks = [Chunk(**c) for c in data["chunks"]]
            self.vectors = data["vectors"]
            return True
        except Exception:
            return False


class ChromaStore:
    """
    Chroma 向量数据库（真实的生产级方案）

    需要 pip install chromadb。它内部用 HNSW 算法做近似最近邻检索，
    数据量达到几万条时依然很快，还能持久化到磁盘。
    """

    name = "chroma"

    def __init__(self, embedder, persist_dir: str, collection: str):
        import chromadb
        from chromadb.config import Settings

        self.embedder = embedder
        self.client = chromadb.PersistentClient(
            path=persist_dir, settings=Settings(anonymized_telemetry=False)
        )
        self.collection = self.client.get_or_create_collection(
            name=collection,
            metadata={"hnsw:space": "cosine"},
        )

    def add(self, chunks: list):
        self.collection.add(
            ids=[c.cid for c in chunks],
            documents=[c.text for c in chunks],
            metadatas=[{"source": c.source, "title": c.title} for c in chunks],
            # 必须转成稠密向量：Chroma 不接受稀疏 dict，
            # 本地 SimpleEmbedder 在没配 API Key 时产出的正是 dict。
            # 少了 to_dense() 这一步就会报
            # "Expected each embedding in the embeddings to be a list, got ['dict']"
            embeddings=[to_dense(self.embedder.embed(c.title + "\n" + c.text)) for c in chunks],
        )

    def search(self, query: str, top_k: int = 3) -> list:
        total = self.collection.count()
        if total == 0:
            return []
        top_k = min(top_k, total)
        # 查询向量也要用同一套转换，否则和库里的向量维度对不上
        qv = to_dense(self.embedder.embed(query))
        res = self.collection.query(
            query_embeddings=[qv], n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )
        out = []
        for i in range(len(res["ids"][0])):
            meta = res["metadatas"][0][i]
            # chroma 返回的是 cosine 距离，相似度 = 1 - 距离
            score = 1 - float(res["distances"][0][i])
            out.append((Chunk(
                cid=res["ids"][0][i],
                source=meta.get("source", ""),
                title=meta.get("title", ""),
                text=res["documents"][0][i],
            ), score))
        return out


def _chroma_available() -> bool:
    """检测 chromadb 是否已安装"""
    try:
        import chromadb  # noqa: F401
        return True
    except ImportError:
        return False


# ============================ 4. 知识库门面（对外统一入口）============================

class KnowledgeBase:
    """
    知识库门面类：把「加载 → 切片 → 向量化 → 存储 → 检索」封装成两个方法。

    用法：
        kb = KnowledgeBase()
        kb.build()                      # 建库（第一次或知识库更新后调用）
        refs = kb.retrieve("Connection refused", top_k=3)
    """

    def __init__(self, backend: Optional[str] = None):
        backend = (backend or Config.VECTOR_BACKEND or "auto").lower()
        self.embedder = make_embedder()
        self.cache_path = Config.DATA_DIR / "kb_memory.json"
        self.chroma_dir = Config.CHROMA_DIR

        use_chroma = False
        if backend in ("chroma", "auto"):
            use_chroma = _chroma_available()

        if backend == "chroma" and not use_chroma:
            print("[RAG] 未安装 chromadb，已自动降级为内存向量库")

        if use_chroma:
            try:
                self.store = ChromaStore(
                    self.embedder, self.chroma_dir, Config.CHROMA_COLLECTION
                )
            except Exception as err:
                print(f"[RAG] Chroma 初始化失败（{err}），降级为内存向量库")
                self.store = MemoryStore(self.embedder)
        else:
            self.store = MemoryStore(self.embedder)

    # ---------- 建库 ----------

    def build(self, force: bool = False) -> int:
        """
        构建知识库。返回片段数量。

        force=True 表示强制重建（改了知识库文档后用它）。
        """
        Config.ensure_dirs()

        # 内存库支持从缓存恢复，避免每次启动都重新向量化
        if isinstance(self.store, MemoryStore):
            if force:
                # 强制重建时先清空，否则 add() 是追加，会导致片段重复
                self.store.chunks.clear()
                self.store.vectors.clear()
            elif self.store.load(self.cache_path):
                return len(self.store.chunks)

        chunks = build_chunks()
        if not chunks:
            print(f"[RAG] 警告：{Config.KNOWLEDGE_DIR} 下没找到知识库文档")
            return 0

        # Chroma 重建前先清空旧数据
        if isinstance(self.store, ChromaStore) and force:
            try:
                self.store.client.delete_collection(Config.CHROMA_COLLECTION)
                self.store.collection = self.store.client.get_or_create_collection(
                    name=Config.CHROMA_COLLECTION,
                    metadata={"hnsw:space": "cosine"},
                )
            except Exception:
                pass

        self.store.add(chunks)

        if isinstance(self.store, MemoryStore):
            self.store.save(self.cache_path)

        return len(chunks)

    # ---------- 检索 ----------

    def retrieve(self, query: str, top_k: Optional[int] = None) -> list:
        """
        检索与 query 最相关的知识片段。

        返回格式（可直接塞进 Prompt，也可直接返给前端展示）：
        [
          {"source": "docker-errors.md", "title": "容器间 Connection refused",
           "text": "...", "score": 0.82},
          ...
        ]
        """
        top_k = top_k or Config.TOP_K
        if isinstance(self.store, MemoryStore) and not self.store.chunks:
            self.build()

        results = self.store.search(query, top_k)
        return [
            {
                "source": chunk.source,
                "title": chunk.title,
                "text": chunk.text[:600],
                "score": round(float(score), 4),
            }
            for chunk, score in results
        ]

    # ---------- 状态信息 ----------

    def info(self) -> dict:
        """返回知识库状态，用于 /docs 调试和前端展示"""
        if isinstance(self.store, MemoryStore):
            count = len(self.store.chunks)
        else:
            try:
                count = self.store.collection.count()
            except Exception:
                count = 0
        return {
            "backend": self.store.name,
            "embedder": self.embedder.name,
            "chunk_count": count,
        }
