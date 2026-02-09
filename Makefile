.PHONY: dev test lint typecheck deploy clean

dev:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test:
	DATABASE_URL=sqlite:///:memory: python -m pytest tests/ -v --tb=short

lint:
	ruff check app/

typecheck:
	mypy app/ --ignore-missing-imports --no-strict-optional --allow-untyped-defs

deploy:
	git push origin main

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
