.PHONY: install install-dev infra-up infra-down dev-api dev-ui test lint format

install:
	python3 -m pip install -r requirements.txt

install-dev:
	python3 -m pip install -r requirements-dev.txt

infra-up:
	bash scripts/docker-up.sh

infra-down:
	docker compose -f compose.yaml down

dev-api:
	uvicorn main:app --host 0.0.0.0 --port 8000 --reload

dev-ui:
	streamlit run frontend/app.py

test:
	pytest

lint:
	ruff check .

format:
	ruff format .
