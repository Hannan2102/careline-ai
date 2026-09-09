# CareLine AI - developer commands.
# Defaults are text-only and cost nothing; paid providers require opt-in.

PY := .venv/bin/python
PIP := uv pip install --python .venv/bin/python
COMPOSE := docker compose

.DEFAULT_GOAL := help
.PHONY: help install up down logs reset seed wait-fhir dev chat dashboard dashboard-install dashboard-check test test-int lint fmt typecheck check budget smoke-cloud smoke-voice voice clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Create the venv and install backend dependencies
	uv venv --python 3.12 .venv
	VIRTUAL_ENV=.venv $(PIP) -e ".[dev,db]"
	@echo "Deepgram streaming also needs: uv pip install --python .venv/bin/python '.[cloud]'"
	@echo "Done. Copy .env.example to .env, then run 'make dev'."

up: ## Start PostgreSQL and HAPI FHIR (follow with 'make wait-fhir')
	$(COMPOSE) up -d postgres hapi-fhir
	@echo "Started. HAPI takes ~20-60s to build its schema; run 'make wait-fhir'."

down: ## Stop infrastructure (keeps data)
	$(COMPOSE) down

logs: ## Tail infrastructure logs
	$(COMPOSE) logs -f postgres hapi-fhir

reset: ## Destroy volumes, restart, and reseed from scratch
	$(COMPOSE) down -v
	$(COMPOSE) up -d postgres hapi-fhir
	$(MAKE) wait-fhir
	EHR_PROVIDER=local $(PY) scripts/seed_data.py --provider local

wait-fhir: ## Block until the FHIR server answers /metadata
	$(PY) scripts/wait_for_fhir.py

seed: ## Load synthetic data into the configured EHR
	$(PY) scripts/seed_data.py

dev: ## Run the backend API (http://localhost:8000/docs)
	$(PY) -m uvicorn app.main:app --reload --app-dir backend --port 8000

chat: ## Talk to the agent in the terminal (ARGS="--script refill --trace")
	$(PY) scripts/text_chat.py $(ARGS)

dashboard-install: ## Install dashboard dependencies
	cd frontend && npm install

dashboard: ## Run the admin dashboard (http://localhost:3000; needs 'make dev' too)
	cd frontend && npm run dev

dashboard-check: ## Lint, type-check, and build the dashboard
	cd frontend && npm run lint && npm run typecheck && npm run build

test: ## Run the test suite (no network, no cost)
	AI_MODE=mock $(PY) -m pytest -m "not integration and not paid"

test-int: ## Run integration tests against a running HAPI FHIR server
	EHR_PROVIDER=local $(PY) -m pytest -m integration

lint: ## Lint with ruff
	.venv/bin/ruff check backend tests scripts

fmt: ## Format with ruff
	.venv/bin/ruff format backend tests scripts
	.venv/bin/ruff check --fix backend tests scripts

typecheck: ## Type-check with mypy
	.venv/bin/mypy

check: lint typecheck test ## Lint, type-check, and test

budget: ## Print estimated API spend and remaining budget
	$(PY) scripts/budget_status.py

voice: ## Run the API and dashboard configured for voice in the browser
	@echo "Backend: make dev   Dashboard: make dashboard   then open /voice"
	@echo "Needs TEXT_ONLY_MODE=false, STT_ENABLED=true, TTS_ENABLED=true in .env."

smoke-voice: ## Drive one whole voice call and save the audio to listen to (free)
	@echo "Scripted speech in, real Groq speech out. Play smoke_voice.wav afterwards."
	TEXT_ONLY_MODE=false TTS_ENABLED=true TTS_PROVIDER=groq \
	  STT_ENABLED=true STT_PROVIDER=mock $(PY) scripts/smoke_voice.py

smoke-cloud: ## ONE live paid call to verify a cloud adapter (costs money)
	@echo "Contacts a real vendor. PROVIDER=<groq|openai|elevenlabs|deepgram>."
	$(PY) scripts/smoke_cloud.py --provider $(PROVIDER) --confirm-spend

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache dist build
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
