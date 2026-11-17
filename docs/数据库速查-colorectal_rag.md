# colorectal_rag 库速查（Navicat 用）

> 连接：`127.0.0.1:5432 / colorectal_rag / rag`
> 10 张表，核心包括 `kb_document`（文献）、`kb_chunk`（文本块+向量）、`kb_case`（病例）和 `kb_image`（影像）。
> 知识图谱**不在这个库**，在 Neo4j（`127.0.0.1:7687`）。

---

## 一、表的分组与规模

| 分组 | 表 | 行数 | 大小 | 作用 |
|---|---|---|---|---|
| **知识库核心** | `kb_document` | 109 | 5.5 MB | 文献元数据（标题/期刊/年份/DOI/证据等级/分期/语种/是否上架） |
| | `kb_chunk` | 3,772 | **134 MB** | 文本块正文 + 1024 维向量 + bigram；体积主要在向量 |
| **影像** | `kb_image` | 3 | 152 KB | 影像文件记录、元数据、VLM 描述与综合解读快照 |
| | `kb_case` | 以数据库实际记录为准 | — | 公开脱敏典型病例、文本相似检索 |
| **系统** | `sys_user` | 2 | 80 KB | 用户（admin / doctor01） |
| | `sys_config` | 15 | 80 KB | 运行参数 |
| | `sys_audit_log` | 44 | 176 KB | 操作审计 |
| **运行日志** | `sys_qa_log` | 18 | 176 KB | 每次问答的落库记录（问题/答案摘要/引用数/耗时） |
| | `sys_qa_feedback` | 1 | 96 KB | 用户反馈（点赞/踩） |
| | `kb_update_batch` | 3 | 96 KB | 入库批次 |

---

## 二、三张核心表的字段要点

### kb_document（1 行 = 1 篇文献）

关键列：
- 题录：`title` `title_en` `authors` `journal` `year` `doi` `pmid` `pages`
- 分类：`language`（chi/en）、`doc_type`、`evidence_level`（guideline/review/…）、`phase`（preop/intraop/postop/general）
- 状态：`is_active`（**软删除，下架不物理删，保住历史引用链**）、`status`、`chunk_count`
- 问答应答时取的就是这里的题录信息

### kb_chunk（1 行 = 1 个文本块，N 个块属于 1 篇文献）

| 列 | 说明 |
|---|---|
| `document_id` | 外键 → `kb_document.id` |
| `content` | 块正文 |
| `embedding` | `vector(1024)`，BGE-M3 产出 |
| `bigrams` | `text[]` 中文 2-gram，供 GIN 倒排 |
| `section` `page_start` `page_end` `char_start` `char_end` | **溯源定位**：引用能显示"第几页、哪一节"靠这些 |
| `chunk_index` | 文献内块序号 |

索引：
```sql
ix_chunk_embedding_hnsw  USING hnsw (embedding vector_cosine_ops)  -- 语义检索
ix_chunk_bigrams         USING gin  (bigrams)                       -- 关键词检索
ix_chunk_phase_evid      USING btree(phase, evidence_level)         -- 元数据过滤
```

### kb_image（1 行 = 1 张影像）

- `file_hash`（SHA256 唯一，重复上传直接复用）
- 元数据：`title` `surgery_type` `anatomy_site` `phase` `source` `is_public`
- `vlm_analysis`：视觉模型的**客观描述**（缓存，避免重复花钱）
- `vlm_interpretation`：**综合解读快照 JSONB**（含引用与耗时，刷新后直接渲染）
- `feature_vector`：512 维 RGB 颜色直方图基线，上传时生成，用于相似影像检索；不表示医学语义，可用 `python -m scripts.backfill_image_features --apply` 为历史影像补算；后续可替换为 CLIP 类视觉编码器

### kb_case（1 行 = 1 个公开脱敏病例）

- 病例编号、病史、检查所见、诊断、术式、手术记录、预后、并发症、影像 ID 和标签
- `embedding`：由上述文本生成的语义向量，用于 `/api/cases/search` 相似病例匹配
- `is_public`：只有管理员审核发布后才会出现在公开列表和影像相关病例推荐中
- `image_ids`：关联的公开影像 ID 列表

---

## 三、按顺序跑这 5 条，十分钟就懂

### 1）库的构成：有多少文献、多少块、语种怎么分

```sql
SELECT language,
       count(*) AS 文献数,
       sum(chunk_count) AS 块数
FROM kb_document
WHERE is_active
GROUP BY language ORDER BY 文献数 DESC;
```

### 2）随便挑一段看"块"长什么样

```sql
SELECT d.title, c.chunk_index, c.section, c.page_start,
       c.char_count, left(c.content, 200) AS 正文开头
FROM kb_chunk c JOIN kb_document d ON d.id = c.document_id
ORDER BY c.id LIMIT 1;
```

### 3）验证向量是真的（自己和自己相似度为 1）

```sql
WITH probe AS (
  SELECT embedding FROM kb_chunk WHERE embedding IS NOT NULL ORDER BY id LIMIT 1
)
SELECT c.chunk_index,
       round((1 - (c.embedding <=> p.embedding))::numeric, 4) AS 余弦相似度,
       left(replace(c.content, E'\n', ' '), 40) AS 片段
FROM kb_chunk c, probe p
WHERE c.embedding IS NOT NULL
ORDER BY c.embedding <=> p.embedding
LIMIT 5;
```

### 4）验证关键词索引（中文 bigram 倒排）

```sql
SELECT left(content, 60) AS 片段
FROM kb_chunk
WHERE bigrams && ARRAY['吻合','合口','口漏']
LIMIT 5;
```

### 5）验证引用可溯源（页码 + 章节）

```sql
SELECT d.title, c.section, c.page_start, c.page_end
FROM kb_chunk c JOIN kb_document d ON d.id = c.document_id
WHERE c.page_start IS NOT NULL
ORDER BY c.id LIMIT 5;
```

---

## 四、Navicat 使用提示

1. **表列表是空的**：左侧 `colorectal_rag` 右键 →「刷新」，或双击展开 `public` 架构。表都在 `public` 下。
2. **`vector` 列显示成一长串数字**：正常，Navicat 不认识这个类型，只当字符串展示。想看得懂就用上面的 `<=>` 距离查询。
3. **不要双击打开 `kb_chunk`**：3772 行 × 1024 维向量，会把客户端卡住。用 `LIMIT` 或只查特定列。
4. **看图谱要去 Neo4j**：浏览器打开 `http://127.0.0.1:7474`，用户 `neo4j`，跑 `MATCH (e:Entity) RETURN e LIMIT 25`。

---

## 五、一句话记忆

> `kb_document` 是"有哪些文献"，`kb_chunk` 是"文献被切成多少块、每块的向量和关键词"，`kb_image` 是"影像和它的解读"；图谱在 Neo4j；其余表都是系统与日志。
