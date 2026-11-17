"""文件存储抽象层。

背景：MinIO 已于 2025-10 撤下官方 Docker 镜像（社区版停止分发），
     因此本项目默认使用本地文件系统存储，但保留 S3 实现以便将来替换。

设计：业务代码只依赖 StorageBackend 接口，通过 get_storage() 获取实例。
     切换后端只需改 .env 中的 STORAGE_BACKEND，无需改动业务代码。

为什么不直接写文件：文献原文与影像需要按类别分目录、生成哈希文件名、
     支持后续迁移到对象存储。统一接口让这些需求收敛在一处。
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from app.core.config import PROJECT_ROOT, settings
from app.core.logging import get_logger

logger = get_logger("services.storage")


@dataclass
class StoredFile:
    """存储结果。"""

    key: str          # 存储键（相对路径）
    size: int         # 字节数
    sha256: str       # 内容哈希，用于去重
    backend: str      # local / s3


class StorageBackend(ABC):
    """存储后端接口。"""

    name: str = "base"

    @abstractmethod
    def save(self, data: bytes, key: str) -> StoredFile:
        """保存文件内容。key 为相对路径，如 'literature/2024/xxx.pdf'。"""

    @abstractmethod
    def read(self, key: str) -> bytes:
        """读取文件内容。"""

    @abstractmethod
    def delete(self, key: str) -> bool:
        """删除文件，返回是否存在。"""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """判断文件是否存在。"""

    @abstractmethod
    def url_for(self, key: str) -> str:
        """返回可访问的 URL（本地后端返回 API 下载路径）。"""

    def health(self) -> tuple[bool, str]:
        """健康检查。"""
        return True, f"{self.name} 存储可用"


class LocalStorage(StorageBackend):
    """本地文件系统存储。

    默认根目录: <项目根>/data/storage
    """

    name = "local"

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else PROJECT_ROOT / "data" / "storage"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        """把 key 解析为绝对路径，并防止路径穿越。"""
        target = (self.root / key).resolve()
        if not target.is_relative_to(self.root.resolve()):
            raise ValueError(f"非法存储键（路径穿越）: {key}")
        return target

    def save(self, data: bytes, key: str) -> StoredFile:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        logger.debug("已保存文件 %s (%d 字节)", key, len(data))
        return StoredFile(key=key, size=len(data), sha256=digest, backend=self.name)

    def read(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> bool:
        path = self._path(key)
        if path.exists():
            path.unlink()
            return True
        return False

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def url_for(self, key: str) -> str:
        return f"{settings.app.api_prefix}/files/{key}"

    def health(self) -> tuple[bool, str]:
        try:
            probe = self.root / ".health_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            # 统计已用空间，便于监控
            total = sum(f.stat().st_size for f in self.root.rglob("*") if f.is_file())
            return True, f"本地存储可用 / {self.root} / 已用 {total / 1024**2:.1f} MB"
        except Exception as exc:  # noqa: BLE001
            return False, f"本地存储不可用: {type(exc).__name__}: {exc}"


class MinioStorage(StorageBackend):
    """MinIO / S3 兼容存储（备用实现）。

    当前 MinIO 官方镜像已下架，如将来使用自建 MinIO 或其他 S3 服务，
    在 .env 中设置 STORAGE_BACKEND=s3 并填写连接信息即可启用。
    """

    name = "s3"

    def __init__(self) -> None:
        from minio import Minio

        self._client = Minio(
            settings.minio.minio_endpoint,
            access_key=settings.minio.minio_user,
            secret_key=settings.minio.minio_password,
            secure=settings.minio.minio_secure,
        )
        self._bucket = settings.minio.minio_bucket_docs
        if not self._client.bucket_exists(self._bucket):
            self._client.make_bucket(self._bucket)
            logger.info("已创建存储桶 %s", self._bucket)

    def save(self, data: bytes, key: str) -> StoredFile:
        import io

        digest = hashlib.sha256(data).hexdigest()
        self._client.put_object(
            self._bucket, key, io.BytesIO(data), length=len(data)
        )
        return StoredFile(key=key, size=len(data), sha256=digest, backend=self.name)

    def read(self, key: str) -> bytes:
        resp = self._client.get_object(self._bucket, key)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    def delete(self, key: str) -> bool:
        try:
            self._client.remove_object(self._bucket, key)
            return True
        except Exception:  # noqa: BLE001
            return False

    def exists(self, key: str) -> bool:
        try:
            self._client.stat_object(self._bucket, key)
            return True
        except Exception:  # noqa: BLE001
            return False

    def url_for(self, key: str) -> str:
        return f"{settings.app.api_prefix}/files/{key}"

    def health(self) -> tuple[bool, str]:
        try:
            buckets = [b.name for b in self._client.list_buckets()]
            return True, f"S3 可用 / 存储桶: {buckets}"
        except Exception as exc:  # noqa: BLE001
            return False, f"S3 不可用: {type(exc).__name__}: {exc}"


_storage: StorageBackend | None = None


def get_storage() -> StorageBackend:
    """获取存储后端单例。"""
    global _storage
    if _storage is None:
        _storage = LocalStorage()
        logger.info("存储后端: %s -> %s", _storage.name, getattr(_storage, "root", "-"))
    return _storage


def check_storage() -> tuple[bool, str]:
    """存储健康检查（供 /health 使用）。"""
    try:
        return get_storage().health()
    except Exception as exc:  # noqa: BLE001
        return False, f"存储检查失败: {type(exc).__name__}: {exc}"
