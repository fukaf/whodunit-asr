"""Post-hoc speaker labelling: result JSON -> clips + review.html -> labels JSON.

The browser page is a plain local HTML file. It only plays short clips and
collects names; embedding and registry writes stay in Python.
"""
from __future__ import annotations

import json
import wave
from pathlib import Path
from typing import Any

import numpy as np

from . import audio as audio_mod
from .config import AudioConfig, OutputConfig, PipelineConfig
from .embedding import SpeakerEmbedder
from .registry import SpeakerRegistry
from .render import write_all
from .types import TranscriptResult, Utterance, Word


def load_result(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def result_from_dict(data: dict[str, Any]) -> TranscriptResult:
    utterances = []
    for raw in data.get("utterances", []):
        words = [Word(start=w["start"], end=w["end"], text=w["text"],
                      score=w.get("score")) for w in raw.get("words", [])]
        utterances.append(Utterance(
            speaker=raw["speaker"], start=raw["start"], end=raw["end"],
            text=raw["text"], words=words,
            speaker_display=raw.get("speaker_display"),
        ))
    return TranscriptResult(
        utterances=utterances,
        language=data.get("language"),
        speakers=list(data.get("speakers", [])),
        duration=float(data.get("duration", 0.0)),
        meta=dict(data.get("meta", {})),
    )


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as fh:
        rate = fh.getframerate()
        raw = fh.readframes(fh.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return audio, rate


def _speaker_ids(data: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    for u in data.get("utterances", []):
        if u["speaker"] not in seen:
            seen.append(u["speaker"])
    return sorted(seen)


def init_labels(result_path: str | Path, *, clip_count: int = 5,
                clip_seconds: float = 8.0, registry_path: str | None = None) -> dict[str, Path]:
    """Create labels JSON, short speaker clips and a local review HTML page."""
    result_path = Path(result_path).expanduser().resolve()
    data = load_result(result_path)
    source = data.get("meta", {}).get("source")
    if not source or not Path(source).is_file():
        raise FileNotFoundError(f"source audio in result meta not found: {source}")

    stem = result_path.stem
    out_dir = result_path.parent
    clips_dir = out_dir / f"{stem}_clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    cfg_audio = AudioConfig()
    speakers: list[dict[str, Any]] = []

    for speaker in _speaker_ids(data):
        utts = [u for u in data["utterances"] if u["speaker"] == speaker]
        utts.sort(key=lambda u: -(u["end"] - u["start"]))
        clips: list[str] = []
        hints: list[str] = []
        for i, utt in enumerate(utts[:clip_count]):
            start = max(0.0, float(utt["start"]))
            end = min(float(data.get("duration", start + clip_seconds)),
                      start + clip_seconds)
            if end <= start:
                end = start + max(1.0, float(utt["end"]) - start)
            arr = audio_mod.decode(source, cfg_audio, start=start, end=end)
            rel = f"{stem}_clips/{speaker}_{i:02d}.wav"
            audio_mod.write_wav(arr, out_dir / rel, cfg_audio.sample_rate)
            clips.append(rel)
            hints.append(f"[{utt['start']:.3f}] {utt['text']}")
        speakers.append({
            "id": speaker,
            "display_name": "",
            "persist": False,
            "registry_id": "",
            "hints": hints,
            "clips": clips,
        })

    labels = {
        "source_result": str(result_path),
        "registry": registry_path or str(Path("~/.sdts/registry.db").expanduser()),
        "speakers": speakers,
    }
    labels_path = out_dir / f"{stem}.labels.json"
    labels_path.write_text(json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8")

    html_path = out_dir / f"{stem}.review.html"
    html_path.write_text(_render_html(labels), encoding="utf-8")
    return {"labels": labels_path, "html": html_path, "clips": clips_dir}


def _render_html(labels: dict[str, Any]) -> str:
    payload = json.dumps(labels, ensure_ascii=False).replace("<", "\\u003c")
    sections = []
    for spk in labels["speakers"]:
        hints = "".join(f"<li>{h}</li>" for h in spk["hints"])
        clips = "".join(
            f'<audio controls preload="none" src="{c}"></audio>' for c in spk["clips"])
        sections.append(f"""
<section class="speaker">
  <h2>{spk['id']}</h2>
  <ul>{hints}</ul>
  <div class="clips">{clips}</div>
  <label>姓名 <input type="text" class="name" value="{spk['display_name']}"></label>
  <label class="persist"><input type="checkbox" class="persist"
      {'checked' if spk['persist'] else ''}> 保存到声纹库</label>
</section>""")
    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>说话人标注</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 860px; margin: 24px auto;
        color: #2c2c2a; background: #fff; line-height: 1.6; }}
.speaker {{ border: 1px solid #d3d1c7; border-radius: 8px; padding: 12px 16px;
            margin: 12px 0; }}
h1 {{ font-size: 20px; font-weight: 500; }}
h2 {{ font-size: 15px; font-weight: 500; }}
audio {{ height: 32px; margin: 2px 8px 2px 0; }}
input[type=text] {{ padding: 4px 6px; }}
button {{ padding: 8px 14px; font-size: 14px; cursor: pointer; }}
label {{ margin-right: 16px; }}
</style></head><body>
<h1>说话人标注</h1>
<p>逐段试听，填入真实姓名。需要长期记住的声音勾选“保存到声纹库”。</p>
{''.join(sections)}
<p><button id="export">导出标注 JSON</button></p>
<script id="labels" type="application/json">{payload}</script>
<script>
const data = JSON.parse(document.getElementById('labels').textContent);
document.querySelectorAll('.speaker').forEach((el, i) => {{
  el.querySelector('.name').addEventListener('input', e => data.speakers[i].display_name = e.target.value);
  el.querySelector('.persist').addEventListener('change', e => data.speakers[i].persist = e.target.checked);
}});
document.getElementById('export').addEventListener('click', () => {{
  const blob = new Blob([JSON.stringify(data, null, 2)], {{type: 'application/json'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = data.source_result.split(/[\\\\/]/).pop().replace(/\\.json$/, '') + '.labels.json';
  a.click();
  URL.revokeObjectURL(a.href);
}});
</script></body></html>
"""


def apply_labels(result_path: str | Path, labels_path: str | Path, *,
                 registry_path: str | None = None, embedder=None) -> dict[str, Any]:
    """Write speaker_display, optionally persist voices, and re-render outputs."""
    result_path = Path(result_path).expanduser().resolve()
    labels_path = Path(labels_path).expanduser().resolve()
    data = load_result(result_path)
    labels = load_result(labels_path)

    mapping = {s["id"]: s.get("display_name", "") for s in labels["speakers"]}
    for u in data["utterances"]:
        name = mapping.get(u["speaker"])
        if name:
            u["speaker_display"] = name

    result = result_from_dict(data)
    out_dir = result_path.parent
    out_cfg = OutputConfig(out_dir=str(out_dir),
                           formats=("json", "txt", "srt", "md"))
    written = write_all(result, out_cfg, result_path.stem)

    persisted: dict[str, str] = {}
    registry = None
    try:
        for entry in labels["speakers"]:
            if not entry.get("persist") or not entry.get("display_name"):
                continue
            if entry.get("registry_id"):
                persisted[entry["id"]] = entry["registry_id"]
                continue
            vecs = []
            for rel in entry.get("clips", []):
                clip = labels_path.parent / rel
                if not clip.is_file():
                    continue
                arr, rate = _read_wav(clip)
                v = _embedder(embedder).embed_audio(arr, rate)
                if v is not None:
                    vecs.append(v)
            if not vecs:
                continue
            mean = np.mean(vecs, axis=0)
            norm = float(np.linalg.norm(mean))
            mean = mean / norm if norm else mean
            if registry is None:
                rp = registry_path or labels.get("registry") or "~/.sdts/registry.db"
                registry = SpeakerRegistry(rp)
            sid = registry.add_speaker(entry["display_name"], mean,
                                       consent_scope="persistent",
                                       source_file=data.get("meta", {}).get("source"))
            entry["registry_id"] = sid
            persisted[entry["id"]] = sid
    finally:
        if registry:
            registry.close()

    if persisted:
        labels_path.write_text(json.dumps(labels, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    return {"persisted": persisted, "written": [str(p) for p in written]}


def _embedder(candidate) -> SpeakerEmbedder:
    if candidate is not None:
        return candidate
    cfg = PipelineConfig()
    return SpeakerEmbedder(cfg.diarization.device, cfg.resolve_hf_token())


def set_label(result_path: str | Path, speaker: str, name: str) -> list[Path]:
    """Quick one-off session alias without registry changes."""
    result_path = Path(result_path).expanduser().resolve()
    data = load_result(result_path)
    for u in data["utterances"]:
        if u["speaker"] == speaker:
            u["speaker_display"] = name
    result = result_from_dict(data)
    out_cfg = OutputConfig(out_dir=str(result_path.parent),
                           formats=("json", "txt", "srt", "md"))
    return write_all(result, out_cfg, result_path.stem)
