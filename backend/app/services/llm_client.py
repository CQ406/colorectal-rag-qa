"""大语言模型客户端（OpenAI 兼容接口）。

支持 DeepSeek / 通义千问 / 智谱等所有兼容 OpenAI Chat Completions 的服务。

设计要点
--------
1. **trust_env=False**：本机 Windows 系统代理指向未运行的 127.0.0.1:7899，
   必须绕开，否则 httpx 会连接失败。这是本环境的必要设置。
2. **流式与非流式双接口**：流式用于前端逐字显示；非流式用于结构化输出
   （引用标注需要拿到完整 JSON 才能解析）。
3. **重试与超时**：LLM 服务偶发超时或 5xx，指数退避重试。
4. **用量统计**：返回 token 用量，供成本监控与论文性能分析。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("services.llm")


# ----------------------------------------------------------------------
#  数据结构
# ----------------------------------------------------------------------
@dataclass
class ChatUsage:
    """token 用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @property
    def as_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class ChatResult:
    """一次完整对话的结果。"""

    content: str
    usage: ChatUsage = field(default_factory=ChatUsage)
    model: str = ""
    elapsed_ms: float = 0.0
    finish_reason: str = ""
    # 推理模型的思考过程（如 deepseek-flash / 各类 reasoner）。
    # 保留供审计：医学场景可复盘模型的推理链条。
    reasoning_content: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.content.strip()


class LLMError(RuntimeError):
    """LLM 调用失败。"""


# ----------------------------------------------------------------------
#  客户端
# ----------------------------------------------------------------------
class LLMClient:
    """OpenAI 兼容的 Chat Completions 客户端。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
    ) -> None:
        self.base_url = (base_url or settings.llm.llm_base_url).rstrip("/")
        self.api_key = api_key or settings.llm.llm_api_key
        self.model = model or settings.llm.llm_model
        self.timeout = timeout or settings.llm.llm_timeout

        if not self.api_key:
            raise LLMError(
                "未配置 LLM_API_KEY。请在 .env 中填写 DeepSeek 密钥。"
            )

        # trust_env=False 是关键：绕开失效的系统代理
        self._client = httpx.Client(
            timeout=httpx.Timeout(self.timeout, connect=15.0),
            trust_env=False,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

    @property
    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # ------------------------------------------------------------------
    #  非流式：用于结构化输出
    # ------------------------------------------------------------------
    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict | None = None,
        max_retries: int = 3,
    ) -> ChatResult:
        """非流式对话，返回完整回复。

        Args:
            messages: OpenAI 格式的消息列表
            temperature: 采样温度，默认取配置（0.2，医学场景需低随机性）
            max_tokens: 最大生成 token 数
            response_format: 如 {"type": "json_object"}，强制 JSON 输出
            max_retries: 失败重试次数
        """
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": (
                temperature if temperature is not None else settings.llm.llm_temperature
            ),
            "max_tokens": max_tokens or settings.llm.llm_max_tokens,
            "stream": False,
        }
        if response_format:
            payload["response_format"] = response_format

        last_error: Exception | None = None
        for attempt in range(max_retries):
            t0 = time.perf_counter()
            try:
                resp = self._client.post(self._endpoint, json=payload)
                elapsed = (time.perf_counter() - t0) * 1000

                if resp.status_code == 200:
                    data = resp.json()
                    choice = data["choices"][0]
                    message = choice["message"]
                    content = message.get("content") or ""
                    reasoning = message.get("reasoning_content") or ""
                    finish_reason = choice.get("finish_reason", "")
                    usage_data = data.get("usage", {})

                    result = ChatResult(
                        content=content,
                        usage=ChatUsage(
                            prompt_tokens=usage_data.get("prompt_tokens", 0),
                            completion_tokens=usage_data.get("completion_tokens", 0),
                            total_tokens=usage_data.get("total_tokens", 0),
                        ),
                        model=data.get("model", self.model),
                        elapsed_ms=round(elapsed, 1),
                        finish_reason=finish_reason,
                        reasoning_content=reasoning,
                    )

                    # 关键防护：空答案必须显式报错，不能静默返回空字符串。
                    # 成因：推理模型（如 deepseek-flash）把 token 用在 reasoning_content 上，
                    # 若 max_tokens 偏小，推理阶段就耗尽额度，content 为空、finish_reason='length'。
                    # 若静默返回，用户会收到空白答案且无从排查。
                    #
                    # 另需注意：即便 max_tokens 充足，也实测到偶发空答案（API 侧抖动），
                    # 此时 finish_reason 通常为 'stop'。故统一按可重试处理——
                    # 空答案重试的成本远低于让用户看到空白。
                    if result.is_empty:
                        detail = (
                            f"模型返回空内容（finish_reason={finish_reason}，"
                            f"推理内容 {len(reasoning)} 字符，"
                            f"完成 token {result.usage.completion_tokens}）"
                        )
                        if finish_reason == "length":
                            raise LLMError(
                                f"{detail}。原因：max_tokens 过小，推理阶段已耗尽额度。"
                                f"解决：增大 LLM_MAX_TOKENS（当前 {payload['max_tokens']}）。"
                            )
                        # 偶发抖动：记录后重试
                        if attempt < max_retries - 1:
                            logger.warning(
                                "%s，第 %d 次重试", detail, attempt + 1
                            )
                            last_error = LLMError(detail)
                            time.sleep(1.5 * (attempt + 1))
                            continue
                        raise LLMError(f"{detail}。已重试 {max_retries} 次仍返回空内容。")

                    return result

                # 可重试的状态码
                if resp.status_code in (429, 500, 502, 503, 504):
                    wait = 2**attempt
                    logger.warning(
                        "LLM 返回 %d，%d 秒后重试 (%d/%d)",
                        resp.status_code, wait, attempt + 1, max_retries,
                    )
                    time.sleep(wait)
                    last_error = LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                    continue

                # 不可重试：密钥或参数错误
                raise LLMError(
                    f"LLM 调用失败 HTTP {resp.status_code}: {resp.text[:300]}"
                )

            except httpx.HTTPError as exc:
                wait = 2**attempt
                logger.warning("LLM 请求异常: %s，%d 秒后重试", exc, wait)
                time.sleep(wait)
                last_error = exc

        raise LLMError(f"LLM 调用失败，已重试 {max_retries} 次: {last_error}")

    # ------------------------------------------------------------------
    #  流式：用于前端逐字显示
    # ------------------------------------------------------------------
    def chat_stream(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[str]:
        """流式对话，逐块产出文本增量。

        用法::

            for delta in client.chat_stream(messages):
                print(delta, end="", flush=True)

        注意：流式模式下**拿不到用量统计**（OpenAI 兼容接口在最后一个 chunk
        才可能返回 usage，且各服务商支持不一）。若需统计用量请用 chat()。
        """
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": (
                temperature if temperature is not None else settings.llm.llm_temperature
            ),
            "max_tokens": max_tokens or settings.llm.llm_max_tokens,
            "stream": True,
        }

        try:
            with self._client.stream("POST", self._endpoint, json=payload) as resp:
                if resp.status_code != 200:
                    body = resp.read().decode("utf-8", "ignore")
                    raise LLMError(f"LLM 流式调用失败 HTTP {resp.status_code}: {body[:300]}")

                for line in resp.iter_lines():
                    if not line:
                        continue
                    # SSE 格式：以 "data: " 开头，"[DONE]" 表示结束
                    if line.startswith("data: "):
                        line = line[6:]
                    if line.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    text = delta.get("content")
                    if text:
                        yield text
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM 流式请求异常: {exc}") from exc


# ----------------------------------------------------------------------
#  便捷函数
# ----------------------------------------------------------------------
def check_llm_available() -> tuple[bool, str]:
    """健康检查：发一次最小请求验证密钥与网络。

    会产生极少量 token 消耗（约 20 个），用于确认配置正确。
    """
    if not settings.llm.is_configured:
        return False, "未配置 LLM_API_KEY"
    try:
        with LLMClient(timeout=30) as client:
            result = client.chat(
                [{"role": "user", "content": "ping"}], max_tokens=5, temperature=0
            )
        return True, f"{result.model} 可用（{result.elapsed_ms:.0f}ms，{result.usage.total_tokens} tokens）"
    except Exception as exc:  # noqa: BLE001
        return False, f"调用失败: {type(exc).__name__}: {str(exc)[:150]}"
