.PHONY: dev test eval ingest lint stop clean detect serve \
        docker-build docker-run deploy deploy-logs

PYTHON := .venv/bin/python
IMAGE  := ops-intelligence-agent

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

serve:
	$(PYTHON) -m uvicorn app.api.main:app --reload --port 8000

lint:
	$(PYTHON) -m ruff check app/ tests/ evals/
	$(PYTHON) -m ruff format --check app/ tests/ evals/

docker-build:
	docker build -t $(IMAGE) .

docker-run:
	docker run --rm -p 8000:8000 \
		--env-file .env \
		--add-host=host.docker.internal:host-gateway \
		$(IMAGE)

deploy:
	fly deploy
	@echo ""
	@echo "Live: https://ops-intelligence-agent.fly.dev/api/v1/health"

deploy-logs:
	fly logs --tail
