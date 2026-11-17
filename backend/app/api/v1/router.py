"""API v1 路由聚合。

路由注册顺序不影响匹配（FastAPI 按前缀与方法精确匹配），
但这里的注释顺序刻意与「数据流」一致，便于阅读：
先认证、再检索问答，最后是管理与运维接口。
"""

from fastapi import APIRouter

from app.api.v1 import admin, auth, cases, chat, health, images, knowledge, models, qa, search

api_router = APIRouter()

# 健康检查（公开：供监控与启动脚本无凭据调用）
api_router.include_router(health.router)

# 认证与用户管理（阶段九）
# 放在检索之前：登录是使用系统的前置步骤
api_router.include_router(auth.router)

# 检索（多路召回 + RRF 融合）
api_router.include_router(search.router)

# 问答（RAG 闭环：检索 + 生成 + 引用溯源）
api_router.include_router(qa.router)

# 多轮对话（查询改写 + 意图识别 + 会话上下文）
api_router.include_router(chat.router)

# 影像知识库与辅助解读（VLM 描述 + 文献证据综合）
api_router.include_router(images.router)

# 典型病例库（文本病例管理 + 相似病例检索）
api_router.include_router(cases.router)

# 知识库维护（阶段九：文献审核、更新、版本管理、质量评估）
api_router.include_router(knowledge.router)

# 系统管理（阶段九：质量监控、审计日志、系统配置）
api_router.include_router(admin.router)

# 模型管理与自检
api_router.include_router(models.router)
