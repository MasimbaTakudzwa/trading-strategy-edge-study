.PHONY: help install up down db-init logs test lint format paper-report clean

help:
	@echo "Trading bot — common commands"
	@echo ""
	@echo "  make install      Install Python deps via uv (or pip fallback)"
	@echo "  make up           Start Postgres + Redis containers"
	@echo "  make down         Stop containers"
	@echo "  make db-init      Run schema migrations against the DB"
	@echo "  make logs         Tail container logs"
	@echo "  make test         Run pytest"
	@echo "  make lint         Run ruff"
	@echo "  make format       Format with ruff"
	@echo ""
	@echo "  make paper        Start paper trading (uses OANDA_ENV=practice)"
	@echo "  make live         Start LIVE trading (requires confirmation prompt)"
	@echo "  make status       Show open positions + today's P&L"
	@echo "  make paper-report Print paper-trade performance summary"

install:
	@command -v uv >/dev/null 2>&1 || { echo "Installing uv..."; curl -LsSf https://astral.sh/uv/install.sh | sh; }
	uv sync --extra dev --extra backtest

up:
	@docker info >/dev/null 2>&1 || { echo "Docker not running. Launching Docker Desktop..."; open -a Docker; }
	@echo "Waiting for Docker daemon (up to 120s)..."
	@n=0; until docker info >/dev/null 2>&1; do n=$$((n+1)); if [ $$n -gt 120 ]; then echo "Docker daemon never came up. Is Docker Desktop installed?"; exit 1; fi; sleep 1; done
	docker compose up -d
	@echo "Waiting for Postgres..."
	@until docker compose exec -T postgres pg_isready -U $${POSTGRES_USER:-tbot} >/dev/null 2>&1; do sleep 1; done
	@echo "Ready."

down:
	docker compose down

db-init:
	uv run tbot db init

logs:
	docker compose logs -f --tail=100

test:
	uv run pytest

lint:
	uv run ruff check src tests

format:
	uv run ruff format src tests
	uv run ruff check --fix src tests

paper:
	OANDA_ENV=practice uv run tbot paper --strategy donchian --instrument EUR_USD

live:
	@echo "Make sure you really mean this. Set OANDA_ENV=live in .env and confirm at the prompt."
	OANDA_ENV=live uv run tbot live --strategy donchian --instrument EUR_USD

status:
	uv run tbot status

paper-report:
	uv run tbot report --env practice

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov dist build
	find . -type d -name __pycache__ -exec rm -rf {} +
