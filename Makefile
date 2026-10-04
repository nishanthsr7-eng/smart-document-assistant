.PHONY: up down logs lint test

up:
	docker compose --profile app up -d --build

down:
	docker compose --profile app down

logs:
	docker compose --profile app logs -f api worker

lint:
	ruff check .
	mypy src

test:
	pytest -q --cov=src --cov-fail-under=75
	cd frontend && npm test
