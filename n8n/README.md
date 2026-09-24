# n8n workflows

Every active n8n workflow tagged `h4ks-workflows` becomes a job type on workflows.h4ks.com, in the API and in the MCP. The service lists them with a read-only n8n API key every 60 seconds. A workflow that breaks the rules below is skipped, and the admin page lists it with the reason.

## What the workflow needs

1. **The `h4ks-workflows` tag**, and the workflow is published. Remove the tag or unpublish it to take the job off the site.
2. **An enabled Webhook trigger** with method `POST`, header auth using the `workflows executor (X-API-Key)` credential, and "respond immediately". Its path is where the service sends each job, so keep it unique, for example `workflows-image`.
3. **An enabled Form Trigger** whose title, description and fields are the form users fill in on the site, set to require an n8n login, with its own path (for example `workflows-image-form`, since it cannot share the webhook's). It is a real trigger: people signed in to n8n can run the workflow from it, which runs the same flow without touching anyone's credits.
4. **Both triggers joined into one flow.** The webhook goes straight into `Read Job`. The form goes through `Form Files` (one item per uploaded file), `Has Files?`, `Store Upload` (the shared store sub-workflow, which turns each upload into a `workflows` bucket link) and `From Form`, which builds the same `params` the service sends, with the manual-run sink as `callback_url`. `Read Job` accepts callback URLs on workflows.h4ks.com and the sink, `https://n8n.t3ks.com/webhook/workflows-manual-sink`, which drops the progress and result calls of manual runs. Copy these four nodes from an existing executor.
5. **Settings in the Job Form's Notes** (node settings, Notes, with "Display note in flow" on): a small JSON object holding what a form cannot, at least the price.

The service skips a workflow whose form is disabled, public, not connected to anything, or has no settings.

## Form fields

Every field needs a Field Name, which becomes the param name the executor receives. Help text comes from the form too: a field's placeholder, or for dropdowns, files and checkboxes (which have no placeholder) a Custom HTML element placed right after the field. A checkbox with no help text uses its option text.

| n8n field | On the site | The executor receives |
| --- | --- | --- |
| Text, Email, Date | text input, 200 characters | string |
| Textarea | text area, 2000 characters | string |
| Number | number input | integer, or a float with `"type": "number"` |
| Dropdown or Radio | buttons, one choice | one of the options |
| File | link, upload or drag and drop, with its accepted types | an `https` URL |
| Checkbox with exactly one option | checkbox; the default is checked when it equals the option | boolean |
| Custom HTML | help text of the field before it | nothing |
| Hidden Field | ignored | nothing |

Required fields are required on the site. A default value pre-fills the field; for a dropdown it must match an option. Multiselect dropdowns, checkboxes with several options and password fields are refused.

## The settings

The smallest settings are just the price:

```json
{"price": "40"}
```

Fuller ones, from the image job:

```json
{
  "pricing": "40 credits per image, 80 for large",
  "price": "80 if size == 'large' else 40",
  "steps": [["generate", 85], ["store", 15]],
  "position": 4,
  "fields": {
    "reference_url": {"show_when": {"model": "flux-klein"}}
  }
}
```

- `price` (required): the credits a job costs, as an expression over the field names. It allows numbers, `+ - * /`, comparisons, `a if condition else b` and `duration(field)`, which reads the length in seconds of the media behind a link field. For example `1.2 * duration(url) + 120`. The estimated run time in seconds equals the price.
- `pricing`: the price explained in words, shown to users. Defaults to the price expression.
- `steps`: the step names the executor reports, in order, each with a weight for the progress bar. Defaults to one step called `run`.
- `position`: the order on the site, lowest first. Defaults to 0.
- `name`: the job type id in URLs and the API, lowercase letters, digits and dashes. Defaults to the webhook path without its `workflows-` prefix.
- `fields`: per-field settings the n8n form cannot hold: `type`, `minimum`, `maximum`, `min_length`, `max_length`, `show_when`, and `description` to override the help text. A field with `show_when` shows only when the other fields hold those values, and the service rejects it otherwise.

## What the executor does

The service posts this JSON to the webhook with the `X-API-Key` header:

```json
{"job_id": 7, "type": "image", "params": {"prompt": "a cat", "model": "z-image"}, "steps": ["generate", "store"], "callback_url": "https://workflows.h4ks.com/api/jobs/7/events", "callback_token": "..."}
```

The workflow then posts events to `callback_url` with `Authorization: Bearer <callback_token>`:

- `{"kind": "step", "step": "generate"}` when a step starts, and optionally `done` and `total` for progress within it. Send the first step right away: a job with no event within 120 seconds fails.
- `{"kind": "log", "message": "..."}` for the activity log.
- `{"kind": "result", "title": "...", "files": [{"url": "https://...", "name": "image.png", "mime": "image/png"}], "metadata_url": "https://..."}` to finish. Store files in the public `workflows` bucket. An optional `links` list of `{"label": "...", "url": "https://..."}` adds pages that open the result, such as a player.
- `{"kind": "error", "message": "..."}` to fail the job and refund the user.

Give the result and error calls "retry on fail" (5 tries, 5 seconds apart) so a service restart does not lose a finished job. Text is trimmed to one line; titles and names hold 200 characters and messages 500.

## Changing workflows

Create and update workflows in the n8n editor or through the n8n MCP. After a `n8n import:workflow` on the server, publish each imported workflow again from the editor or the MCP: the running n8n only registers its webhooks on publish.
