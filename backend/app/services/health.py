"""外部依赖连通性检查 —— 供 /health 接口使用。

每个检查函数都返回 (是否可用, 描述信息)，且绝不抛异常，
保证健康检查本身在依赖不可用时也能正常返回结果。
"""

from __future__ import annotations

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("services.health")


def check_postgres() -> tuple[bool, str]:
    """检查 PostgreSQL + pgvector。"""
    from app.db.session import check_connection

    return check_connection()


def check_neo4j() -> tuple[bool, str]:
    """检查 Neo4j 连通性并统计节点数（验证图谱是否已构建）。"""
    try:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(
            settings.neo4j.neo4j_uri,
            auth=(settings.neo4j.neo4j_user, settings.neo4j.neo4j_password),
            connection_timeout=5,
        )
        try:
            driver.verify_connectivity()
            with driver.session() as session:
                version = session.run(
                    "CALL dbms.components() YIELD name, versions "
                    "RETURN versions[0] AS v"
                ).single()
                node_count = session.run(
                    "MATCH (n) RETURN count(n) AS c"
                ).single()["c"]
            return True, f"Neo4j {version['v'] if version else '?'} / 节点数 {node_count}"
        finally:
            driver.close()
    except Exception as exc:  # noqa: BLE001
        return False, f"连接失败: {type(exc).__name__}: {str(exc)[:120]}"


def check_redis() -> tuple[bool, str]:
    """检查 Redis 连通性。"""
    try:
        import redis

        client = redis.Redis(
            host=settings.redis.redis_host,
            port=settings.redis.redis_port,
            db=settings.redis.redis_db,
            socket_connect_timeout=5,
        )
        client.ping()
        info = client.info("server")
        # 键数量用于确认会话/缓存是否在用
        key_count = client.dbsize()
        return True, f"Redis {info.get('redis_version', '?')} / 当前 {key_count} 个键"
    except Exception as exc:  # noqa: BLE001
        return False, f"连接失败: {type(exc).__name__}: {str(exc)[:120]}"


def check_storage() -> tuple[bool, str]:
    """检查文件存储后端（默认本地文件系统）。

    原计划使用 MinIO，但其官方镜像已于 2025-10 下架，
    故改为本地存储，见 app/services/storage.py 中的说明。
    """
    from app.services.storage import check_storage as _check

    return _check()


def check_llm_config() -> tuple[bool, str]:
    """检查 LLM 参数是否已配置（不发起真实请求，避免浪费额度）。"""
    if not settings.llm.is_configured:
        return False, "未配置 LLM_API_KEY（请在 .env 中填写 DeepSeek 密钥）"
    return True, f"已配置: {settings.llm.llm_model} @ {settings.llm.llm_base_url}"


def check_vlm_config() -> tuple[bool, str]:
    """检查多模态模型参数是否已配置。"""
    if not settings.vlm.is_configured:
        return False, "未配置 VLM_API_KEY（请在 .env 中填写智谱密钥）"
    return True, f"已配置: {settings.vlm.vlm_model} @ {settings.vlm.vlm_base_url}"
