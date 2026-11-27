.PHONY: install up down migrate backfill substreams-backfill backfill-svr-liquidations \
        index snapshot signals backtest api \
        test lint contracts-test reset fmt typecheck reconcile gen-proto

# ── Setup ─────────────────────────────────────────────────────────────────────
install:
	uv pip install -e ".[dev]"

# ── Infrastructure ────────────────────────────────────────────────────────────
up:
	docker compose up -d postgres
	@echo "Waiting for Postgres..."
	@until docker compose exec postgres pg_isready -U risk 2>/dev/null; do sleep 1; done
	@echo "Postgres ready."

down:
	docker compose down

# Full stack (all services)
stack:
	docker compose up -d

# ── Database ──────────────────────────────────────────────────────────────────
migrate:
	python -m risk.db.migrate

reset:
	@echo "Dropping and recreating database..."
	docker compose exec postgres psql -U risk -c "DROP DATABASE IF EXISTS risk;"
	docker compose exec postgres psql -U risk -c "CREATE DATABASE risk;"
	$(MAKE) migrate

# ── Data pipeline ─────────────────────────────────────────────────────────────
backfill:
	python -m risk.cmd.backfill

backfill-svr-liquidations:
	python -m risk.cmd.backfill_svr_liquidations

substreams-backfill:
	python -m risk.cmd.substreams_backfill

gen-proto:
	python -m grpc_tools.protoc -I proto \
	  -I "$$(python -c 'import grpc_tools,os; print(os.path.dirname(grpc_tools.__file__))')/_proto" \
	  --python_out=risk/proto --grpc_python_out=risk/proto \
	  proto/sf/substreams/rpc/v2/service.proto \
	  proto/sf/substreams/v1/clock.proto \
	  proto/sf/substreams/v1/modules.proto \
	  proto/sf/ethereum/type/v2/type.proto

index:
	python -m risk.cmd.index

snapshot:
	python -m risk.cmd.snapshot

signals:
	python -m risk.cmd.signals

backtest:
	python -m risk.backtest.runner --days 90

reconcile:
	python -m risk.cmd.reconcile

# ── API ───────────────────────────────────────────────────────────────────────
api:
	uvicorn risk.api.app:app --host 0.0.0.0 --port 8000 --reload

# ── Quality ───────────────────────────────────────────────────────────────────
lint:
	ruff check risk/ tests/
	ruff format --check risk/ tests/

fmt:
	ruff format risk/ tests/
	ruff check --fix risk/ tests/

typecheck:
	mypy risk/ --ignore-missing-imports

test:
	pytest tests/ -v --tb=short

# ── Solidity ──────────────────────────────────────────────────────────────────
contracts-test:
	cd contracts && forge test -vv

contracts-build:
	cd contracts && forge build

# ── Composite ────────────────────────────────────────────────────────────────
ci: lint typecheck test contracts-test

all: install up migrate backfill snapshot signals api
