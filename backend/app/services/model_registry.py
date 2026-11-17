"""检索模型注册表（延迟加载 + 单例）。

设计要点：
  1. **懒加载**：不在应用启动时载入模型，首次调用才加载。这样：
     - 启动快（无需等 6 秒模型加载）
     - 不占用显存影响其他任务（如本地训练、其他项目）
  2. **线程安全**：用锁保护加载过程，避免并发请求重复加载导致显存翻倍。
  3. **可卸载**：提供 unload 接口，供后台批量任务与在线服务切换时释放显存。
  4. **本地路径加载**：必须传目录路径。传 'BAAI/bge-m3' 会触发 snapshot_download
     拉取仓库全部文件，而仓库中 imgs/.DS_Store 被镜像站拒绝(403)导致加载失败。

实测性能（RTX 5060 Laptop 8GB，fp16）：
     BGE-M3      : 694 条/秒 (batch 64)，峰值显存 2.37GB
     reranker    : 105 对/秒，双模型全开峰值 4.94GB
"""

from __future__ import annotations

import gc
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("services.model_registry")

# 离线模式提示：模型已下载到本地目录，禁止回源 HuggingFace 以避免网络问题
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")


@dataclass
class _ModelSlot:
    """单个模型的加载状态。"""

    name: str
    path: str
    instance: Any = None
    load_seconds: float = 0.0
    load_count: int = 0
    last_error: str | None = field(default=None)


class ModelRegistry:
    """BGE-M3 与 reranker 的懒加载单例注册表。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._embedder = _ModelSlot("bge-m3", settings.model.embedding_model_path)
        self._reranker = _ModelSlot("bge-reranker-v2-m3", settings.model.reranker_model_path)
        # 是否强制以 fp32 加载向量模型。
        # None = 按设备默认（GPU 用 fp16）；True/False = 被 reload_embedder 显式指定。
        self._embedder_force_fp32: bool | None = None

    # ------------------------------------------------------------------
    #  设备与可用性
    # ------------------------------------------------------------------
    @property
    def device(self) -> str:
        """解析实际使用的设备。GPU 不可用时自动回退到 CPU。"""
        if settings.model.force_cpu:
            return "cpu"
        if settings.model.embedding_device != "cuda":
            return settings.model.embedding_device
        try:
            import torch

            return "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            return "cpu"

    def _validate_path(self, slot: _ModelSlot) -> None:
        """加载前校验模型目录，给出可操作的错误提示。"""
        p = Path(slot.path)
        if not p.is_dir():
            raise FileNotFoundError(
                f"{slot.name} 模型目录不存在: {slot.path}\n"
                f"解决: 运行 download_models.py 下载模型，"
                f"或修改 .env 中的路径配置。"
            )
        if not any(p.iterdir()):
            raise FileNotFoundError(f"{slot.name} 模型目录为空: {slot.path}")

    @property
    def status_text(self) -> str:
        """当前加载状态摘要（用于启动日志与健康检查）。"""
        loaded = [
            s.name for s in (self._embedder, self._reranker) if s.instance is not None
        ]
        return f"已加载 {loaded}" if loaded else "未加载（首次调用时载入）"

    # ------------------------------------------------------------------
    #  BGE-M3 向量模型
    # ------------------------------------------------------------------
    def get_embedder(self):
        """获取 BGE-M3 模型实例（首次调用时加载）。"""
        with self._lock:
            if self._embedder.instance is not None:
                return self._embedder.instance

            self._validate_path(self._embedder)
            logger.info("加载 BGE-M3 向量模型: %s (device=%s)", self._embedder.path, self.device)

            from FlagEmbedding import BGEM3FlagModel

            t0 = time.perf_counter()
            # fp16 显存与速度更优，但在部分 GPU/驱动组合下会数值溢出，
            # 使 encode 返回 NaN（pgvector 直接拒收 NaN 向量）。
            # 一旦实测到 NaN，由 reload_embedder(force_fp32=True) 切到 fp32。
            if self._embedder_force_fp32 is None:
                use_fp16 = self.device == "cuda"
            else:
                use_fp16 = not self._embedder_force_fp32
            logger.info(
                "BGE-M3 精度: %s", "fp16" if use_fp16 else "fp32"
            )
            try:
                model = BGEM3FlagModel(
                    self._embedder.path,          # 本地目录，非仓库名
                    use_fp16=use_fp16,
                    device=self.device,
                )
            except Exception as exc:  # noqa: BLE001
                self._embedder.last_error = str(exc)
                logger.error("BGE-M3 加载失败: %s", exc)
                raise

            self._embedder.instance = model
            self._embedder.load_seconds = time.perf_counter() - t0
            self._embedder.load_count += 1
            self._embedder.last_error = None
            logger.info("BGE-M3 加载完成，耗时 %.1f 秒", self._embedder.load_seconds)
            return model

    def reload_embedder(self, *, force_fp32: bool | None = None) -> None:
        """卸载向量模型，下一次使用时按新的精度设置重新加载。

        为什么需要这个方法
        ------------------
        fp16 在部分 GPU/驱动组合下会出现数值溢出，`encode` 返回 **NaN**。
        NaN 向量会让 pgvector 抛 `NaN not allowed in vector`，
        表现为「检索/问答整体 500」，用户侧只看到一长串 SQL 报错。

        实测发生过一次：后端进程运行一段时间后，所有查询向量都变成 NaN，
        重启进程即恢复。说明是进程内的模型状态问题，而非数据问题。
        本方法提供**不重启进程**的自愈路径：切到 fp32 重新加载后重试。

        Args:
            force_fp32: True 强制 fp32；False 强制 fp16；None 恢复按设备默认。
        """
        with self._lock:
            self._embedder.instance = None
            self._embedder.load_seconds = 0.0
            self._embedder_force_fp32 = force_fp32
            try:
                import torch

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass
            logger.warning(
                "向量模型已卸载，下次调用按 %s 重新加载",
                "fp32" if force_fp32 else ("fp16" if force_fp32 is False else "设备默认"),
            )

    # ------------------------------------------------------------------
    #  重排模型
    # ------------------------------------------------------------------
    def get_reranker(self):
        """获取 bge-reranker-v2-m3 实例（首次调用时加载）。"""
        with self._lock:
            if self._reranker.instance is not None:
                return self._reranker.instance

            self._validate_path(self._reranker)
            logger.info(
                "加载重排模型: %s (device=%s)", self._reranker.path, self.device
            )

            from FlagEmbedding import FlagReranker

            t0 = time.perf_counter()
            try:
                model = FlagReranker(
                    self._reranker.path,
                    use_fp16=(self.device == "cuda"),
                    device=self.device,
                )
            except Exception as exc:  # noqa: BLE001
                self._reranker.last_error = str(exc)
                logger.error("重排模型加载失败: %s", exc)
                raise

            self._reranker.instance = model
            self._reranker.load_seconds = time.perf_counter() - t0
            self._reranker.load_count += 1
            self._reranker.last_error = None
            logger.info("重排模型加载完成，耗时 %.1f 秒", self._reranker.load_seconds)
            return model

    # ------------------------------------------------------------------
    #  显存管理
    # ------------------------------------------------------------------
    def unload_all(self) -> None:
        """卸载全部模型并释放显存。"""
        with self._lock:
            for slot in (self._embedder, self._reranker):
                if slot.instance is not None:
                    logger.info("卸载 %s", slot.name)
                    slot.instance = None
            gc.collect()
            try:
                import torch

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    torch.cuda.synchronize()
            except ImportError:
                pass

    def vram_info(self) -> dict:
        """返回显存使用情况（供健康检查与监控看板）。"""
        try:
            import torch

            if not torch.cuda.is_available():
                return {"available": False, "device": "cpu"}
            free, total = torch.cuda.mem_get_info()
            return {
                "available": True,
                "device": torch.cuda.get_device_name(0),
                "total_gb": round(total / 1024**3, 2),
                "used_gb": round((total - free) / 1024**3, 2),
                "free_gb": round(free / 1024**3, 2),
                "torch_allocated_gb": round(torch.cuda.memory_allocated() / 1024**3, 2),
            }
        except ImportError:
            return {"available": False, "device": "unknown", "reason": "torch 未安装"}

    def status(self) -> dict:
        """完整的模型状态（健康检查用）。"""
        return {
            "device": self.device,
            "embedding": {
                "name": self._embedder.name,
                "path": self._embedder.path,
                "loaded": self._embedder.instance is not None,
                "load_seconds": round(self._embedder.load_seconds, 2),
                "path_exists": Path(self._embedder.path).is_dir(),
                "last_error": self._embedder.last_error,
            },
            "reranker": {
                "name": self._reranker.name,
                "path": self._reranker.path,
                "loaded": self._reranker.instance is not None,
                "load_seconds": round(self._reranker.load_seconds, 2),
                "path_exists": Path(self._reranker.path).is_dir(),
                "last_error": self._reranker.last_error,
            },
            "vram": self.vram_info(),
        }


registry = ModelRegistry()
