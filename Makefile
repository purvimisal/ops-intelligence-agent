.PHONY: dev test eval ingest lint stop clean detect

PYTHON := .venv/bin/python

dev:
	docker-compose up -d
	@echo "Stack running. Postgres: 5432, Redis: 6379, Grafana: http://localhost:3000"

stop:
	docker-compose down

clean:
	docker-compose down -v

test:
	$(PYTHON) -m pytest tests/ -v --tb=short

eval:
	$(PYTHON) -m pytest evals/ -v --tb=short

ingest:
	$(PYTHON) -m app.cli ingest-runbooks

detect:
	$(PYTHON) -m app.cli detect $(SCENARIO)

lint:
	$(PYTHON) -m ruff check app/ tests/ evals/
	$(PYTHON) -m ruff format --check app/ tests/ evals/
