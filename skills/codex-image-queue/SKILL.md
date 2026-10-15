---
name: codex-image-queue
description: Prepare JSON image jobs and coordinate multiple local Codex windows using the session's built-in image generation. Use when the user chooses Codex for queued batches, shared claims, or targeted revisions. Requires an available native image tool; the queue does not provide a model or an API subscription.
---

# Codex Image Queue

The bundled `scripts/image_queue.py` is a Python 3.11+ local queue. It coordinates jobs; the conversation generates images with its available native tool. Keep the user's chosen backend. Do not replace a missing tool with a paid API without their instruction.

## Producer

- Use the user's common queue folder or `~/.codex-image-queue`. Every window uses the same absolute `--home`. Keep it on local disk, outside synchronized folders.
- Write one JSON per image with `job_id`, `project`, and one `items` object containing `id`, `prompt`, `output_path`; include local `reference_images` for edits. New jobs need fresh identifiers and distinct outputs.
- Run `python <skill-dir>/scripts/image_queue.py --home <queue-home> enqueue --job-file <json> --base-dir <project-root>`. Relative paths resolve against `--base-dir`.
- Check `status` or bounded `wait`, then inspect the delivered images. Completed means saved, not approved. Quality revisions use new jobs and paths; keep originals for comparison.

Read [protocol.md](references/protocol.md) for the schema and commands.

## Worker

Use a distinct worker name per window. Claim one item at a time:

```text
python <skill-dir>/scripts/image_queue.py --home <queue-home> claim --worker-id <worker-name>
```

- If `claimed` is false, do not invent work. Use bounded `watch` when monitoring is requested. Stop when the user stops the worker or the requested batch is done; do not create automations implicitly.
- Keep `claim_id`. Read `next_item.prompt`, inspect local reference images before edits, and generate exactly one requested image with the available native tool.
- Do not write directly to the reserved final `next_item.output_path`. Submit the tool-returned local file with `complete --claim-id <id> --source <returned-file>`. Alternatively save to `generation_output_path` and complete without `--source`.
- If the tool returns only a displayed image, use the host's supported local-save mechanism. Do not guess paths or claim completion without a file. If native generation/export is unavailable, report the limitation and mark failed.
- Technical errors use `fail --claim-id <id> --error <reason>`. Do not keep redrawing because you dislike the picture; quality belongs to requester review.
- Use `heartbeat` between long steps when practical. Missing heartbeats are diagnostic, not proof generation stopped.

## Recovery

Preview with `recover --claim-id <id>`. Use `--confirm-worker-stopped` only after confirming the old worker/request stopped. A saved candidate can be completed; absent images are requeued within the attempt limit. Revoked tokens cannot submit. Do not claim exactly-once remote generation across crashes.

`retry --job-id <id>` retries failed technical work within the total attempt limit. New artistic revisions use new JSON and output paths. Conflicting files are preserved for inspection.

Account eligibility and provider limits still apply. Local concurrency is not a provider quota; unchanged usage percentages do not establish free or unlimited generation.
