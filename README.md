# h4ks workflows

A small shop for AI jobs: make a song, a podcast, an image, turn a song into MIDI. You pick a job, see what it costs, and watch it run. Jobs cost credits. Everyone gets some free every day, and you can buy more with [beans](https://beans.h4ks.com).

It runs at [workflows.h4ks.com](https://workflows.h4ks.com). The h4ks IRC bot uses it too, so you can ask for a job in chat.

## Run it locally

```sh
make install
cp .env.example .env
```

Set `SESSION_SECRET` to anything and `DEV_LOGIN=true` to log in without Logto, then:

```sh
make run
```

and open http://localhost:8000.

## Add a job

The site sells jobs and hands each one to whatever does the work:

- an n8n workflow tagged `h4ks-workflows`, see [n8n/README.md](n8n/README.md), which comes with a template to import
- any service that serves `GET /v1/workflow`, listed in `WORKFLOW_SERVICES`

## Develop

```sh
make quality   # everything CI checks
make e2e-up && make e2e && make e2e-down
```
