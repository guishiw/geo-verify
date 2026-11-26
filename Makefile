# geo-verify 开发命令。首次需联网执行 `make setup`，其余目标均可断网运行。
SHELL := /bin/sh
BIN := .venv/bin

# 沙箱里默认的 uv 缓存目录可能不可写：探测失败时改用仓库内缓存目录。
ifeq ($(origin UV_CACHE_DIR),undefined)
ifneq ($(shell d="$${XDG_CACHE_HOME:-$$HOME/.cache}/uv"; mkdir -p "$$d" 2>/dev/null && touch "$$d/.geo-probe" 2>/dev/null && rm -f "$$d/.geo-probe" && echo ok),ok)
export UV_CACHE_DIR := $(CURDIR)/.uv-cache
endif
endif

.PHONY: help setup lint type test check perf format

help:
	@echo "make setup   安装依赖（需联网一次）"
	@echo "make lint    ruff 静态检查与格式检查"
	@echo "make type    mypy --strict（src）"
	@echo "make test    pytest + 覆盖率门槛"
	@echo "make check   lint + type + test"
	@echo "make perf    10 万条观测性能检查"

setup:
	uv sync --locked

$(BIN)/python:
	@echo "未找到 .venv，请先联网执行 make setup" >&2; exit 2

lint: $(BIN)/python
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

type: $(BIN)/python
	$(BIN)/mypy

test: $(BIN)/python
	$(BIN)/python -m pytest
	$(BIN)/python tools/check_coverage.py

check: lint type test

perf: $(BIN)/python
	$(BIN)/python -m pytest perf -p no:cacheprovider --no-cov -o testpaths=perf

format: $(BIN)/python
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .
