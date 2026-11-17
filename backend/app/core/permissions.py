"""角色与权限映射（阶段九「用户权限管理模块」）。

报审表要求：「支持医师、管理员等不同角色的权限配置与账号管理」

设计取舍：为什么用固定角色 + 权限集，而不是自由权限位
--------------------------------------------------
本系统只面向院内两类使用者（管理员、医师）。若采用自由权限位
（每个用户单独勾选几十个权限点），会带来两个问题：
  1. 配置复杂度高，管理员容易误配，出现「医师能改系统配置」这类事故
  2. 权限点难以随功能演进同步维护

因此采用「角色 → 权限集」的集中映射：新增权限只需在本文件添加，
数据库无需变更；需要更细粒度时把 role 字段换成 role 表即可，
用户接口不受影响。

公开路径
--------
以下接口**刻意不做认证**，原因如下：

  /api/health*    健康检查需要被监控系统与启动脚本无凭据调用
  /api/qa*        问答是核心只读能力；阶段十的评测脚本依赖其无状态调用，
                  加认证会使评测脚本必须处理令牌刷新，增加失败面
  /api/search     检索同问答，纯读取
  /api/phases     前端下拉选项，无敏感信息
  /api/chat*      多轮对话与问答同类，属只读问答能力
  /api/images/{id}/raw  影像原图需被 <img src> 直接引用，
                  浏览器不会在 img 请求中携带 Authorization 头；
                  若强制认证会导致前端图片全部加载失败

这与「权限管理」并不矛盾：权限管理保护的是**写操作与用户数据**
（用户管理、系统配置、审计日志、影像上传删除），
而公开的是**无副作用的只读查询**。这样既保证了安全性，
又不牺牲核心能力的可用性。
"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    """系统角色。"""

    ADMIN = "admin"
    PHYSICIAN = "physician"


ROLE_LABELS: dict[str, str] = {
    Role.ADMIN.value: "管理员",
    Role.PHYSICIAN.value: "医师",
}


class Permission(str, Enum):
    """权限点。

    命名规则：`<资源>:<动作>`，便于阅读与将来扩展。
    """

    # 问答与检索（只读能力，所有登录用户拥有）
    QA_ASK = "qa:ask"

    # 影像
    IMAGE_VIEW = "image:view"
    IMAGE_UPLOAD = "image:upload"
    IMAGE_UPDATE = "image:update"
    IMAGE_DELETE = "image:delete"
    IMAGE_ANALYZE = "image:analyze"
    IMAGE_PUBLISH = "image:publish"

    # 典型病例库
    CASE_VIEW = "case:view"
    CASE_UPDATE = "case:update"
    CASE_DELETE = "case:delete"
    CASE_PUBLISH = "case:publish"

    # 知识库维护
    KB_VIEW = "kb:view"
    KB_REVIEW = "kb:review"          # 审核文献（标记是否可用）
    KB_UPDATE = "kb:update"          # 更新元数据
    KB_DELETE = "kb:delete"
    KB_EVALUATE = "kb:evaluate"      # 触发质量评估

    # 问答质量监控
    LOG_VIEW_SELF = "log:view_self"      # 查看自己的问答记录
    LOG_VIEW_ALL = "log:view_all"        # 查看全部用户记录
    FEEDBACK_SUBMIT = "feedback:submit"
    FEEDBACK_MANAGE = "feedback:manage"  # 处理反馈（标记已解决）

    # 系统配置与用户管理
    CONFIG_VIEW = "config:view"
    CONFIG_UPDATE = "config:update"
    USER_VIEW = "user:view"
    USER_MANAGE = "user:manage"      # 新增/修改/停用账号
    AUDIT_VIEW = "audit:view"

    # 运维操作
    MODEL_MANAGE = "model:manage"    # 加载/卸载检索模型


# 角色 → 权限集
#
# 医师拥有全部**内容相关**权限（问答、影像、反馈、查看自己的记录），
# 但不拥有**系统与用户管理**权限，也不能删除知识库内容
# —— 删除文献会破坏引用溯源链条，属高风险操作，仅管理员可执行。
ROLE_PERMISSIONS: dict[str, set[str]] = {
    Role.ADMIN.value: {p.value for p in Permission},
    Role.PHYSICIAN.value: {
        Permission.QA_ASK.value,
        Permission.IMAGE_VIEW.value,
        Permission.IMAGE_UPLOAD.value,
        Permission.IMAGE_UPDATE.value,
        Permission.IMAGE_ANALYZE.value,
        Permission.CASE_VIEW.value,
        Permission.CASE_UPDATE.value,
        Permission.KB_VIEW.value,
        Permission.LOG_VIEW_SELF.value,
        Permission.FEEDBACK_SUBMIT.value,
        Permission.CONFIG_VIEW.value,
    },
}


# 无需认证的路径前缀
PUBLIC_PATH_PREFIXES: tuple[str, ...] = (
    "/api/health",
    # ⚠️ 登录接口必须公开，否则无人能登录 —— 系统会被自己的认证拦住。
    # 实测踩过：受「默认拒绝」策略影响，/api/auth/login 被中间件拦截返回 401，
    # 导致完全无法登录。注意这里只放行 login；
    # /api/auth/me 与 /api/auth/change-password 仍需认证。
    "/api/auth/login",
    "/api/qa",
    "/api/search",
    # 引用面板「展开原文」取文本块全文。
    # 与 /api/search 同为只读，返回的也是检索结果本就会展示的原文；
    # 检索已公开，单独拦全文没有意义，反而会让未登录用户展不开引用。
    "/api/chunks",
    "/api/chat",
    "/api/phases",
    "/api/stats",
    # 影像原图与列表：前端 <img> 无法携带 Authorization 头
    "/api/images",
    "/api/cases",
    # 接口文档
    "/docs",
    "/redoc",
    "/openapi.json",
    "/",
)

# 需要认证的路径前缀（覆盖上面的公开前缀）。
#
# 存在这个「例外表」的原因：/api/images 整体公开是为了让 <img src> 能取图，
# 但上传、修改、删除、解读是有副作用的写操作，必须认证。
# 仅靠前缀匹配无法表达这种「同前缀下部分公开」的规则，
# 因此增加精确匹配的例外表。
PROTECTED_PATHS: tuple[tuple[str, str], ...] = (
    # (方法, 路径前缀)
    ("GET", "/api/images/manage"),
    ("GET", "/api/cases/manage"),
    ("POST", "/api/images"),
    ("PATCH", "/api/images"),
    ("DELETE", "/api/images"),
    ("POST", "/api/images/analyze"),
    ("POST", "/api/cases"),
    ("PATCH", "/api/cases"),
    ("DELETE", "/api/cases"),
)


# 明确允许匿名访问的只读检索子路径。
#
# 它们挂在 /api/images、/api/cases 前缀下，而这两组前缀在
# PROTECTED_PATHS 里"整组写操作需认证"。若不用白名单，
# 这两个端点会被一并拦掉；若沿用旧的"前缀下所有 POST 都豁免"，则未来新增的
# 写操作子路径会默认公开。白名单是这两种坏结果之间的唯一安全表达。
PUBLIC_SUBPATHS: tuple[tuple[str, str], ...] = (
    ("POST", "/api/images/similar"),
    ("POST", "/api/cases/search"),
)


def permissions_for(role: str) -> set[str]:
    """取某角色的权限集。未知角色返回空集（默认拒绝）。"""
    return ROLE_PERMISSIONS.get(role, set())


def has_permission(role: str, permission: str | Permission) -> bool:
    """判断角色是否拥有指定权限。"""
    value = permission.value if isinstance(permission, Permission) else permission
    return value in permissions_for(role)


def is_public_path(method: str, path: str) -> bool:
    """判断某请求是否无需认证。

    判定顺序（越靠前优先级越高）：
      1. 明确公开的只读检索子路径 → 公开
      2. 例外表（需认证的写操作）→ 需认证
      3. 公开前缀 → 公开
      4. 其余一律需认证（默认拒绝）

    两个容易出错的地方在这里统一处理：

    **尾斜杠**：`POST /api/cases/` 与 `POST /api/cases` 是同一个资源。
    此前用 `path != prot_prefix` 判断"是否创建接口本身"，带斜杠会被判成
    "不是创建接口"从而放行（当时只靠 Starlette 的 307 重定向兜住，
    关掉 redirect_slashes 就是真绕过）。现在先规范化去尾斜杠。

    **子路径**：原规则让 `/api/images`、`/api/cases` 下的**任何** POST 都豁免，
    未来新增 `POST /api/cases/{id}/publish` 之类会默认公开。
    现在改为白名单：只有明确列出的只读检索子路径公开，其余子路径仍需认证。
    """
    norm = path.rstrip("/") or "/"
    method = method.upper()

    # ① 明确公开的只读检索端点（与 /api/search 同性质，不需要登录）
    if (method, norm) in PUBLIC_SUBPATHS:
        return True

    # ② 例外表：同前缀下的写操作
    for prot_method, prot_prefix in PROTECTED_PATHS:
        if method == prot_method and norm.startswith(prot_prefix):
            return False

    # ③ 公开前缀
    for prefix in PUBLIC_PATH_PREFIXES:
        if prefix == "/":
            if norm == "/":
                return True
            continue
        if norm.startswith(prefix):
            return True

    # ④ 默认拒绝
    return False


def role_label(role: str) -> str:
    """角色中文名。"""
    return ROLE_LABELS.get(role, role)
