"""Server tests: job queue, SSE events, REST surface, i18n assets.

Everything runs against the ASGI app in-process (`httpx.ASGITransport`), so
there is no port, no uvicorn and no model download. The model work is replaced
by a fake Pipeline that emits progress events and returns a fixed transcript -
the same trick `test_integration.py` uses, applied one layer up.

What is deliberately covered here rather than in the pipeline tests:
  * the state machine (queued -> running -> done/failed/cancelled),
  * that a job record survives a page reload (it is read back from disk),
  * that the browser cannot reach config keys it should not (`hf_token`),
  * that renames write through to the JSON *and* the text exports,
  * that all three languages are complete.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import numpy as np
import pytest

from sdts.artifacts import save_artifacts
from sdts.config import PipelineConfig
from sdts.server.app import create_app
from sdts.server.events import EventBus
from sdts.server.jobs import JobManager, apply_overrides
from sdts.types import Segment, TranscriptResult, Turn, Utterance

WEB = Path(__file__).resolve().parents[1] / "sdts" / "web"


class FakePipeline:
    """Stands in for `Pipeline`; honours the on_stage contract and can stall."""

    fail_with: str | None = None
    hold: float = 0.0
    last_config: PipelineConfig | None = None

    def __init__(self, cfg: PipelineConfig):
        self.cfg = cfg
        FakePipeline.last_config = cfg

    def run(self, path, num_speakers=None, *, on_stage=None, artifacts_dir=None):
        stages = [("probe", 0, 1), ("chunk", 0, 2), ("chunk", 1, 2),
                  ("chunk", 2, 2), ("reconcile", 0, 1), ("fuse", 0, 1), ("done", 1, 1)]
        for stage, done, total in stages:
            if on_stage:
                on_stage(stage, done, total)
            if FakePipeline.hold:
                time.sleep(FakePipeline.hold)
        if FakePipeline.fail_with:
            raise RuntimeError(FakePipeline.fail_with)
        result = TranscriptResult(
            utterances=[
                Utterance(speaker="SPEAKER_00", start=0.0, end=2.0, text="hello there"),
                Utterance(speaker="SPEAKER_01", start=2.0, end=4.0, text="general kenobi"),
                Utterance(speaker="SPEAKER_00", start=4.0, end=5.0, text="you are a bold one"),
            ],
            language="en", speakers=["SPEAKER_00", "SPEAKER_01"], duration=5.0,
            meta={"source": str(path), "chunks": 2, "asr_engine": "fake",
                  "asr_model": "fake", "diarization": "fake", "diarization_mode": "exclusive",
                  "elapsed_seconds": 0.1, "rtf": 0.02, "sample_rate": 16000,
                  "codec": "pcm_s16le", "channels": 1},
        )
        if artifacts_dir is not None:
            save_artifacts(
                artifacts_dir,
                segments=[Segment(0.0, 2.0, "hello there"),
                          Segment(2.0, 4.0, "general kenobi"),
                          Segment(4.0, 5.0, "you are a bold one")],
                turns=[Turn(0.0, 2.0, "SPEAKER_00"), Turn(2.0, 4.0, "SPEAKER_01"),
                       Turn(4.0, 5.0, "SPEAKER_00")],
                speaker_vectors={"SPEAKER_00": [1.0] + [0.0] * 255,
                                 "SPEAKER_01": [0.0, 1.0] + [0.0] * 254},
                result=result, chunks=[(0.0, 3.0), (2.0, 5.0)], stride=1.0)
        return result


@pytest.fixture()
def app(tmp_path, monkeypatch):
    FakePipeline.fail_with = None
    FakePipeline.hold = 0.0
    FakePipeline.last_config = None
    monkeypatch.setattr("sdts.server.jobs.Pipeline", FakePipeline)
    application = create_app(
        data_dir=tmp_path / "wb",
        registry_path=str(tmp_path / "registry.db"),
        pipeline_factory=lambda cfg: FakePipeline(cfg),
    )
    yield application
    application.state.jobs.shutdown()


@pytest.fixture()
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://127.0.0.1:8765") as c:
        yield c


@pytest.fixture()
def sample(tmp_path):
    from audio_fixtures import tone
    return tone(tmp_path / "meeting.wav", seconds=3.0)


async def wait_done(client, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = (await client.get(f"/api/jobs/{job_id}")).json()["job"]
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish: {job}")


# -- module-level behaviour ------------------------------------------------

async def test_health_and_static_pages(client):
    assert (await client.get("/api/health")).json()["ok"] is True
    index = await client.get("/")
    assert index.status_code == 200
    assert "index.title" in index.text or "index.title" in index.text
    for page in ("job.html", "registry.html", "settings.html", "sweep.html", "eval.html"):
        assert (await client.get(f"/{page}")).status_code == 200, page
    assert (await client.get("/i18n.js")).status_code == 200
    assert (await client.get("/style.css")).status_code == 200


def test_i18n_dictionaries_are_complete():
    """Three languages, identical key sets, and every key the pages use exists.

    Cheap to assert and it is exactly the class of bug a hand-maintained
    dictionary invites: a translated block that silently fell behind.
    """
    source = (WEB / "i18n.js").read_text(encoding="utf-8")
    import re

    def keys_of(lang: str) -> set[str]:
        start = source.index(f"  {lang}: {{")
        depth, i = 0, source.index("{", start)
        while True:
            ch = source[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return set(re.findall(r'"([a-zA-Z][\w.]+)":', source[start:i + 1]))
            i += 1

    zh, en, ja = keys_of("zh"), keys_of("en"), keys_of("ja")
    assert zh == en == ja, {"missing_en": sorted(zh - en), "missing_ja": sorted(zh - ja)}

    used = set()
    for page in WEB.glob("*.html"):
        text = page.read_text(encoding="utf-8")
        used |= set(re.findall(r'data-i18n(?:-placeholder|-title|-aria)?="([\w.]+)"', text))
        used |= set(re.findall(r'data-title="([\w.]+)"', text))
        used |= set(re.findall(r'\bt\("([\w.]+)"', text))
    # `t(`job.stage.${...}`)`-style lookups are checked by prefix instead.
    prefixes = ("job.stage.", "index.jobs.", "job.export.", "job.filters.",
                "job.speakers.decision.", "job.meta.", "settings.")
    concrete = {k for k in used if not k.startswith(prefixes) and k != "id"}
    assert concrete - zh == set(), sorted(concrete - zh)


# -- job lifecycle --------------------------------------------------------

async def test_job_from_local_path_runs_to_completion(client, sample):
    created = await client.post("/api/jobs", json={"source": str(sample), "config": {}})
    assert created.status_code == 201, created.text
    job = created.json()["job"]
    # The single-worker pool may pick it up before the POST response returns.
    assert job["status"] in ("queued", "running")

    final = await wait_done(client, job["id"])
    assert final["status"] == "done"
    assert final["meta"]["utterances"] == 3
    assert final["meta"]["speakers"] == ["SPEAKER_00", "SPEAKER_01"]

    result = (await client.get(f"/api/jobs/{job['id']}/result")).json()
    assert [u["speaker"] for u in result["utterances"]] == [
        "SPEAKER_00", "SPEAKER_01", "SPEAKER_00"]
    # Outputs on disk: the JSON the API serves plus the three text formats.
    out = Path(final["out_dir"])
    assert (out / f"{final['stem']}.json").is_file()
    assert (out / f"{final['stem']}.txt").is_file()


async def test_summary_is_available_while_job_is_running(client, sample):
    """Regression: the job page opened right after upload used to receive 409
    from /summary, never start SSE, and sit at `...` for the entire run."""
    FakePipeline.hold = 0.05
    job = (await client.post("/api/jobs", json={"source": str(sample)})).json()["job"]
    resp = await client.get(f"/api/jobs/{job['id']}/summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job"]["id"] == job["id"]
    assert body["job"]["status"] in ("queued", "running")
    assert body["result_ready"] is False
    assert body["speakers"] == [] and body["utterances"] == 0
    final = await wait_done(client, job["id"])
    assert final["status"] == "done"
    ready = (await client.get(f"/api/jobs/{job['id']}/summary")).json()
    assert ready["result_ready"] is True
    assert len(ready["speakers"]) == 2


async def test_upload_round_trip(client, sample):
    with sample.open("rb") as fh:
        created = await client.post(
            "/api/jobs",
            files={"file": ("meeting.wav", fh, "audio/wav")},
            data={"config": json.dumps({"asr": {"language": "ja"}})})
    assert created.status_code == 201, created.text
    job = created.json()["job"]
    assert job["source_name"] == "meeting.wav"
    final = await wait_done(client, job["id"])
    assert final["status"] == "done"


async def test_unsupported_container_is_rejected(client, tmp_path):
    text_file = tmp_path / "notes.txt"
    text_file.write_text("not audio", encoding="utf-8")
    resp = await client.post("/api/jobs", json={"source": str(text_file)})
    assert resp.status_code == 400
    assert "unsupported" in resp.json()["detail"]


async def test_missing_file_is_404(client, tmp_path):
    resp = await client.post("/api/jobs", json={"source": str(tmp_path / "nope.wav")})
    assert resp.status_code == 404


async def test_failure_becomes_job_state(client, sample):
    FakePipeline.fail_with = "CUDA out of memory"
    job = (await client.post("/api/jobs", json={"source": str(sample)})).json()["job"]
    final = await wait_done(client, job["id"])
    assert final["status"] == "failed"
    assert "CUDA out of memory" in final["error"]
    # A failed job has no result, and asking for one is a 409, not a 500.
    assert (await client.get(f"/api/jobs/{job['id']}/result")).status_code == 409


async def test_cancel_marks_job_cancelled(client, sample):
    FakePipeline.hold = 0.05
    job = (await client.post("/api/jobs", json={"source": str(sample)})).json()["job"]
    time.sleep(0.02)
    await client.post(f"/api/jobs/{job['id']}/cancel")
    final = await wait_done(client, job["id"])
    assert final["status"] == "cancelled"


def test_job_survives_a_restart(tmp_path, sample):
    """The record on disk is the source of truth after the process dies."""
    FakePipeline.fail_with = None
    manager = JobManager(tmp_path / "wb", pipeline_factory=lambda cfg: FakePipeline(cfg))
    job = manager.add_local_file(sample, config={})
    deadline = time.monotonic() + 10
    while manager.get(job.id).status != "done" and time.monotonic() < deadline:
        time.sleep(0.05)
    manager.shutdown()

    reopened = JobManager(tmp_path / "wb")
    try:
        again = reopened.get(job.id)
        assert again.status == "done"
        assert again.meta["utterances"] == 3
    finally:
        reopened.shutdown()


def test_interrupted_job_is_reported_as_failed(tmp_path):
    jobs = tmp_path / "wb" / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "abc123.json").write_text(json.dumps({
        "id": "abc123", "source": "/x/a.wav", "source_name": "a.wav",
        "config": {}, "status": "running", "stage": "chunk",
    }), encoding="utf-8")
    manager = JobManager(tmp_path / "wb")
    try:
        job = manager.get("abc123")
        assert job.status == "failed"
        assert "server stopped" in job.error
    finally:
        manager.shutdown()


# -- events ---------------------------------------------------------------

def test_event_bus_replays_history_to_late_subscribers():
    bus = EventBus()
    bus.publish("progress", {"stage": "chunk", "done": 1})
    bus.publish("progress", {"stage": "chunk", "done": 2})
    late = bus.subscribe()
    bus.publish("done", {})
    bus.close()
    events = late.drain(timeout=0.5)
    assert [e["event"] for e in events] == ["progress", "progress", "done"]
    assert events[0]["data"]["done"] == 1        # replayed, not lost


async def test_sse_stream_carries_progress_frames(client, sample):
    job = (await client.post("/api/jobs", json={"source": str(sample)})).json()["job"]
    frames = []
    async with client.stream("GET", f"/api/jobs/{job['id']}/events") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                frames.append(line.split(":", 1)[1].strip())
            if "done" in frames:
                break
    assert "progress" in frames
    assert frames[-1] in ("done", "result", "end")


# -- results and renaming -------------------------------------------------

async def _finished_job(client, sample) -> dict:
    job = (await client.post("/api/jobs", json={"source": str(sample)})).json()["job"]
    return await wait_done(client, job["id"])


async def test_rename_writes_json_and_text_exports(client, sample):
    job = await _finished_job(client, sample)
    summary = (await client.get(f"/api/jobs/{job['id']}/summary")).json()
    assert [s["id"] for s in summary["speakers"]] == ["SPEAKER_00", "SPEAKER_01"]
    assert summary["speakers"][0]["display_name"] is None

    resp = await client.post(f"/api/jobs/{job['id']}/rename",
                       json={"speaker": "SPEAKER_00", "name": "  Okada  "})
    assert resp.status_code == 200, resp.text
    assert resp.json()["speakers"][0]["display_name"] == "Okada"

    result = (await client.get(f"/api/jobs/{job['id']}/result")).json()
    named = [u for u in result["utterances"] if u["speaker"] == "SPEAKER_00"]
    assert all(u["speaker_display"] == "Okada" for u in named)
    # The cluster that was not renamed must stay null, not fall back to its id.
    others = [u for u in result["utterances"] if u["speaker"] == "SPEAKER_01"]
    assert all(u["speaker_display"] is None for u in others)

    txt = (await client.get(f"/api/jobs/{job['id']}/export?fmt=txt")).text
    assert "Okada: hello there" in txt


async def test_summary_exposes_identification_suggestion(client, sample, app):
    """`review` mode computes a best candidate but must not apply it. The page
    needs the candidate name to offer a one-click 'Apply' button. This test
    also covers the backwards path for jobs produced by the old UI."""
    job = await _finished_job(client, sample)
    path = Path(app.state.jobs.get(job["id"]).result_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["meta"]["identification"] = {
        "mode": "review",
        "matches": [{"speaker": "SPEAKER_00", "decision": "accept",
                     "best": "高市早苗", "score": 0.8272, "candidates": []}],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = (await client.get(f"/api/jobs/{job['id']}/summary")).json()
    spk0 = next(s for s in summary["speakers"] if s["id"] == "SPEAKER_00")
    assert spk0["decision"] == "accept"
    assert spk0["best"] == "高市早苗"
    assert spk0["display_name"] is None       # review mode: no silent rename

    applied = await client.post(f"/api/jobs/{job['id']}/rename",
                                json={"speaker": "SPEAKER_00", "name": spk0["best"]})
    assert applied.status_code == 200
    result = (await client.get(f"/api/jobs/{job['id']}/result")).json()
    named = [u for u in result["utterances"] if u["speaker"] == "SPEAKER_00"]
    assert all(u["speaker_display"] == "高市早苗" for u in named)


async def test_identification_mode_reaches_the_pipeline(client, sample):
    """The new-job form offers 'auto' explicitly; a checked box must send the
    mode the user picked, not silently fall back to review."""
    job = (await client.post("/api/jobs", json={
        "source": str(sample),
        "config": {"identification": {"enabled": True, "mode": "auto"}},
    })).json()["job"]
    await wait_done(client, job["id"])
    assert FakePipeline.last_config is not None
    assert FakePipeline.last_config.identification.enabled is True
    assert FakePipeline.last_config.identification.mode == "auto"


async def test_rename_rejects_unknown_speaker(client, sample):
    job = await _finished_job(client, sample)
    resp = await client.post(f"/api/jobs/{job['id']}/rename",
                       json={"speaker": "SPEAKER_09", "name": "x"})
    assert resp.status_code == 404


async def test_exports_are_available_in_all_formats(client, sample):
    job = await _finished_job(client, sample)
    assert "-->" in (await client.get(f"/api/jobs/{job['id']}/export?fmt=srt")).text
    assert "| Time |" in (await client.get(f"/api/jobs/{job['id']}/export?fmt=md")).text
    assert (await client.get(f"/api/jobs/{job['id']}/export?fmt=pdf")).status_code == 400


async def test_clip_endpoint_returns_wav(client, sample):
    job = await _finished_job(client, sample)
    resp = await client.get(f"/api/jobs/{job['id']}/clip?start=0.5&seconds=1.0")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.content[:4] == b"RIFF"


# -- overrides and config -------------------------------------------------

def test_overrides_are_whitelisted():
    cfg = apply_overrides(PipelineConfig(), {
        "asr": {"language": "ja", "model": "tiny"},
        "diarization": {"num_speakers": 4},
        "hf_token": "sneaky", "registry_path": "/etc/passwd",
    })
    assert cfg.asr.language == "ja" and cfg.asr.model == "tiny"
    assert cfg.diarization.num_speakers == 4
    # Neither of these is reachable from a request body.
    assert cfg.hf_token is None
    assert cfg.identification.registry_path != "/etc/passwd"


def test_overrides_reject_bad_values():
    with pytest.raises(ValueError):
        apply_overrides(PipelineConfig(), {"identification": {"mode": "Review"}})
    with pytest.raises(ValueError):
        apply_overrides(PipelineConfig(), {"merge": {"granularity": "wordy"}})
    with pytest.raises(ValueError):
        apply_overrides(PipelineConfig(), {"audio": {"chunk_seconds": 10,
                                                    "chunk_overlap_seconds": 20}})


async def test_config_defaults_hide_the_token(client):
    body = (await client.get("/api/config/defaults")).json()
    assert "hf_token" not in body["config"]
    assert body["config"]["identification"]["mode"] == "review"


async def test_config_file_round_trip(client, tmp_path):
    target = tmp_path / "ident.yaml"
    payload = {"path": str(target),
               "config": {"identification": {"accept_threshold": 0.81,
                                            "reject_threshold": 0.62, "margin": 0.12}}}
    written = await client.put("/api/config/file", json=payload)
    assert written.status_code == 200, written.text
    assert target.is_file()
    text = target.read_text(encoding="utf-8")
    assert "0.81" in text and "!!python" not in text

    read_back = (await client.get("/api/config/file", params={"path": str(target)})).json()
    assert read_back["config"]["identification"]["accept_threshold"] == 0.81


async def test_config_file_write_rejects_unknown_keys(client, tmp_path):
    resp = await client.put("/api/config/file", json={
        "path": str(tmp_path / "x.yaml"),
        "config": {"identification": {"nonsense": 1}},
    })
    assert resp.status_code == 400
    assert "unknown keys" in resp.json()["detail"]


# -- registry -------------------------------------------------------------

async def test_registry_crud_over_http(client, tmp_path):
    assert (await client.get("/api/registry")).json()["speakers"] == []
    from sdts.registry import SpeakerRegistry

    reg = SpeakerRegistry(tmp_path / "registry.db")
    try:
        sid = reg.add_speaker("Okada", np.array([1.0, 0.0, 0.0], dtype=np.float32),
                              consent_scope="persistent")
    finally:
        reg.close()

    listed = (await client.get("/api/registry")).json()
    assert [s["display_name"] for s in listed["speakers"]] == ["Okada"]
    assert "anchor" not in listed["speakers"][0]      # no vectors over the wire

    await client.post(f"/api/registry/{sid}/rename", json={"name": "Okada Taro"})
    assert (await client.get("/api/registry")).json()["speakers"][0]["display_name"] == "Okada Taro"

    await client.post(f"/api/registry/{sid}/revoke")
    assert (await client.get("/api/registry")).json()["speakers"] == []
    assert len((await client.get("/api/registry?include_revoked=true")).json()["speakers"]) == 1

    # Deleting biometric data needs an explicit confirmation flag.
    assert (await client.delete(f"/api/registry/{sid}")).status_code == 400
    assert (await client.delete(f"/api/registry/{sid}?confirm=true")).status_code == 200


# -- security posture -----------------------------------------------------

async def test_cross_origin_write_is_refused(client, sample):
    resp = await client.post("/api/jobs", json={"source": str(sample)},
                       headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403
    # Same-origin writes still work.
    ok = await client.post("/api/jobs", json={"source": str(sample)},
                     headers={"Origin": "http://127.0.0.1:8765"})
    assert ok.status_code == 201


async def test_delete_job_keeps_the_audio_by_default(client, sample):
    job = await _finished_job(client, sample)
    assert (await client.delete(f"/api/jobs/{job['id']}")).status_code == 200
    assert sample.is_file()                          # source untouched
    assert (await client.get(f"/api/jobs/{job['id']}")).status_code == 404


# -- replay and evaluation ------------------------------------------------

async def test_replay_sweeps_thresholds_without_touching_the_original(client, sample, app):
    from sdts.registry import SpeakerRegistry

    # `identify_speakers` reads the config registry path, i.e. the app's
    # setting - not tmp_path. Use the same database the request will use.
    reg_path = app.state.settings.registry_path
    reg = SpeakerRegistry(reg_path)
    try:
        reg.add_speaker("Taro", np.array([1.0] + [0.0] * 255, dtype=np.float32),
                        consent_scope="persistent")
    finally:
        reg.close()

    job = (await client.post("/api/jobs", json={
        "source": str(sample),
        "config": {"identification": {"enabled": True, "mode": "auto"}},
    })).json()["job"]
    final = await wait_done(client, job["id"])
    assert final["status"] == "done"
    original = (await client.get(f"/api/jobs/{job['id']}/result")).json()
    assert "identification" not in original["meta"]   # FakePipeline never identified

    resp = await client.post(f"/api/jobs/{job['id']}/replay", json={"runs": [
        {"name": "loose", "config": {"identification": {
            "enabled": True, "mode": "auto", "accept_threshold": 0.5}}},
        {"name": "strict", "config": {"identification": {
            "enabled": True, "mode": "auto", "accept_threshold": 1.01,
            "reject_threshold": 1.01}}},
    ]})
    assert resp.status_code == 200, resp.text
    runs = resp.json()["runs"]
    assert [r["name"] for r in runs] == ["loose", "strict"]
    loose = {d["speaker"]: d for d in runs[0]["decisions"]}
    strict = {d["speaker"]: d for d in runs[1]["decisions"]}
    assert loose["SPEAKER_00"]["decision"] == "accept"
    assert loose["SPEAKER_00"]["best"] == "Taro"
    assert strict["SPEAKER_00"]["decision"] == "unknown"

    listed = (await client.get(f"/api/jobs/{job['id']}/replays")).json()
    assert len(listed["runs"]) == 2
    replayed = (await client.get(
        f"/api/jobs/{job['id']}/replays/{runs[0]['id']}/result")).json()
    assert any(u["speaker_display"] == "Taro" for u in replayed["utterances"])

    # Replays are read-only: no score history / adaptation writes.
    reg = SpeakerRegistry(reg_path)
    try:
        assert reg.conn.execute("SELECT COUNT(*) FROM score_history").fetchone()[0] == 0
    finally:
        reg.close()
    # Original job result is byte-for-byte unchanged.
    assert (await client.get(f"/api/jobs/{job['id']}/result")).json() == original


async def test_eval_reports_perfect_score_on_identical_transcript(client, sample):
    job = await _finished_job(client, sample)
    reference = (await client.get(f"/api/jobs/{job['id']}/result")).json()
    resp = await client.post("/api/eval", json={
        "job_id": job["id"], "gold": reference, "language": "en"})
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["der"] == 0.0
    assert report["wer"] == 0.0
    assert report["wer_components"]["unit"] == "word"


def test_wer_uses_characters_for_japanese():
    from sdts.evaluate import word_error_rate

    assert word_error_rate("こんにちは", "こんにちは", character=True)["wer"] == 0.0
    assert word_error_rate("こんにちは", "こんにちは世界", character=True)["wer"] > 0


# -- CLI entry ------------------------------------------------------------

def test_serve_refuses_a_public_bind():
    from sdts.__main__ import main

    # `serve` must never accidentally expose a no-auth API on all interfaces.
    assert main(["serve", "--host", "0.0.0.0"]) == 2


def test_serve_reports_missing_optional_dependency(monkeypatch):
    import builtins

    from sdts.__main__ import main

    real_import = builtins.__import__

    def no_uvicorn(name, *args, **kwargs):
        if name == "uvicorn":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_uvicorn)
    assert main(["serve"]) == 2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
