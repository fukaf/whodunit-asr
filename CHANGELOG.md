# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The rules for writing entries and bumping versions live in
[`docs/versioning.md`](docs/versioning.md).

Change types: **Added** · **Changed** · **Fixed** · **Removed** · **Deprecated** · **Security**.

---

## [Unreleased]

Nothing yet.

---

## [0.3.0] - 2026-11-28

### Added

- **Local web workbench** (`python -m sdts serve`): a browser UI over the
  existing pipeline with four pages — new job (upload or local path), live job
  view, voice gallery and settings. Static `sdts/web/` assets need no build
  step; FastAPI/uvicorn are an optional `sdts[web]` extra, so the CLI install
  stays unchanged.
- **Three-language interface**: 中文 / English / 日本語 on every page, with the
  choice persisted in `localStorage` and mirrored to `<html lang>`. Missing keys
  fall back to English and are reported by `I18N.missing()`; a test asserts all
  three dictionaries have identical key sets.
- **Live job progress**: `Pipeline.run(on_stage=…)` emits `probe`, `chunk`,
  `reconcile`, `fuse`, `identify` and `done` events. The server fans them out
  over SSE with a 200-event replay, so a reload mid-job does not lose earlier
  stages. Passing no callback (every existing caller) changes nothing.
- **Cooperative cancellation**: a job cancel flag is checked on stage
  boundaries, the only point where stopping is cheap and safe. A chunk already
  running finishes first; the UI says so rather than pretending an instant stop.
- **Job state on disk**: `<data-dir>/jobs/<id>.json` records survive a page
  reload. A job left `queued`/`running` by a crash is marked `failed` on restart
  instead of appearing to run forever.
- **In-browser transcript review**: per-`SPEAKER_0i` cards show identification
  decisions/scores, accept a display name, and regenerate the `txt`/`srt`/`md`
  exports. A ▶ button fetches a server-decoded WAV slice of that turn.
- **Voice-gallery management**: list / enrol / rename / revoke / delete
  through the UI. Vectors never leave the process; hard delete requires an
  explicit `?confirm=true`.
- **Config endpoint**: reads/writes YAML through `PipelineConfig.from_dict` so
  unknown keys and bad enums fail before anything is written; `hf_token` is
  stripped from every response.
- **Documentation**: `docs/web-ui.md` (start, page guide, REST table, security
  posture, limits) and a linked section in both READMEs.

### Changed

- New jobs go through a single-worker `ThreadPoolExecutor`: one transcription
  at a time so two jobs cannot compete for the same GPU memory. Later jobs wait
  in `queued`.
- The JSON written at the end of a job now keeps `speaker_display: null` for
  unnamed speakers. Previously `Utterance.to_dict()` silently substituted the
  raw `SPEAKER_0i` label, which made the rename form look pre-filled. The same
  correction already existed in `labels.apply_labels`.
- `sdts dev` now includes `pytest-asyncio`; the ASGI tests run in-process with
  `httpx.ASGITransport` and need no port, GPU or model download.

### Fixed

- Voice matching in the new-job form silently used `mode: review`, so accepted
  matches were displayed as suggestions but never applied to the transcript.
  The form now asks for the mode explicitly (`review` = suggest only, `auto` =
  apply names), defaults to `auto` when the box is ticked, and the job page
  shows an **Apply** button with the candidate name for accepted matches —
  including jobs produced by the old UI. A bare score is no longer shown
  without the name it belongs to.
- Opening a job page while the job was still queued or running returned `409`
  from `/api/jobs/{id}/summary`, so the page never subscribed to SSE and stayed
  at `...` until a manual reload. `/summary` now answers for every job state
  (with empty result fields until the job is done), the page polls `/api/jobs`
  every 3 s as a fallback if the event stream is interrupted, and a finished
  job is loaded even if the page missed the `result` event.

### Security

- The workbench binds to `127.0.0.1` by default and **refuses a non-loopback
  bind** unless `--allow-remote` is passed. There is no authentication or TLS,
  so it must not be treated as a service.
- Write requests carrying an `Origin` header must be same-origin, preventing a
  page open in the same browser from driving the local API.
- Job config is applied through a whitelist: a request cannot set `hf_token` or
  `registry_path`, filenames are reduced to their basename, and audio is
  confined to the workbench data directory.

### Known limitations

- The workbench does not yet offer stage replay: changing a threshold submits a
  whole new job instead of re-running only fusion/identification. The pure
  stage interfaces needed for that already exist.
- DER/WER evaluation and threshold sweeps are not in this change. The job
  results are groundwork for a future `eval` page.
- Cancellation is cooperative and only checked between chunks.
- No authentication, multi-user isolation or consent expiry — same
  local-trusted-machine posture as the registry.

---

## [0.2.0] - 2026-11-28

Optional speaker identity: `SPEAKER_0i` clusters can now be labelled with real
names, either as a one-off session alias or as a voice that is remembered and
matched automatically in later recordings. Off by default; enabling it is a
per-config decision because it stores biometric data.

### Added

- **Local voice registry** (`sdts/registry.py`): SQLite store with one
  L2-normalised anchor vector per speaker, up to `max_centroids` adapted
  centroids, an adaptation buffer, an audit log and a score history. Single-user,
  no encryption — intended for a trusted local machine only.
- **Cluster-level identification** (`sdts/identify.py`): runs after fusion and
  writes only `Utterance.speaker_display`, never the ASR text or the raw label.
  Cosine `max(anchor, centroids)` against every registered speaker, an
  accept threshold plus a runner-up margin, a grey zone that goes to review, and
  an UNKNOWN exit that keeps `SPEAKER_0i`.
- **Post-hoc labelling workflow** (`sdts/labels.py`):
  - `labels init` writes a labels JSON, 5 short WAV clips per speaker and a
    self-contained `review.html` that plays from `file://` with no server;
  - `labels apply` writes the names back, re-renders every output format,
    persists only the voices explicitly ticked, and regenerates the review page
    so the next speaker does not have to be listened to twice;
  - `labels set` sets a single session alias with no registry write at all.
- **CLI subcommands**: `labels init|apply|set`, `registry list|show|rename|delete|adapt-queue|adapt-approve`,
  `enroll` (register a voice from a sample file) and `calibrate`.
- **Threshold calibration** (`sdts/calibrate.py`): labelled same/different pairs
  CSV → FAR/FRR/EER sweep → `identification.yaml` with the two thresholds.
  Reported EER and the FAR achieved at the accept threshold; refuses to run on
  one-class data.
- **Controlled adaptation**: accepted matches above `accept_threshold +
  update_margin` accumulate into `pending` centroids, never promoted silently;
  drift in the recent score window raises `needs_re_enrollment`.
- **Shared speaker embedder** (`sdts/embedding.py`): the wespeaker model is now
  loaded once and used for cross-chunk reconciliation, identification and
  enrolment instead of once per stage.
- **Configuration**: `identification` section with `enabled`, `mode`
  (`off`/`review`/`auto`), thresholds, margin, `max_centroids` and an
  `adaptation` subsection. Invalid `mode` strings now raise at load time.
- **Documentation**: `docs/speaker-identity-plan.md` (full design, decision
  table, schema, risks), a speaker-identity section in both READMEs, and a
  commented `identification` block in `configs/example-ja.yaml`.

### Changed

- `Pipeline` reuses the shared `SpeakerEmbedder`; the private
  `_embedding_model` handle and the duplicated crop logic in `pipeline.py` are
  gone.
- `meta.source` in the result JSON is now an absolute path, so `labels init`
  works from any working directory. Older results with a relative path are
  still resolved against the result file's directory.
- `pyproject.toml` and `sdts.__version__` bumped to `0.2.0`.

### Fixed

- `labels apply` wrote `speaker_display: "SPEAKER_01"` for speakers left
  unnamed, because `Utterance.to_dict()` falls back to the raw label; the JSON
  now keeps `null`.
- Drift detection could be triggered by a single recording when several
  clusters of the same person each contributed a score; it now records one vote
  per speaker per run.
- The review page rounded sample duration down (`8.2` → `8 秒`); it now shows
  one decimal.
- `labels init` crashed on `init_labels() got an unexpected keyword argument
  'registry'` — the CLI passed `registry=` where the function takes
  `registry_path=`. Covered by a CLI regression test.
- `labels init` produced a single clip for a speaker who only has one long
  utterance; long utterances are now sliced into several windows, and 1–2 s
  turns (usually the chair calling a name) are skipped unless nothing longer
  exists.
- Clip paths containing spaces or CJK broke `<audio src>`; they are now
  URL-encoded.

### Security

- Voice embeddings are only written when "save to the voice gallery" is ticked;
  the default is a session alias with nothing persisted. Consent scope is
  recorded per speaker and `registry revoke` exists. This is still
  local-trusted-machine grade: no encryption, no per-user permissions, no
  consent expiry — see the limitations in `docs/speaker-identity-plan.md`.

### Known limitations

- The default thresholds (0.70 / 0.55) are placeholders; run `calibrate` on
  your own audio before switching `mode` to `auto`.
- Clips are cut on turn boundaries and can start mid-word.
- Diarization errors propagate: a cluster that mixes two people will still be
  matched confidently. The review page warns below 10 s of usable audio.

---

## [0.1.1] - 2026-11-25

### Added

- `README.ja.md`: full Japanese translation of the README, with a language
  switcher at the top of both files.

---

## [0.1.0] - 2026-11-25

First public release: sentence-level speaker-diarized transcription.

### Added

- **Ingest** (`sdts/audio.py`): ffprobe/ffmpeg decode of any container to
  16 kHz mono float32, plus a chunk planner (30 min windows, 15 s overlap) that
  keeps GPU memory flat on multi-hour files.
- **ASR** (`sdts/asr.py`): `faster-whisper` (`large-v3-turbo` by default) and
  an optional SenseVoice engine behind one protocol.
- **Diarization** (`sdts/diarize.py`): pyannote `speaker-diarization-community-1`,
  with `exclusive` / `regular` view selection and `min_duration_off` gap filling
  (1.0 s turned 422 fragments into 101 turns on the reference recording).
- **Cross-chunk speaker reconciliation** (`sdts/pipeline.py`): wespeaker
  embeddings + agglomerative clustering on cosine distance (threshold 0.35),
  optional `num_speakers` cap, and a loud positional fallback when embeddings
  are unavailable.
- **Fusion** (`sdts/fuse.py`): per-word speaker attribution in four tiers
  (weighted overlap ≥60 %, midpoint owner, dominant overlap, nearest turn within
  `snap_tolerance`), sentence-preserving utterance building, asymmetric
  micro-fragment absorption so short answers survive.
- **Optional alignment** (`sdts/align.py`): wav2vec2 CTC forced alignment via
  WhisperX's alignment module for per-word timings; off by default because
  sentence-level output does not need it.
- **Rendering** (`sdts/render.py`): `txt`, `json`, `srt` and `md`, the last with
  a speaker/statistics table.
- **Configuration** (`sdts/config.py`): dataclass config buildable from code,
  YAML or CLI flags; HF token resolution from config → env → `hf auth login`
  store.
- **CLI** (`sdts/__main__.py`): `python -m sdts FILE` with model, device,
  language, chunking, speaker-count and output overrides, plus
  `--diarize-only` for a cheap speaker-count check.
- **Testing**: 32 unit and integration tests (29 in `test_pipeline.py`, 3 in
  `test_integration.py`) covering fusion edge cases, the chunk ownership
  contract and an offline end-to-end run with mocked models.
- **Docs**: README with architecture, licensing table (including the CC-BY-NC
  and GPL traps), measured performance on an RTX 5090, risk table and the
  punctuation findings behind `condition_on_previous_text: true`.

### Changed

- Punctuation comes from Whisper with `condition_on_previous_text: true`
  (3 % → 99 % of Japanese sentences punctuated) rather than from a
  post-processing model.

### Notes

- `pyannote/speaker-diarization-community-1` and the wespeaker embedding model
  are gated on Hugging Face and require accepting the terms.

---

## Release links

`v0.2.0` and `v0.3.0` are real tags and their links resolve. `0.1.0` / `0.1.1`
were shipped before tagging started, so their sections are dated records of
commits rather than checkouts (see `docs/versioning.md` §3 and §6).

[Unreleased]: https://github.com/fukaf/whodunit-asr/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/fukaf/whodunit-asr/releases/tag/v0.3.0
[0.2.0]: https://github.com/fukaf/whodunit-asr/releases/tag/v0.2.0
[0.1.1]: https://github.com/fukaf/whodunit-asr/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/fukaf/whodunit-asr/releases/tag/v0.1.0
