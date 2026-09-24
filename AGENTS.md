# AGENTS.md

Instructions for AI agents working in this repo. Read before editing.

## What this is
h4ks workflows is a storefront and queue for workflows that run elsewhere. It sells job types for credits (bought with beans, plus a daily free allowance), keeps one global queue where exactly one job runs at a time, dispatches each job to its executor, and shows progress from the executor's callbacks. It knows nothing about how a job is done: an executor is any HTTP endpoint that accepts the dispatch and reports `step`, `log`, `result` or `error` events back. Job types (their form, price expression, steps and executor) come from providers, which the catalog merges.

## Where things live
- App factory (routers, middleware, error handlers, lifespan tasks): `src/workflows/app.py`
- Settings, one module that reads every environment variable: `src/workflows/settings.py` (all listed in `.env.example`)
- Database models and engine (SQLAlchemy, `create_all` plus additive column migrations on startup): `src/workflows/db.py`
- Credits ledger (daily free grant, reserve, capture, refund, top-up, admin adjust): `src/workflows/ledger.py`
- Job type catalog, the `Provider` and `Executor` interfaces, the HTTP executor and the in-memory provider: `src/workflows/catalog.py`
- Generic forms (field specs, strict validation, JSON schema) and the JSON Schema form driver: `src/workflows/forms.py`
- Price expressions: `src/workflows/pricing.py`; media probing: `src/workflows/probe.py`
- Built-in job types served by the in-memory provider: `src/workflows/builtin.py`
- Job lifecycle and the executor callback contract: `src/workflows/jobs.py`
- Queue worker (dispatch, timeout watchdog, pause): `src/workflows/worker.py`
- Job status webhooks: `src/workflows/webhooks.py`
- ETAs from rolling averages: `src/workflows/eta.py`
- In-process event bus per job and for the queue: `src/workflows/bus.py`
- Auth dependencies (session user, admin, service token, CSRF): `src/workflows/auth.py`
- Shared app services and the DB session dependency: `src/workflows/state.py`
- JSON API under `/api`: `src/workflows/api.py` with its response models in `src/workflows/views.py`, SSE streams: `src/workflows/stream.py`, read-only MCP at `/mcp`: `src/workflows/mcp.py`
- Login (Logto OIDC, dev login): `src/workflows/login.py`; account, wallet and top-ups: `src/workflows/account.py`; Beans top-up poller: `src/workflows/beans.py`
- Client-facing job submission and identity linking, confirm and link pages: `src/workflows/clients.py`; admin API: `src/workflows/admin.py`
- Object storage for admin removal of generated files (MinIO): `src/workflows/storage.py`
- Web pages: `src/workflows/web.py` with `webviews.py` (templates, page context, rendering, CSRF form helper), `templates/` and `static/`
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
- Credits are integers. 1 bean is 100 credits. Every balance change goes through `ledger.py`, which writes a ledger entry and updates the cached balance on the user in the same transaction. The ledger changes balances with SQL increments and rereads the user before every check.
- Never `await` between reading a balance and committing. The app shares one event loop, so another request can change the balance at any `await`.
- The daily free allowance sets the free balance to 500 on the first action of a UTC day and is spent before paid credits.
- A job pays a fixed quote from its type's formula. Credits are reserved when the job is queued, captured on success, and refunded on failure or cancellation.
- The rest of the app reads job types only through `Catalog` (`get`, `find`, `all`). The catalog asks every `Provider` to `discover()` its job types on startup and every 60 seconds, keeps the first type of each name, and keeps a provider's last good list when it raises `ProviderError`. An unknown name resolves to a retired type.
- A `JobType` carries its `Form`, a `PriceRule`, its steps and an optional `Executor`. A type without an executor is listed as unavailable and refuses quotes and submissions. The form is the single source for the web form, the JSON schema in the API and MCP, and strict validation; a field's `show_when` also means it is only accepted when that condition holds.
- Prices are expressions over the validated params: numbers, field names, `+ - * /`, comparisons, `a if cond else b` and `duration(field)`, which probes that field's media. The estimate in seconds equals the price.
- With `N8N_API_KEY` set, the n8n provider (`src/workflows/n8n.py`) serves every active n8n workflow tagged `h4ks-workflows`, read through the n8n public API with a read-only key. Each needs an enabled Webhook node (its path is the executor URL), a Form Trigger node (usually disabled) whose title, description and fields define the form, and a sticky note with a fenced `h4ks-workflows` JSON block: a required `price`, and optional `name` (defaults to the webhook path without `workflows-`), `pricing`, `steps`, `position` and per-field `fields` overrides (`description`, `type`, `minimum`, `maximum`, `min_length`, `max_length`, `show_when`). A field's placeholder is its help text. A workflow that breaks these rules is skipped, logged, and listed on the admin page with the reason. `n8n/README.md` is the guide for building such a workflow; keep it in step with `src/workflows/n8n.py`. Without the key, the built-in provider serves the types in `builtin.py` with an `HttpExecutor` that posts to `N8N_URL/webhook/<webhook path>`.
- Executor dispatch carries the `X-API-Key` header and a JSON body with `job_id`, `type`, `params`, `steps` (step names in order), `callback_url` and `callback_token`. Executors call back with the per-job bearer token; we store only its hash.
- Executor contract: accept the dispatch with a 2xx and send the first `step` event right away. A 4xx/5xx answer or a refused connection fails the job and refunds it. A dispatch timeout leaves the job running, and a job with no event within `FIRST_EVENT_TIMEOUT` seconds fails and is refunded. Executors retry callbacks: a repeated `result` after success or `error` after failure gets 204, any other event after the job ended gets 409, which includes every callback for a cancelled job.
- Lifecycle changes publish on the event bus after the commit, for live views.
- A job submitted through `POST /api/clients/jobs` can carry a `webhook` (`url`, `token`, `extra_params`, `message_prefix`). When the job starts, succeeds, fails or is cancelled, `webhooks.py` posts JSON with `Authorization: Bearer <token>`: the `extra_params`, then `message` (the prefix plus a short plain sentence), `job_id`, `type`, `status`, `run_url` and `result_urls` (file URLs on success). Delivery is best-effort: it retries connection errors, timeouts and 5xx three times with backoff, gives up on 4xx, and never affects the job.
- A trusted client can `PUT /api/clients/subscription` with `url` and `signing_key` (upserted by `url`, removed with `DELETE /api/clients/subscription?url=...`) to get a signed raw event for every paid job when it is queued, starts, succeeds, fails or is cancelled. The body is `event`, `job_id`, `type`, `type_title`, `status`, `owner`, `title` (the result title), `run_url`, `result_urls`, `error`, with header `X-Webhook-Signature: sha256=<hex>` where `hex` is `HMAC-SHA256(signing_key, json.dumps(payload, sort_keys=True))`. Same retry and logging rules as the per-job webhook.
- The queue pause flag lives in memory, so a restart resumes the queue.
- The production database is never reset. On startup, any column declared on a model but missing from an existing table is added with `ALTER TABLE ... ADD COLUMN`. A new column must be nullable or have a server default, since it lands on rows that already exist.
- An admin can remove a job's generated files (`POST /api/admin/jobs/{id}/remove`). It deletes every result file URL and the metadata URL from object storage (any bucket on the MinIO host, configured by `MINIO_ENDPOINT`/`MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY`/`MINIO_USE_SSL`), clears `Job.result` and sets `Job.removed_at`. A removed job's result is replaced everywhere with "removed by an admin".
- Logged-in browser calls that change state under `/api` send `X-Requested-With: fetch`; requests with a bearer token are exempt. Request bodies are capped at 256 KB.
- Prose (docs, commits): declarative, terse, no em dashes.

## Hygiene
Never commit tokens or databases. The `.gitignore` excludes `.env`, `data/`, `*.db` and the `.rev-ok` marker.
