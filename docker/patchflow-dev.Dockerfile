FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        git \
        curl \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace/PatchFlow

# 先复制最小安装元数据，提升重复 build 的缓存命中率。
COPY pyproject.toml README.md USAGE.md /workspace/PatchFlow/
COPY agent /workspace/PatchFlow/agent
COPY config /workspace/PatchFlow/config
COPY context /workspace/PatchFlow/context
COPY entry /workspace/PatchFlow/entry
COPY llm /workspace/PatchFlow/llm
COPY tools /workspace/PatchFlow/tools

RUN python -m pip install --upgrade pip \
    && python -m pip install -e .

CMD ["bash"]
