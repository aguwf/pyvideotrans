# ============================================================
# pyVideoTrans WebUI Dockerfile
#
# CPU:  docker build -t pyvideotrans-webui .
# GPU:  docker build --build-arg USE_CUDA=true -t pyvideotrans-webui:gpu .
# ============================================================

# 定义全局 ARG 变量
ARG USE_CUDA=false

# 巧妙地将阶段命名为 base-false 和 base-true
FROM python:3.10-slim AS base-false
FROM nvidia/cuda:12.8.0-cudnn-runtime-ubuntu22.04 AS base-true

# 根据 USE_CUDA 变量的值，动态继承上文对应的基础镜像
FROM base-${USE_CUDA} AS final-base

# 【关键】在新的 FROM 阶段之后，必须重新声明一次 ARG 才能在 RUN 等指令中使用该变量
ARG USE_CUDA

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV GRADIO_SERVER_NAME="0.0.0.0"
ENV GRADIO_SERVER_PORT=7860
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV FONTCONFIG_PATH=/etc/fonts

WORKDIR /app

# Distro ffmpeg (built with VA-API) + Intel media driver from non-free so the
# iGPU can encode burned-in subtitles; the static ffmpeg build has no VA-API.
RUN sed -i 's/^Components: main$/Components: main non-free non-free-firmware/' /etc/apt/sources.list.d/*.sources 2>/dev/null || true; \
    apt-get update && apt-get install -y --no-install-recommends \
    fontconfig fonts-noto-core fonts-noto-cjk fonts-liberation fonts-dejavu wget \
    xz-utils git libglib2.0-0 libgl1 libsm6 libxext6 libxrender-dev \
    libxkbcommon-x11-0 libdbus-1-3 libsndfile1 python3-dev rubberband-cli libsndfile1-dev \
    ffmpeg vainfo \
    && (apt-get install -y --no-install-recommends intel-media-va-driver-non-free || \
        apt-get install -y --no-install-recommends intel-media-va-driver) \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

ENV LIBVA_DRIVER_NAME=iHD

# Copy dependency specification files first for Docker layer caching
COPY pyproject.toml uv.lock .python-version* ./

# 修复丢失了变量的 if 语句，正确引用 "${USE_CUDA}"
RUN if [ "${USE_CUDA}" = "true" ]; then \
        echo ">>> CUDA" && \
        uv pip install --system -r pyproject.toml --all-extras && \
        uv pip install --system torch==2.7.1 torchaudio==2.7.1 --index-url https://download.pytorch.org/whl/cu128 && \
        uv pip install --system nvidia-cublas-cu12 nvidia-cudnn-cu12; \
    else \
        echo ">>> CPU" && \
        # Pre-install CPU PyTorch so that 'uv pip install -r pyproject.toml' sees torch as satisfied and does not download NVIDIA CUDA wheels \
        uv pip install --system torch==2.7.1 torchaudio==2.7.1 --index-url https://download.pytorch.org/whl/cpu && \
        uv pip install --system -r pyproject.toml --all-extras; \
    fi && \
    rm -rf /root/.cache/uv /tmp/*

# Downloader for the WebUI "paste a link" box (TikTok, Douyin, YouTube, ...)
RUN uv pip install --system -U yt-dlp && rm -rf /root/.cache/uv

# Copy application source code
COPY . .

EXPOSE 7860

CMD ["python", "webui.py"]
