---
name: aliyun-cos-video-transcription
description: Transcribe one or many local meeting recordings, interviews, courses, or videos in parallel by uploading them temporarily to a private Tencent COS bucket and calling Alibaba Cloud DashScope file transcription. Use this skill whenever the user asks to batch transcribe local audio/video, generate timed TXT/SRT/JSON transcripts, manage parallel transcription jobs, or track estimated and billed transcription costs.
compatibility: Requires uv, ffprobe, a private Tencent COS bucket, DashScope access, and network access. macOS Keychain is optional when credentials are supplied through environment variables or .env.
---

# Tencent COS + DashScope Batch Transcription

Use the bundled `scripts/transcribe_videos.py` for deterministic batch work. It loads credentials from environment variables or a local `.env` file first, then falls back to macOS Keychain when a variable is missing. It uploads each input to a private COS bucket, creates a short-lived signed URL, calls DashScope asynchronous file transcription, exports results locally, and deletes the temporary COS object by default.

## Credentials

Copy `.env.example` to `.env` and replace the placeholders, or export these variables in the shell. Do not ask users to paste secret values into chat. The script checks these variables before trying macOS Keychain:

```bash
cp .env.example .env
```

- `DASHSCOPE_API_KEY`: DashScope API key
- `COS_SECRET_ID`: Tencent COS SecretId
- `COS_SECRET_KEY`: Tencent COS SecretKey

When these variables are omitted, the default macOS Keychain service names are `DASHSCOPE_API_KEY`, `COS_SECRET_ID`, and `COS_SECRET_KEY`.

Override service names when an existing Keychain uses different labels:

```bash
export DASHSCOPE_KEYCHAIN_SERVICE="..."
export COS_SECRET_ID_KEYCHAIN_SERVICE="..."
export COS_SECRET_KEY_KEYCHAIN_SERVICE="..."
```

Actual billing reconciliation additionally needs separate read-only billing credentials. Keep those outside this skill and never commit them.

## Workflow

1. Confirm the user owns or is authorized to upload each source recording. Explain that real submission uploads media to Tencent COS and creates an Alibaba Cloud job that may incur charges.
2. Run a dry run first when file paths, media types, concurrency, or estimated cost need checking. It does not access cloud services.
3. Before retrying a submission, verify that no previous local runner is still active. Check with `pgrep -fl transcribe_videos.py`, then inspect the existing runner output and `report.json` before taking any action.
4. For long uploads, use a durable local session such as `tmux` when the command runner may disconnect. Monitor that same session; do not start a second invocation merely because log streaming stopped.
5. Start at `--max-workers 2`. Increase only after the user accepts the additional simultaneous uploads and potential concurrent billing.
6. Inspect `report.json` after the run. Each job includes duration, task ID, outputs, local estimated cost, and a placeholder for actual billed cost. The report is only written after all local jobs reach a terminal state.
7. Treat `actual_cost` as pending until the billing record is available. Do not present an estimate as a confirmed charge.

## Commands

Run a local-only estimate for every supported media file under a directory:

```bash
uv run --with cos-python-sdk-v5 --with dashscope --with requests --with python-dotenv \
  python scripts/transcribe_videos.py \
  ~/Downloads/meetings \
  --cos-bucket "${COS_BUCKET_NAME}" \
  --cos-region "${COS_REGION}" \
  --price-per-minute <current-price> \
  --dry-run
```

Submit up to two files concurrently and write local exports:

```bash
uv run --with cos-python-sdk-v5 --with dashscope --with requests --with python-dotenv \
  python scripts/transcribe_videos.py \
  ~/Downloads/meeting-1.mp4 ~/Downloads/meeting-2.mp4 \
  --cos-bucket "${COS_BUCKET_NAME}" \
  --cos-region "${COS_REGION}" \
  --max-workers 2 \
  --output-dir ./transcripts
```

## Outputs

For each source file, produce collision-resistant filenames in the output directory:

- `.json`: provider result with timing and speaker metadata when available.
- `.txt`: readable lines with timestamps and speaker labels.
- `.srt`: subtitle file.
- `report.json`: all submitted jobs, their task IDs, state, outputs, duration, and cost fields.

The source filename may contain dots, spaces, or Unicode. Preserve the complete filename stem when appending `.json`, `.txt`, and `.srt`; never use `Path.with_suffix()` on a generated stem derived from such a filename, because it can truncate the visible name and cause collisions.

## Failure Handling

- If a job fails, keep other jobs running and inspect its `error` in `report.json`.
- If the command caller disconnects without a final report, first check for an active `transcribe_videos.py` process. If one exists, leave it running and recover its task IDs from the live session or eventual report. Do not submit the media again.
- If the local runner is gone but no report or task ID is available, treat the attempt as ambiguous. Check the private COS transcription prefix and available DashScope task evidence before retrying; explain the duplicate-billing risk when task creation cannot be ruled out.
- The script deletes uploaded COS media in `finally` blocks. Use `--keep-cos-source` only when debugging a provider-side download failure, then explicitly delete the objects afterward.
- A completed DashScope task can have a delayed or transient result-download failure. Retry the result download with the existing task ID; do not resubmit the media unless the task itself failed.
- Use the DashScope file transcription endpoint for timed text. It is not the full web-based Tongyi Tingwu workflow for generated chapters, summaries, or action items.
