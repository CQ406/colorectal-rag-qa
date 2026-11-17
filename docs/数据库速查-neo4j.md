# Neo4j 知识图谱速查

> Browser：<http://127.0.0.1:7474>　Bolt：`bolt://127.0.0.1:7687`
> 用户 `neo4j` / 口令见 `.env` 的 `NEO4J_PASSWORD`（社区版 5.26，容器 `cr-neo4j`）
>
> 它和 PostgreSQL 是**两个库**：Postgres 存"文献与文本块"，Neo4j 存"实体与关系"。
> 两者靠 `chunk_ids` 打通——这是理解整套图谱召回的关键。

---

## 一、规模与结构

| 项 | 值 |
|---|---|
| 节点 | **4,156** |
| 关系 | **5,600** |
| 节点标签 | 只有一个：`:Entity` |
| 关系类型 | 10 类 |

**为什么只有一个标签**：实体类型由 LLM 抽取，如果每类都建标签（`:Disease` `:Drug` …），
类型一多标签就失控、索引要逐个建。所以统一 `:Entity`，类型放进 `type` 属性，
在 `(name, type)` 上建唯一约束。

### 实体类型分布（`n.type`）

| 类型 | 数量 | 含义 |
|---|---|---|
| `metric` | 412 | 指标（如吻合口漏发生率、住院天数） |
| `procedure` | 230 | 术式/操作 |
| `exam` | 181 | 检查（MRI、肠镜等） |
| `treatment` | 160 | 治疗（新辅助治疗、观察等待） |
| `disease` | 134 | 疾病 |
| `complication` | 133 | 并发症 |
| `drug` | 92 | 药物 |
| `anatomy` | 81 | 解剖部位 |

### 关系类型分布

| 关系 | 数量 | 语义 |
|---|---|---|
| `TREATS` | 345 | 治疗 → 疾病 |
| `MEASURES` | 200 | 指标 → 衡量对象 |
| `APPLIES_TO` | 198 | 术式/治疗 → 适用疾病 |
| `COMPARES_WITH` | 132 | 方案之间的比较 |
| `LOCATED_AT` | 122 | 病变 → 解剖部位 |
| `PREVENTS` | 102 | 措施 → 并发症 |
| `DIAGNOSES` | 101 | 检查 → 疾病 |
| `CAUSES_COMPLICATION` | 89 | 疾病/术式 → 并发症 |
| `USES_DRUG` | 49 | 方案 → 药物 |
| `HAS_RISK_FACTOR` | 42 | 疾病 → 危险因素 |

---

## 二、属性

### 节点 `:Entity`

| 属性 | 说明 |
|---|---|
| `name` | 实体名（如"直肠癌"） |
| `norm` | 归一化名（去空格/统一写法，用于合并同义） |
| `type` | 8 类之一 |
| `frequency` | 出现频次 |
| `aliases` | 别名列表 |
| **`chunk_ids`** | **它出现在哪些文本块**——回映射到 PostgreSQL 的桥 |

### 关系

| 属性 | 说明 |
|---|---|
| `chunk_ids` | 这条关系是从哪些文本块抽出来的 |
| `weight` | 权重（抽取次数/置信度折算） |

### 约束与索引

```cypher
CONSTRAINT entity_unique FOR (e:Entity) REQUIRE (e.name, e.type) IS UNIQUE

INDEX entity_name  ON :Entity(name)
INDEX entity_norm  ON :Entity(norm)     -- 别名归一后模糊匹配用
INDEX entity_type  ON :Entity(type)     -- 按类型筛选
```

> 约束是 **(name, type) 复合唯一**，不是 name 唯一：
> "吻合口漏"既是 `complication` 也可能作为 `disease` 出现，两者要能共存。

---

## 三、图谱枢纽（边数最多的实体）

| 实体 | 类型 | 边数 |
|---|---|---|
| 直肠癌 | disease | 230 |
| 结直肠癌 | disease | 95 |
| 吻合口漏 | complication | 46 |
| 局部晚期直肠癌 | disease | 44 |
| 结肠癌 | disease | 42 |
| 直肠 | anatomy | 38 |
| 低位直肠癌 | disease | 38 |
| 腹腔镜手术 | procedure | 33 |

这张表能说明一件事：**图谱是围绕"直肠癌"长起来的**，
和语料主体一致（中文指南 + 结直肠外科文献）。

---

## 四、五条 Cypher，由浅入深

### 1）看一眼图谱长什么样

```cypher
MATCH (n:Entity) RETURN n LIMIT 25
```

（在 Browser 里点节点可展开邻居，比纯表格直观。）

### 2）规模与类型分布

```cypher
MATCH (n:Entity) RETURN n.type AS 类型, count(*) AS 数量
ORDER BY 数量 DESC;

MATCH ()-[r]->() RETURN type(r) AS 关系, count(*) AS 数量
ORDER BY 数量 DESC;
```

### 3）以"吻合口漏"为中心看一跳邻居

```cypher
MATCH (n:Entity {name: '吻合口漏'})-[r]-(m:Entity)
RETURN n.name AS 中心, type(r) AS 关系, m.name AS 邻居, m.type AS 类型
LIMIT 30;
```

### 4）看一条完整的三元组（含来源块）

```cypher
MATCH (a:Entity)-[r]->(b:Entity)
RETURN a.name AS 头, type(r) AS 关系, b.name AS 尾,
       size(r.chunk_ids) AS 来源块数, r.weight AS 权重
ORDER BY 来源块数 DESC LIMIT 10;
```

### 5）从图谱反查文本块（这就是召回在做的事）

```cypher
MATCH (n:Entity)
WHERE n.name IN ['直肠癌', '吻合口漏']
RETURN n.name AS 实体, n.chunk_ids[0..5] AS 前5个块ID
LIMIT 5;
```

拿到这些 `chunk_ids` 后，去 PostgreSQL 里：

```sql
SELECT id, section, page_start, left(content, 80)
FROM kb_chunk WHERE id = ANY('{<粘贴块ID>}'::uuid[]);
```

---

## 五、图谱召回是怎么用它的

实际实现是 `app/services/graph_search.py`，流程：

```
① 启动时从 Neo4j 拉全量实体（name / norm / type / chunk_ids）
   → 在内存里建 EntityIndex，并把实体名向量化（带磁盘缓存）

② 查询向量 vs 实体名向量 做余弦匹配
   → 相似度 ≥ 阈值、最多 8 个实体（MAX_MATCHED_ENTITIES）

③ 命中实体做多跳扩展（按跳数衰减权重）

④ 收集这些实体的 chunk_ids → 回 PostgreSQL 取块
   → 块得分取「来源实体的最高分」（不累加，避免长文本靠实体多占优）

⑤ 这些块作为第三路召回结果，与向量路、关键词路一起进 RRF
```

**两个关键点（答辩常被问）**

1. **图谱不是直接返回答案，而是返回"文本块"**。真正喂给大模型的还是原文，
   图谱只负责"把和问题实体相关的段落找出来"。
2. **实体名也做向量匹配，而不是纯字面匹配**。因为用户问"吻合口瘘"、
   库里存"吻合口漏"，纯字面会漏；向量匹配 + `norm`/`aliases` 归一能兜住。

---

## 六、一句话记忆

> Neo4j 里只有一种节点 `:Entity`（8 类实体）和 10 类关系；
> 每个实体、每条关系都记着 `chunk_ids`——**这是图谱能回到原文的桥**；
> 图谱召回 = 实体名向量匹配 → 多跳扩展 → 用 `chunk_ids` 取回文本块 → 并入 RRF。
