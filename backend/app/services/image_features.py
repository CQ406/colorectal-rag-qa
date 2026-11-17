"""无额外模型依赖的 512 维图像特征。

这是可复现的颜色直方图基线，用于教学样例的相似影像检索；生产环境可将
实现替换为 CLIP 或院内视觉编码器，而不改变数据库与 API 契约。
"""

from __future__ import annotations

import io
import math

FEATURE_VERSION = 'histogram-rgb8-v1'
FEATURE_DIMENSION = 512


def extract_feature(data: bytes) -> list[float]:
    """计算 RGB 8x8x8 直方图并 L2 归一化为 512 维向量。"""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGB")
        # 缩小只为控制内存和运行时间，不改变直方图维度。
        image.thumbnail((512, 512))
        pixels = image.tobytes()
    histogram = [0.0] * 512
    if not pixels:
        return histogram
    for offset in range(0, len(pixels), 3):
        red, green, blue = pixels[offset:offset + 3]
        index = (red // 32) * 64 + (green // 32) * 8 + (blue // 32)
        histogram[index] += 1.0
    scale = math.sqrt(sum(value * value for value in histogram)) or 1.0
    return [value / scale for value in histogram]
