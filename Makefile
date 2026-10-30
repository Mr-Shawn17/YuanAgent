# 启动前后端推荐直接用 Python 命令（端口与 Python 环境从 config/agent.yaml 的 server 段读取）：
#   python scripts/start_backend.py    # 后端 API
#   python scripts/start_frontend.py   # 前端
# 本 Makefile 的 dev/web 目标已等价封装这两个脚本；install/lint/test 等开发工具链仍走 Makefile。
# 默认用项目内 .venv312（基于 py312 + --system-site-packages）；
# 若你已把依赖直接装进 py312，可用 make PY=D:/software/miniconda3/envs/py312/python.exe <目标> 覆盖
PY ?= .venv312/Scripts/python.exe
PIP_INDEX ?= https://pypi.tuna.tsinghua.edu.cn/simple
# .venv312 继承 py312 的 site-packages，pytest 断言重写缓存会尝试写入受保护目录，故全局禁写字节码
export PYTHONDONTWRITEBYTECODE := 1

.PHONY: help install lint format check typecheck test test-online test-mysql eval chat sessions db-init dev web up down clean

help:
	@echo "install      - 安装全部依赖（dev/tools/memory）到 PY 指定的解释器"
	@echo "lint         - ruff 检查并自动修复"
	@echo "format       - ruff 格式化"
	@echo "typecheck    - mypy 严格模式"
	@echo "test         - 离线测试（FakeChatModel，排除 online 与 mysql）"
	@echo "test-online  - 需要真实 API Key 的测试"
	@echo "test-mysql   - 需要真实 MySQL 实例的测试"
	@echo "check        - lint + typecheck + test"
	@echo "db-init      - 幂等建库 + 建表（LangGraph checkpoint/store）"
	@echo "chat         - 终端聊天"
	@echo "sessions     - 列出历史会话"
	@echo "eval         - RAG 金标准回归（需先 python scripts/rag/serve_mcp.py 起服务）"
	@echo "dev          - 启动后端 API（热重载，读取 config/agent.yaml 的 server 段）"
	@echo "web          - 启动前端（读取 config/agent.yaml 的 server 段）"

install:
	$(PY) -m pip install -i $(PIP_INDEX) -e ".[dev,tools,memory]"

lint:
	$(PY) -m ruff check . --fix

format:
	$(PY) -m ruff format .

typecheck:
	$(PY) -m mypy backend

test:
	$(PY) -m pytest -q -m "not online and not mysql and not rag_service"

test-online:
	$(PY) -m pytest -q -m online

test-mysql:
	$(PY) -m pytest -q -m mysql

eval:
	$(PY) -m pytest tests/eval -q -m rag_service -s

check: lint typecheck test

db-init:
	$(PY) scripts/init_mysql.py

chat:
	$(PY) -m app.cli chat

sessions:
	$(PY) -m app.cli sessions

dev:
	$(PY) scripts/start_backend.py

web:
	$(PY) scripts/start_frontend.py

clean:
	$(PY) -c "import shutil,pathlib;[shutil.rmtree(p,ignore_errors=True) for p in [pathlib.Path('.mypy_cache'),pathlib.Path('.pytest_cache'),pathlib.Path('.ruff_cache')]]"
