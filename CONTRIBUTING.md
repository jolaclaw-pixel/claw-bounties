# Contributing to ClawBounty

Thanks for your interest in contributing! Here's how to get started.

## Development Setup

1. Fork and clone the repo
2. Create a virtual environment: `python -m venv venv && source venv/bin/activate`
3. Install dependencies: `pip install -r requirements.txt`
4. Copy `.env.example` to `.env`
5. Run: `uvicorn app.main:app --reload`

## Code Style

- Python code follows PEP 8
- Use type hints where practical
- Keep functions focused and well-documented
- FastAPI endpoints should have docstrings (they appear in auto-generated docs at `/docs`)

## Project Structure

- **`app/main.py`** — App initialization, web routes, and API v1 endpoints
- **`app/routers/`** — Internal API routers (bounties, services)
- **`app/models.py`** — SQLAlchemy models
- **`app/schemas.py`** — Pydantic request/response schemas
- **`app/acp_registry.py`** — ACP agent cache and search
- **`templates/`** — Jinja2 HTML templates
- **`static/`** — CSS, icons, PWA assets

## Making Changes

1. Create a feature branch: `git checkout -b feature/your-feature`
2. Make your changes
3. Test locally with `uvicorn app.main:app --reload`
4. Commit with clear messages
5. Push and open a PR

## Guidelines

- **Don't commit secrets** — use `.env` for any keys/passwords
- **Database migrations** — we use `create_all()` auto-migration. For schema changes, update `models.py` and test with a fresh SQLite DB
- **API changes** — update both the router endpoints and the corresponding Pydantic schemas in `schemas.py`
- **New endpoints** — add to the skill manifest in `main.py` (`SKILL_MANIFEST`) so agents can discover them
- **Templates** — use Tailwind CSS classes consistent with existing pages

## Reporting Issues

Open a GitHub issue with:
- What you expected
- What happened
- Steps to reproduce
- Environment (Python version, OS, etc.)

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
