"""多模态视觉模型客户端（影像解读）。

设计要点
--------
**为什么走云端 API 而不是本地部署视觉模型**
  本机 GPU 为 RTX 5060 Laptop 8GB，已被 BGE-M3 与重排模型占用约 3GB。
  本地部署 7B 级视觉模型至少需 6~8GB 显存，会与检索模型争抢资源，
  导致问答首字延迟显著上升。而影像解读是**低频操作**（用户偶尔上传一张图），
  调用云端 API 更合理：不占显存、无需维护、且免费额度足够毕设使用。

  本机算力优先保障高频的检索链路，这是明确的资源分配取舍。

**接口兼容性**
  智谱 GLM-4V 系列提供 OpenAI 兼容的 /chat/completions 接口，
  消息体用 image_url 传 base64 data URL。因此本客户端与文本客户端
  结构一致，便于统一维护与替换模型。

**免费模型选择**
  glm-4v-flash 为智谱免费模型，适合毕设。注册后把密钥填入 .env 的
  VLM_API_KEY 即可（见 README 的申请步骤）。
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("services.vision")

# 支持的图片格式（与前端上传校验保持一致）
SUPPORTED_MIME = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/bmp": "bmp",
}

# 单张图片大小上限（base64 编码后体积增大约 33%，过大易触发 API 限制）
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# 输出的 max_tokens 上限。
#
# ⚠️ 不同厂商的范围差异很大，必须按模型区分：
#   - 智谱 glm-4v-flash：**上限 1024**，超出直接返回 400
#     （实测 {"error":{"code":"1210","message":"max_tokens参数非法：
#      限制数值范围[1,1024]"}}）
#   - 其他多模态模型常见上限为 2048~4096
#
# 这里取保守的 1024，兼容免费模型。若更换为上限更高的模型，
# 可通过 .env 的 VLM_MAX_TOKENS 覆盖。
DEFAULT_MAX_TOKENS = 1024

# 不同模型的已知上限（用于自动收敛，避免用户配置越界而报错）
MODEL_MAX_TOKENS = {
    "glm-4v-flash": 1024,
    "glm-4v": 1024,
    "glm-4v-plus": 1024,
}


class VisionError(RuntimeError):
    """视觉模型调用错误。"""


@dataclass
class VisionResult:
    """一次影像解读结果。"""

    content: str
    model: str
    elapsed_ms: float
    usage: dict = field(default_factory=dict)


def is_configured() -> bool:
    """检查 VLM 是否已配置密钥。

    未配置时前端应给出明确指引而不是报错，
    因为「未填密钥」是部署常态而非程序缺陷。
    """
    key = (settings.vlm.vlm_api_key or "").strip()
    if not key:
        return False
    # 占位符判定：.env.example 中的中文提示未被替换
    placeholders = ("在此填写", "your-", "sk-xxx", "TODO", "CHANGE_ME")
    return not any(p in key for p in placeholders)


def guess_mime(path: Path | str, fallback: str = "image/jpeg") -> str:
    """按扩展名推断 MIME 类型。"""
    suffix = Path(path).suffix.lower().lstrip(".")
    mapping = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "webp": "image/webp", "bmp": "image/bmp",
    }
    return mapping.get(suffix, fallback)


def encode_data_url(data: bytes, mime: str = "image/jpeg") -> str:
    """把图片字节编码为 data URL（OpenAI 兼容接口所需格式）。"""
    if len(data) > MAX_IMAGE_BYTES:
        raise VisionError(
            f"图片过大（{len(data) / 1024 / 1024:.1f} MB），"
            f"上限 {MAX_IMAGE_BYTES // 1024 // 1024} MB。请压缩后重试。"
        )
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


class VisionClient:
    """视觉模型客户端（OpenAI 兼容 /chat/completions）。"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
    ) -> None:
        cfg = settings.vlm
        self.api_key = api_key or cfg.vlm_api_key
        self.base_url = (base_url or cfg.vlm_base_url).rstrip("/")
        self.model = model or cfg.vlm_model
        self.timeout = timeout or cfg.vlm_timeout

        if not is_configured() and api_key is None:
            raise VisionError(
                "未配置视觉模型密钥。请在 .env 中设置 VLM_API_KEY。"
                "智谱 GLM-4V-Flash 免费，申请地址 https://bigmodel.cn"
            )

        # trust_env=False：绕过本机失效的代理设置（历史上曾导致连接挂起）
        self._client = httpx.Client(timeout=self.timeout, trust_env=False)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "VisionClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def analyze(
        self,
        image: bytes | str | Path,
        prompt: str,
        *,
        mime: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 0.2,
    ) -> VisionResult:
        """解读单张图片。

        Args:
            image: 图片字节 / 文件路径
            prompt: 解读指令（决定输出结构）
            mime: MIME 类型，None 时按路径推断
            max_tokens: 输出上限，会按模型已知上限自动收敛
        """
        if isinstance(image, (str, Path)):
            path = Path(image)
            data = path.read_bytes()
            mime = mime or guess_mime(path)
        else:
            data = image
            mime = mime or "image/jpeg"

        # 按模型已知上限收敛：部分模型（如 glm-4v-flash）上限仅 1024，
        # 超出会直接返回 400 而非静默截断，因此必须在请求前收敛。
        cap = MODEL_MAX_TOKENS.get(self.model)
        if cap is not None and max_tokens > cap:
            logger.debug(
                "max_tokens %d 超过 %s 的上限 %d，自动收敛",
                max_tokens, self.model, cap,
            )
            max_tokens = cap

        data_url = encode_data_url(data, mime)
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        t0 = time.perf_counter()
        try:
            resp = self._client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise VisionError(f"视觉模型请求失败: {exc}") from exc

        elapsed = (time.perf_counter() - t0) * 1000

        if resp.status_code != 200:
            detail = resp.text[:400]
            raise VisionError(
                f"视觉模型返回 {resp.status_code}: {detail}"
            )

        try:
            body = resp.json()
        except ValueError as exc:
            raise VisionError(f"视觉模型响应非 JSON: {resp.text[:200]}") from exc

        choices = body.get("choices") or []
        if not choices:
            raise VisionError(f"视觉模型响应缺少 choices: {str(body)[:300]}")

        content = (choices[0].get("message") or {}).get("content") or ""
        if isinstance(content, list):
            # 部分实现返回分段内容数组
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        content = content.strip()
        if not content:
            raise VisionError("视觉模型返回空内容")

        return VisionResult(
            content=content,
            model=body.get("model") or self.model,
            elapsed_ms=round(elapsed, 1),
            usage=body.get("usage") or {},
        )


def check_vision() -> tuple[bool, str]:
    """健康检查：判断视觉模型是否可用（不实际发起推理以避免耗时）。"""
    if not is_configured():
        return False, "未配置 VLM_API_KEY（智谱 GLM-4V-Flash 免费，见 README）"
    return True, f"已配置 {settings.vlm.vlm_model}"
