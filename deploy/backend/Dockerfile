# backend 生产镜像（docs/sandbox-query.md §12 + wayfinder T06 spike 骨架）。
# 构建位置 = 沙箱 Engine（Ubuntu2204 内 docker-ce）；compose 已按此装配
# （deploy/compose/docker-compose.yml：context=仓库根，dockerfile=本文件）。
#
# 与 spike 原型（分支 spike/backend-container 的 Dockerfile.spike）的差异：
#   - uv.lock 已刷新（坑1 关闭）→ 直接 uv sync --frozen，无需补装；
#   - aiomysql / langmem 已升入主依赖（坑2 关闭）；
#   - 加装 git：RepoSource 匿名 clone/fetch 在 backend 容器内执行（spec §3），
#     git_query 沙箱容器本身用 yuan-sandbox-git 镜像（deploy/sandbox/Dockerfile）；
#   - config/agent.yaml 被 .dockerignore 排除（密钥不进镜像层），由 compose 单文件挂载。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
    UV_LINK_MODE=copy

RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends tzdata curl git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple uv

WORKDIR /app
# 依赖层：pyproject + uv.lock 变动才触发重装
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY backend/ ./backend/
# agent.yaml 已被 .dockerignore 排除，仅 example 兜底（真实配置由 compose 挂载）
COPY config/ ./config/
COPY scripts/mock_mcp_server.py ./scripts/mock_mcp_server.py

# 非 root 运行；对 docker.sock 的组访问由 compose group_add 注入 Engine 侧 GID
RUN useradd -m -u 10001 appuser \
 && mkdir -p /app/data /app/log \
 && chown -R appuser:appuser /app
USER appuser

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH=/app/backend

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
