-- ============================================================
--  PostgreSQL 首次初始化脚本
--  由 docker-entrypoint-initdb.d 在数据卷为空时自动执行一次
-- ============================================================

-- 1) 向量扩展：BGE-M3 输出 1024 维向量
CREATE EXTENSION IF NOT EXISTS vector;

-- 2) 中文全文检索支持
--    zhparser 对中文分词效果最好，但官方 pgvector 镜像不含该扩展。
--    这里先用 PostgreSQL 内置能力兜底，方案见 docs/中文检索说明.md
--    - pg_trgm：三元组模糊匹配，对中文短语有效，无需额外分词词典
--    - unaccent：去除音标符号（英文标题检索有用）
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- 3) 常用工具扩展
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- 4) 确认向量扩展版本
DO $$
BEGIN
    RAISE NOTICE 'pgvector 版本: %', (SELECT extversion FROM pg_extension WHERE extname='vector');
    RAISE NOTICE '初始化完成：vector / pg_trgm / unaccent 已就绪';
END $$;
