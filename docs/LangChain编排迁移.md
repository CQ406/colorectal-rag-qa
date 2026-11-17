# LangChain 编排迁移

日期：2026-11-08。主问答已使用 `langchain-core==1.7.6` 提供的 LangChain Expression Language（LCEL）执行，替换原同步和流式入口的手写流程控制。

## 实际执行流程

`rag_service.answer()` / `answer_stream()` → LCEL 请求状态 → 身份问题分支 / 多路检索 → 证据门控 → 拒答分支 / 提示词链 → 模型生成 → 引用解析 → 数值核验 → 原接口结果。

- `backend/app/services/rag_chain.py`：RunnableSequence（通过 `|` 连接）、RunnableBranch、RunnableGenerator、RunnableLambda、ChatPromptTemplate。
- `backend/app/services/rag_service.py`：实际 `.invoke()` / `.stream()` 入口、现有 HTTP 模型客户端适配、QaAnswer 和 SSE 输出适配。
- `conversation.py` 的同步和流式多轮问答继续调用上述入口，所以实际使用新链；数据库会话持久化、身份隔离、查询改写和意图识别仍由原服务负责。
- pgvector / 关键词 / 可选 Neo4j 召回、RRF、交叉编码器重排继续复用原算法，不重建向量或图谱，不变更语料和公开状态。
- 模型传输仍用原 `LLMClient`，保留代理、超时、重试、空答案检查及非流式用量统计。采用 LangChain 编排不要求改用另一个模型服务。
- 流式先发送 retrieval，再逐块 delta，最后校验并发送 done；取消时关闭生成器与 HTTP 客户端。空生成流输出错误，避免误报成功。
- 正式入口强制关闭 LangSmith 云追踪；每次推进/关闭流分别进入追踪上下文，避免跨线程恢复生成器时上下文令牌错配。

这不是只在依赖中安装 LangChain，也不是把整个旧问答函数包成单个 Runnable：检索、证据门控、提示词、生成和校验分别组成可执行阶段。当前没有引入 LangGraph Agent。影像视觉描述、独立影像综合解读和图谱抽取仍使用原专用服务，本次迁移范围是文本主问答与多轮问答的 RAG 编排。

## 验证

- 安装当前项目虚拟环境依赖，`pip check` 通过。
- `python -m pytest backend/tests -q`：**144 项通过，10.50 秒**，含新增 15 项 LangChain 回归。
- 新测试覆盖：提示词字面花括号保留、引用页码范围、无效引用、数值提醒、用量和模型字段；空/低相关证据与身份短路不调用模型；流式逐块输出、异常、空流、取消释放及跨线程恢复；FastAPI 实际 SSE 接口；多轮会话经新链生成并持久化引用、隔离其他身份。
- 模型与检索使用本地合成适配器，验收没有新增云模型调用或语料外发。
- 用户恢复 Docker 后重新执行 `npm run test:e2e`：**16 项全部通过，约 1.2 分钟**；覆盖权限、发布与撤回、会话恢复与隔离、引用原文、移动端及文献修订历史。独立测试库 `rag_e2e_5501301f549f` 已自动清理。首次因 PostgreSQL 未运行而启动超时的阻塞已解除。
- Docker 故障历史（现已恢复，PostgreSQL、Neo4j、Redis 均 healthy）：此前 Docker Desktop 启动失败，用户截图为 WSL `main` 目录创建冲突。只读检查时 `main` 已不存在，WSL 无已注册发行版，但 `C:/Users/97657/AppData/Local/Docker/wsl/disk/docker_data.vhdx` 仍存在（12588154880 字节）。文件存在不代表内部资料已完成完整性验证。没有删除目录、注销发行版或恢复出厂设置。
- 导出真实链的结构图：[rag-langchain.mmd](rag-langchain.mmd)。分支内各阶段以 `rag_chain.py` 定义为准。

## 安装与运行

依赖入口为 `backend/requirements.txt`，当前环境快照为 `backend/constraints-20261108.txt`。旧 `constraints-20261104.txt` 保留作历史快照，不应用于本次 LangChain 安装，因为旧 certifi 约束与新增依赖要求不一致。新快照尚未在全新机器上验证。

Docker/数据库已恢复，浏览器验收已完成。原接口地址和前端无需改动。
