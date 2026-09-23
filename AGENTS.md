# AGENTS.md

Instructions for AI agents working in this repo. Read before editing.

## What this is
h4ks workflows: a paid job service for the h4ks community. Users order AI media jobs (parody, song, voice swap, podcast episode), pay a fixed quote in credits, and wait in one global queue where exactly one job runs at a time. Executors (n8n webhooks) do the work and report progress back through callbacks.

## Where things live
- App factory (routers, middleware, error handlers, worker lifespan): `src/workflows/app.py`
- Settings, one module that reads every environment variable: `src/workflows/settings.py` (all listed in `.env.example`)
- Database models and engine (SQLAlchemy, `create_all` on startup): `src/workflows/db.py`
- Credits ledger (daily free grant, reserve, capture, refund, top-up, admin adjust): `src/workflows/ledger.py`
- Job type registry, params models, quotes, probing: `src/workflows/jobtypes.py`
- Job lifecycle and the executor callback contract: `src/workflows/jobs.py`
- Queue worker (dispatch, timeout watchdog, pause): `src/workflows/worker.py`
- ETAs from rolling averages: `src/workflows/eta.py`
- In-process event bus per job and for the queue: `src/workflows/bus.py`
- Auth dependencies (session user, admin, service token): `src/workflows/auth.py`
- Shared app services and the DB session dependency: `src/workflows/state.py`
- JSON API under `/api`: `src/workflows/api.py`
- The `Makefile` is the single canonical interface for all checks; CI and pre-commit both call it.

## Stack
- Python 3.14, managed with `uv`
- FastAPI, SQLAlchemy 2 on SQLite (single replica), httpx for outbound calls, Pydantic models for every API shape

## Commands (Makefile is SSoT)
- `make install` uv sync plus install pre-commit hooks
- `make quality` the full gate: format-check, lint, typecheck, imports, dead-code, unused-deps, security, audit, coverage, build
- `make run` serve the app with reload, reading `.env`
- `make docker-build` / `make docker-run` build and run the image with `./data` as the SQLite volume

## Before considering work complete
1. Run `make quality`.
2. Fix all failures.
3. Do not weaken or remove quality checks to make them pass.
4. Do not lower coverage requirements.
5. Do not leave unused dependencies or dead code.

## Code style
- Modern static type hints for Python: `dict` and `list` (not `Dict`/`List`), `X | None` (not `Union`/`Optional`). PEP 695 generics (`def f[T]()`). Compatible with mypy strict. Type hints required on function/method signatures and arguments, not local variables.
- Imports only at the top of a module, never inside functions.
- No `Any` and no bare `object` as a type annotation. Use dataclasses, Pydantic models, or concrete types.
- Every API model field has `Field(description=...)`.
- Low complexity, enforced by ruff: cyclomatic complexity 10, at most 10 branches, 6 returns, 40 statements and 6 arguments per function. Split a function that trips these into well-named smaller ones; never raise the limits or add `noqa`.
- Comments only to explain the non-obvious why, never the what. Default to zero.
- Descriptive, explicit variable names. Keep solutions short and simple.
- No bare `except`. Catch only the exceptions actually expected.

## Conventions
- Credits are integers. 1 bean is 100 credits. Every balance change goes through `ledger.py`, which writes a ledger entry and updates the cached balance on the user in the same transaction.
- The daily free allowance sets the free balance to 500 on the first action of a UTC day and is spent before paid credits.
- A job pays a fixed quote from its type's formula. Credits are reserved when the job is queued, captured on success, and refunded on failure or cancellation.
- Job types live in code in `jobtypes.py`. A type whose `EXECUTOR_URL_<TYPE>` is empty is listed as unavailable and refuses quotes and orders.
- Executor dispatch carries the `X-API-Key` header. Executors call back with the per-job bearer token; we store only its hash.
- Lifecycle changes publish on the event bus after the state change, for live views.
- Prose (docs, commits): declarative, terse, no em dashes.

## Hygiene
Never commit tokens or databases. The `.gitignore` excludes `.env`, `data/`, `*.db` and the `.rev-ok` marker.
