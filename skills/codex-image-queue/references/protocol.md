# Job and worker protocol

```json
{
  "job_id": "demo_20261015_001_portrait",
  "project": "Demo",
  "items": [{
    "id": "shopkeeper",
    "prompt": "Hand-painted shopkeeper portrait, complete head with margin.",
    "output_path": "art/shopkeeper.png",
    "reference_images": []
  }]
}
```

`job_id` is unique across all retained jobs, including completed/failed jobs. `id` identifies the image inside the job. IDs use letters, digits, dots, underscores and hyphens, maximum 120 characters. A final output path can only belong to one job; pre-existing files are preserved.

## Commands

Replace `Q` with the absolute path to this Skill's `scripts/image_queue.py`. All windows use the same absolute `HOME` queue directory.

```text
python Q --home HOME init --max-active 3 --max-attempts 3
python Q --home HOME enqueue --job-file JOB.json --base-dir PROJECT_ROOT
python Q --home HOME claim --worker-id window-1
python Q --home HOME watch --worker-id window-1 --timeout 45 --poll 3
python Q --home HOME heartbeat --claim-id TOKEN
python Q --home HOME complete --claim-id TOKEN --source GENERATED_LOCAL_FILE
python Q --home HOME fail --claim-id TOKEN --error REASON
python Q --home HOME status
python Q --home HOME wait --job-id JOB_ID --timeout 45
python Q --home HOME retry --job-id JOB_ID
python Q --home HOME recover --claim-id TOKEN
python Q --home HOME recover --claim-id TOKEN --confirm-worker-stopped
```

Responses are JSON. Exit 1 means invalid input or an operation error. `claim`/`watch` can return `claimed:false` with exit 0: inspect the JSON. A bounded wait returns `timed_out:true` without cancelling a job.

## State and output

`queued → in_progress → completed | failed`

Each attempt gets a new token and candidate path. Claims are short transactions; generation happens outside the lock. Repeating a completed submission is idempotent if the file still matches its recorded hash. Failed/revoked tokens cannot submit.

State and event history live in `queue.sqlite3`; intermediate images live under `attempts/<claim_id>/`. Keep these private and out of Git. The CLI has no network requests.

`complete` checks the image signature and publishes without replacing a different existing file. Full decode and art quality are requester review. If publication was interrupted, confirm the original worker stopped, then recover the matching attempt. A final file without its matching candidate requires manual inspection.

`max_attempts` counts all claims, including the first. Technical failure requires explicit retry. Stale status is information only; recovery requires confirmation that the worker stopped. The program cannot prove remote generation has terminated from a timestamp.

Read the native image tool's instructions in the host; names, reference formats and export mechanisms vary. If no native generation/export exists, fail clearly. This Skill does not invent endpoints, read browser cookies or switch providers on its own.
