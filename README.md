# h4ks workflows

Storefront and queue for h4ks workflows: users pay credits to run a job, and external executors do the work.

Users pick a job type, see its price before submitting, and follow each run live. They pay in credits: a free allowance every day, plus credits bought by exchanging beans. Their wallet shows the balance and history, and past runs stay browsable. Chat bots and agents submit jobs through the API and MCP on behalf of linked accounts. Admins pause the queue, cancel and refund jobs, grant credits and remove result files.

Job types come from n8n workflows (see [n8n/](n8n/README.md)) and from standalone services.

```sh
make install
cp .env.example .env
make run
make quality
```

```sh
docker compose up
make e2e-up
make e2e
make e2e-down
```
