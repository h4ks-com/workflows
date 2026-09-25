# AGENTS.md

Instructions for AI agents working in this repo. Read before editing.

## What this is
h4ks workflows is a storefront and queue for workflows that run elsewhere. It sells job types for credits (bought with beans, plus a daily free allowance), keeps one global queue where exactly one job runs at a time, dispatches each job to its executor, and shows progress from the executor's callbacks. It knows nothing about how a job is done: an executor is any HTTP endpoint that accepts the dispatch and reports `step`, `log`, `result` or `error` events back. Job types (their form, price expression, steps and executor) come from providers, which the catalog merges.

## Where things live
Code lives under `src/workflows/`, grouped by domain; `tests/` mirrors the same paths (`jobtypes/pricing.py` is tested in `tests/jobtypes/test_pricing.py`).
- Root: app factory (routers, middleware, error handlers, lifespan tasks) `app.py`; settings, the one module that reads the environment (all listed in `.env.example`) `settings.py`; database models and engine (`create_all` plus additive column migrations) `db.py`; shared app services and the DB session dependency `state.py`; MinIO object storage for admin removal `storage.py`
- `jobtypes/`: the catalog with the `Provider` and `Executor` interfaces and the HTTP executor `catalog.py`; generic forms and the JSON Schema form driver `forms.py`; price expressions `pricing.py`; media probing `probe.py`
- `jobtypes/providers/`: built-in job types `builtin.py`, tagged n8n workflows `n8n.py`, standalone services `services.py`
- `runs/`: job lifecycle and the executor callback contract `jobs.py`; queue worker (dispatch, timeout watchdog, pause) `worker.py`; job status webhooks `webhooks.py`; ETAs `eta.py`; in-process event bus `bus.py`
- `accounts/`: auth dependencies (session user, admin, service token, CSRF) `auth.py`; Logto login `login.py`; account, wallet and top-ups `account.py`; credits ledger `ledger.py`; Beans top-up poller `beans.py`
- `api/`: JSON API under `/api` `routes.py` with response models in `views.py`; SSE streams `stream.py`; read-only MCP at `/mcp` `mcp.py`; client job submission, identity linking and confirm pages `clients.py`; admin API `admin.py`
- `web/`: pages `pages.py`, templates and rendering helpers `render.py`, `templates/` and `static/`
- The `Makefile` is the single canonical interface for all checks; CI and the prek git hooks both call it.

## Stack
- Python 3.14, managed with `uv`
- FastAPI, SQLAlchemy 2 on SQLite (single replica), httpx for outbound calls, Pydantic models for every API shape

## Commands (Makefile is SSoT)
- `make install` uv sync plus install the git hooks with prek
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
- Imports only at the top of a module, never inside functions, one name per import line (ruff enforces it).
- No `Any` and no bare `object` as a type annotation. Use dataclasses, Pydantic models, or concrete types.
- Every API model field has `Field(description=...)`.
- Low complexity, enforced by ruff: cyclomatic complexity 10, at most 10 branches, 6 returns, 40 statements and 6 arguments per function. Split a function that trips these into well-named smaller ones; never raise the limits or add `noqa`.
- Comments only to explain the non-obvious why, never the what. Default to zero.
- Main interfaces and functions (providers, executors, the catalog, forms, pricing, the job lifecycle, the ledger) carry a docstring: one imperative line, then `:param:`, `:return:` or `:raises:` only when they add something. Ruff checks every docstring against pep257.
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
- With `N8N_API_KEY` set, the n8n provider (`src/workflows/jobtypes/providers/n8n.py`) serves every active n8n workflow tagged `h4ks-workflows`, read through the n8n public API with a read-only key. Each needs an enabled Webhook node (its path is the executor URL), an enabled Form Trigger node that requires an n8n login and is connected into the flow, whose title, description and fields define the form, and JSON settings in that form node's Notes: a required `price`, and optional `pricing`, `steps`, `position` and per-field `fields` limits (`minimum`, `maximum`, `min_length`, `max_length`, `show_when`). Everything else comes from the form itself, so it also works for manual runs in n8n: the job type's name is the webhook path without `workflows-`, a field's help text is its placeholder, else the text of a Custom HTML element right after it, else a single checkbox's option text, each `<img alt="option">` in that HTML is the picture on that dropdown option, and a number field is a float when its default has a decimal point. A workflow that breaks these rules is skipped, logged, and listed on the admin page with the reason. `n8n/README.md` is the guide for building such a workflow; keep it in step with `src/workflows/jobtypes/providers/n8n.py`. Without the key, the built-in provider serves the types in `builtin.py` with an `HttpExecutor` that posts to `N8N_URL/webhook/<webhook path>`.
- `WORKFLOW_SERVICES` lists base URLs of services that sell their own job type (`src/workflows/jobtypes/providers/services.py`). Each serves `GET /v1/workflow` with `name`, `title`, `description`, `pricing`, `price`, `steps` and a JSON schema `params_schema`, and takes dispatches at `POST /v1/workflow/jobs`. Both calls carry `WORKFLOW_SERVICE_TOKEN` as `X-API-Key`, a key separate from the n8n executor token. midifier serves the `midi` job type this way.
- Executor dispatch carries the `X-API-Key` header and a JSON body with `job_id`, `type`, `params`, `steps` (step names in order), `callback_url` and `callback_token`. Executors call back with the per-job bearer token; we store only its hash.
- Executor contract: accept the dispatch with a 2xx and send the first `step` event right away. A 4xx/5xx answer or a refused connection fails the job and refunds it. A dispatch timeout leaves the job running, and a job with no event within `FIRST_EVENT_TIMEOUT` seconds fails and is refunded. Executors retry callbacks: a repeated `result` after success or `error` after failure gets 204, any other event after the job ended gets 409, which includes every callback for a cancelled job.
- Lifecycle changes publish on the event bus after the commit, for live views.
- A job submitted through `POST /api/clients/jobs` can carry a `webhook` (`url`, `token`, `extra_params`, `message_prefix`). When the job starts, succeeds, fails or is cancelled, `webhooks.py` posts JSON with `Authorization: Bearer <token>`: the `extra_params`, then `message` (the prefix plus a short plain sentence), `job_id`, `type`, `status`, `run_url` and `result_urls` (file URLs, then result link URLs, on success). Delivery is best-effort: it retries connection errors, timeouts and 5xx three times with backoff, gives up on 4xx, and never affects the job.
- A trusted client can `PUT /api/clients/subscription` with `url` and `signing_key` (upserted by `url`, removed with `DELETE /api/clients/subscription?url=...`) to get a signed raw event for every paid job when it is queued, starts, succeeds, fails or is cancelled. The body is `event`, `job_id`, `type`, `type_title`, `status`, `owner`, `title` (the result title), `run_url`, `result_urls`, `error` and `has_webhook` (whether the job also reports to its own webhook), with header `X-Webhook-Signature: sha256=<hex>` where `hex` is `HMAC-SHA256(signing_key, json.dumps(payload, sort_keys=True))`. Same retry and logging rules as the per-job webhook.
- The queue pause flag lives in memory, so a restart resumes the queue.
- The production database is never reset. On startup, any column declared on a model but missing from an existing table is added with `ALTER TABLE ... ADD COLUMN`. A new column must be nullable or have a server default, since it lands on rows that already exist.
- An admin can remove a job's generated files (`POST /api/admin/jobs/{id}/remove`). It deletes every result file URL and the metadata URL from object storage (any bucket on the MinIO host, configured by `MINIO_ENDPOINT`/`MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY`/`MINIO_USE_SSL`), clears `Job.result` and sets `Job.removed_at`. A removed job's result is replaced everywhere with "removed by an admin".
- Logged-in browser calls that change state under `/api` send `X-Requested-With: fetch`; requests with a bearer token are exempt. Request bodies are capped at 256 KB.
- Prose (docs, commits): declarative, terse, no em dashes.

## Hygiene
Never commit tokens or databases. The `.gitignore` excludes `.env`, `data/`, `*.db` and the `.rev-ok` marker.
