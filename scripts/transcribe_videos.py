#!/usr/bin/env python3
"""Batch transcribe local media through private Tencent COS and DashScope."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import requests
from dashscope.audio.asr.transcription import Transcription
from dotenv import load_dotenv
from qcloud_cos import CosConfig, CosS3Client

load_dotenv()

MEDIA_SUFFIXES = {".aac", ".flac", ".m4a", ".mkv", ".mov", ".mp3", ".mp4", ".wav", ".webm"}
KEYCHAIN_SERVICES = {
    "dashscope_api_key": os.environ.get("DASHSCOPE_KEYCHAIN_SERVICE", "DASHSCOPE_API_KEY"),
    "cos_secret_id": os.environ.get("COS_SECRET_ID_KEYCHAIN_SERVICE", "COS_SECRET_ID"),
    "cos_secret_key": os.environ.get("COS_SECRET_KEY_KEYCHAIN_SERVICE", "COS_SECRET_KEY"),
}
PRINT_LOCK = threading.Lock()


@dataclass(frozen=True)
class Credentials:
    dashscope_api_key: str
    cos_secret_id: str
    cos_secret_key: str


def log(message: str) -> None:
    with PRINT_LOCK:
        print(message, flush=True)


def keychain_secret(service: str) -> str:
    account = os.environ.get("KEYCHAIN_ACCOUNT", os.environ.get("USER", ""))
    command = ["security", "find-generic-password"]
    if account:
        command.extend(["-a", account])
    command.extend(["-s", service, "-w"])
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def load_credentials() -> Credentials:
    values = {}
    environment_names = {
        "dashscope_api_key": "DASHSCOPE_API_KEY",
        "cos_secret_id": "COS_SECRET_ID",
        "cos_secret_key": "COS_SECRET_KEY",
    }
    for name, service in KEYCHAIN_SERVICES.items():
        environment_name = environment_names[name]
        value = os.environ.get(environment_name)
        if value:
            values[name] = value
            continue
        try:
            values[name] = keychain_secret(service)
        except (OSError, subprocess.CalledProcessError) as error:
            raise RuntimeError(
                f"Missing {environment_name}. Set it in the environment or a local .env file, "
                f"or configure the macOS Keychain service {service!r}."
            ) from error
    return Credentials(**values)


def media_duration_seconds(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def format_timestamp(milliseconds: int, separator: str = ",") -> str:
    total = timedelta(milliseconds=milliseconds)
    hours, remainder = divmod(int(total.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{milliseconds % 1000:03d}"


def output_stem(path: Path, output_dir: Path) -> Path:
    digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:8]
    return output_dir / f"{path.stem}-{digest}"


def transcript_sentences(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [sentence for transcript in payload.get("transcripts", []) for sentence in transcript.get("sentences", [])]


def write_exports(payload: dict[str, Any], stem: Path) -> list[str]:
    # `Path.with_suffix()` would treat a dot inside the original filename as a suffix.
    json_path = Path(f"{stem}.json")
    text_path = Path(f"{stem}.txt")
    srt_path = Path(f"{stem}.srt")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    text_lines = []
    srt_blocks = []
    for index, sentence in enumerate(transcript_sentences(payload), start=1):
        begin = int(sentence.get("begin_time", 0))
        end = int(sentence.get("end_time", begin))
        speaker = sentence.get("speaker_id")
        text = sentence.get("text", "").strip()
        speaker_prefix = f"说话人 {speaker}: " if speaker is not None else ""
        text_lines.append(f"[{format_timestamp(begin, '.')}-{format_timestamp(end, '.')}] {speaker_prefix}{text}")
        srt_blocks.append(f"{index}\n{format_timestamp(begin)} --> {format_timestamp(end)}\n{speaker_prefix}{text}\n")

    text_path.write_text("\n".join(text_lines) + "\n", encoding="utf-8")
    srt_path.write_text("\n".join(srt_blocks), encoding="utf-8")
    return [str(json_path), str(text_path), str(srt_path)]


def download_result(url: str) -> dict[str, Any]:
    for attempt in range(1, 4):
        try:
            response = requests.get(url, timeout=(30, 300))
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(attempt * 5)
    raise RuntimeError("Unreachable")


def create_cos_client(credentials: Credentials, region: str) -> CosS3Client:
    return CosS3Client(
        CosConfig(
            Region=region,
            SecretId=credentials.cos_secret_id,
            SecretKey=credentials.cos_secret_key,
            Scheme="https",
        )
    )


def transcribe_one(path: Path, args: argparse.Namespace, credentials: Credentials) -> dict[str, Any]:
    duration = media_duration_seconds(path)
    estimate = round(duration / 60 * args.price_per_minute, 6) if args.price_per_minute is not None else None
    record: dict[str, Any] = {
        "source": str(path),
        "duration_seconds": round(duration, 3),
        "model": args.model,
        "estimated_cost": estimate,
        "actual_cost": None,
        "actual_cost_status": "pending_bss_bill",
        "task_id": None,
        "outputs": [],
        "status": "planned" if args.dry_run else "running",
    }
    if args.dry_run:
        return record

    cos = create_cos_client(credentials, args.cos_region)
    object_key = f"codex-transcription/{uuid.uuid4()}/{path.name}"
    uploaded = False
    try:
        log(f"[{path.name}] Uploading to COS")
        cos.upload_file(Bucket=args.cos_bucket, LocalFilePath=str(path), Key=object_key)
        uploaded = True
        signed_url = cos.get_presigned_url(
            Method="GET", Bucket=args.cos_bucket, Key=object_key, Expired=args.url_expiry_hours * 3600
        )
        task = Transcription.async_call(
            model=args.model,
            file_urls=[signed_url],
            api_key=credentials.dashscope_api_key,
            diarization_enabled=args.diarization,
            timestamp_alignment_enabled=True,
        )
        if getattr(task, "status_code", None) != 200:
            raise RuntimeError(f"Task creation failed: {getattr(task, 'code', None)}")
        record["task_id"] = task.output["task_id"]
        log(f"[{path.name}] Task created: {record['task_id']}")

        while True:
            result = Transcription.fetch(task, api_key=credentials.dashscope_api_key)
            task_status = result.output.get("task_status")
            if task_status == "SUCCEEDED":
                break
            if task_status == "FAILED":
                raise RuntimeError(f"Transcription failed: {result.output.get('code')} {result.output.get('message')}")
            time.sleep(args.poll_interval)

        payload = download_result(result.output["results"][0]["transcription_url"])
        record["outputs"] = write_exports(payload, output_stem(path, args.output_dir))
        record["sentence_count"] = len(transcript_sentences(payload))
        record["status"] = "succeeded"
        log(f"[{path.name}] Transcription complete")
    except Exception as error:
        record["status"] = "failed"
        record["error"] = str(error)
        log(f"[{path.name}] Failed: {type(error).__name__}")
    finally:
        if uploaded and not args.keep_cos_source:
            try:
                cos.delete_object(Bucket=args.cos_bucket, Key=object_key)
                log(f"[{path.name}] COS source deleted")
            except Exception as error:
                record["cleanup_error"] = type(error).__name__
    return record


def collect_media(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES:
            files.append(path)
        elif path.is_dir():
            files.extend(candidate for candidate in path.rglob("*") if candidate.is_file() and candidate.suffix.lower() in MEDIA_SUFFIXES)
        else:
            raise FileNotFoundError(f"No supported media file found at: {path}")
    return sorted(set(files))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parallel local media transcription via private Tencent COS and DashScope")
    parser.add_argument("paths", nargs="+", help="Media files or directories to transcribe")
    parser.add_argument("--cos-bucket", required=True, help="Tencent COS bucket name, for example bucket-appid")
    parser.add_argument("--cos-region", required=True, help="Tencent COS region, for example ap-shanghai")
    parser.add_argument("--output-dir", type=Path, default=Path("transcripts"))
    parser.add_argument("--max-workers", type=int, default=2, help="Concurrent jobs, 1-4 (default: 2)")
    parser.add_argument("--model", default="paraformer-v1")
    parser.add_argument("--poll-interval", type=int, default=10)
    parser.add_argument("--url-expiry-hours", type=int, default=24)
    parser.add_argument("--price-per-minute", type=float, help="Optional current price for local cost estimates")
    parser.add_argument("--no-diarization", dest="diarization", action="store_false")
    parser.add_argument("--keep-cos-source", action="store_true", help="Keep uploaded source objects after each task")
    parser.add_argument("--dry-run", action="store_true", help="Validate files and calculate estimates without uploads")
    parser.set_defaults(diarization=True)
    args = parser.parse_args()
    if not 1 <= args.max_workers <= 4:
        parser.error("--max-workers must be between 1 and 4")
    if args.url_expiry_hours < 1:
        parser.error("--url-expiry-hours must be at least 1")
    return args


def main() -> int:
    args = parse_args()
    media_files = collect_media(args.paths)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    credentials = None if args.dry_run else load_credentials()
    records: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        futures = {executor.submit(transcribe_one, path, args, credentials): path for path in media_files}
        for future in as_completed(futures):
            records.append(future.result())

    records.sort(key=lambda item: item["source"])
    report = {
        "provider": "DashScope file transcription",
        "model": args.model,
        "cost_note": "estimated_cost is local only; actual_cost requires later BSS billing reconciliation.",
        "jobs": records,
    }
    report_path = args.output_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    completed = sum(job["status"] == "succeeded" for job in records)
    failed = sum(job["status"] == "failed" for job in records)
    planned = sum(job["status"] == "planned" for job in records)
    log(f"Finished: {completed} succeeded, {failed} failed, {planned} planned. Report: {report_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
