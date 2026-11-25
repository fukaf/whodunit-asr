"""Stage 6: rendering.

Default human-readable form is the one requested: "somebody said something at
some time". Also emits SRT for review tools and JSON for downstream systems.
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import OutputConfig
from .types import TranscriptResult, Utterance


def fmt_time(seconds: float, style: str = "hms") -> str:
    seconds = max(0.0, seconds)
    if style == "seconds":
        return f"{seconds:.2f}"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    # Fixed-width components keep columns aligned when scanning a long list.
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def fmt_srt_time(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def render_txt(result: TranscriptResult, cfg: OutputConfig) -> str:
    lines = []
    for u in result.utterances:
        t = fmt_time(u.start, cfg.timestamp_format)
        lines.append(f"[{t}] {u.label}: {u.text}")
    return "\n".join(lines) + ("\n" if lines else "")


def render_srt(result: TranscriptResult) -> str:
    blocks = []
    for i, u in enumerate(result.utterances, start=1):
        blocks.append(
            f"{i}\n{fmt_srt_time(u.start)} --> {fmt_srt_time(u.end)}\n"
            f"[{u.label}] {u.text}\n"
        )
    return "\n".join(blocks)


def render_markdown(result: TranscriptResult, cfg: OutputConfig) -> str:
    lines = [
        f"# Transcript ({result.language or 'unknown language'})",
        "",
        f"- Duration: {fmt_time(result.duration, 'hms')}",
        f"- Speakers: {', '.join(result.speakers) if result.speakers else 'n/a'}",
        "",
        "| Time | Speaker | Utterance |",
        "| --- | --- | --- |",
    ]
    for u in result.utterances:
        text = u.text.replace("|", "\\|")
        lines.append(f"| {fmt_time(u.start, cfg.timestamp_format)} | {u.label} | {text} |")
    return "\n".join(lines) + "\n"


def write_all(result: TranscriptResult, cfg: OutputConfig, stem: str) -> list[Path]:
    out_dir = Path(cfg.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    formats = set(cfg.formats)
    if "json" in formats:
        p = out_dir / f"{stem}.json"
        p.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
                     encoding="utf-8")
        written.append(p)
    if "txt" in formats:
        p = out_dir / f"{stem}.txt"
        p.write_text(render_txt(result, cfg), encoding="utf-8")
        written.append(p)
    if "srt" in formats:
        p = out_dir / f"{stem}.srt"
        p.write_text(render_srt(result), encoding="utf-8")
        written.append(p)
    if "md" in formats:
        p = out_dir / f"{stem}.md"
        p.write_text(render_markdown(result, cfg), encoding="utf-8")
        written.append(p)
    return written
