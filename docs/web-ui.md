# Local web workbench

A thin browser interface around the existing pipeline. It does **not** replace
the CLI: `python -m sdts file.mp4` remains the one-shot path, and `labels`,
`registry`, `enroll` and `calibrate` remain the scriptable path. The workbench
adds three things the CLI cannot give you — a live progress view for a long
job, a click-through transcript with per-speaker names, and a voice-gallery
manager.

The UI has three complete translations, **中文 / English / 日本語**. Pick one
in the language switcher at the top of every page; the choice is remembered in
`localStorage` and survives a reload and a server restart.

---

## 1. Start it

```bash
pip install "sdts[web]"              # fastapi + uvicorn + python-multipart
python -m sdts serve --open-browser  # http://127.0.0.1:8765/
```

Useful flags:

| Flag | Default | Notes |
|---|---|---|
| `--host` | `127.0.0.1` | A non-loopback bind is refused unless `--allow-remote` is passed. There is no auth; do not expose this on a LAN. |
| `--port` | `8765` | |
| `--data-dir` | `~/.sdts/workbench` | Job records (`jobs/*.json`), uploads and outputs (`out/`). |
| `--registry` | config value | SQLite voice gallery; defaults to `~/.sdts/registry.db`. |
| `--config` | none | Base YAML for the job form. New jobs copy its settings, then apply the form's overrides. |
| `--allow-remote` | off | Explicit opt-in to `--host 0.0.0.0`. Reads/writes are still same-origin checked, but there is no login. |

Stop with Ctrl+C. The web stack is an optional dependency group: without it,
`python -m sdts serve` prints an install hint and exits with status 2; every
non-web command is unaffected.

---

## 2. What each page does

**New job (`index.html`).** Upload a file or enter an absolute path to a file
already on this machine. The paths in the form are deliberately limited to ASR
language/model, chunking, known speaker count and identification mode — these
are the knobs that change transcription quality most often. Advanced or
site-specific settings stay in the YAML file. When voice matching is enabled,
choose the mode explicitly: **Suggest only** leaves names untouched, while
**Apply names automatically** writes accepted matches into `speaker_display`.
The recent-job table polls every 4 s; one job runs at a time and the rest wait
in the queue.

**Job (`job.html?id=…`).** Safe to open at any time, including immediately
after `POST /api/jobs` while the job is still `queued` — `/summary` returns the
live job state for every state and fills in the result fields once they exist.
The page opens an `EventSource` to `GET /api/jobs/{id}/events`, with a 3 s poll
of `/api/jobs/{id}` as a fallback if the stream is interrupted. Progress events
carry `stage`, `done`, `total`
and a coarse `progress` fraction; chunk numbering comes from the new
`Pipeline.run(on_stage=…)` hook. When the job finishes the page loads the
result JSON, lists one card per raw `SPEAKER_0i` cluster (with the
identification decision, best candidate and score when identification is on),
and lets you type a display name. A **one-click "Apply"** button appears for
`accept` decisions that have not been named yet — this is how a `review`-mode
job (including one produced before this button existed) gets its suggested name
without re-running transcription. Renaming writes `speaker_display` back to the
JSON and regenerates the `.txt`/`.srt`/`.md` exports; it is session-only and
never writes a voiceprint. Treat low scores with suspicion: diarization can
split one person into several clusters, and a confident-looking name on a
0.08-score cluster is not evidence. The ▶ button asks the server for a decoded WAV slice of the
source, so listening to a turn does not seek inside a compressed long file.
Cancel is cooperative: the cancel flag is checked on stage boundaries, so a
chunk already running must finish first.

**Voice gallery (`registry.html`).** Lists enrolled speakers, pending centroids
and consent scope; rename, revoke (keeps the record, stops matching) and hard
delete (requires `?confirm=true`) are explicit actions. Enrolment takes a path
to a one-speaker sample. The browser never receives an embedding vector — the
API returns names, counts and timestamps only.

**Settings (`settings.html`).** Shows the thresholds from the base config and
writes an edited YAML through `PipelineConfig.from_dict`, which rejects unknown
keys before anything is written. `hf_token` is never echoed to the browser.
The page links to `sdts calibrate` as the way to derive real thresholds; the
defaults remain placeholders.

---

## 3. How it fits into the code

```text
browser (sdts/web/, static, no build step)
   │  JSON + SSE
   ▼
FastAPI app (sdts/server/app.py)          ← optional dependency
   │  JobManager ── ThreadPoolExecutor(max_workers=1)
   │  EventBus ─── replay + fan-out to every tab
   ▼
existing core
   Pipeline.run(path, on_stage=…) → fuse → identify → render
   SpeakerRegistry / SpeakerEmbedder / labels / config
```

The only change to the core is the optional `on_stage` callback on
`Pipeline.run`. With no callback it adds one branch per stage; all 48
pre-existing tests pass unchanged. The callback is guarded: a listener that
raises is logged and ignored, except for a registered cancellation signal,
which is re-raised so the worker can stop the job deliberately.

Two details are worth knowing before extending this layer:

- **Stage replay is available but not wired to a UI yet.** The pure interfaces
  `Diarizer.diarize_array`, `fuse(segments, turns, cfg)` and
  `SpeakerEmbedder.embed_turns` mean a future "re-run with different thresholds"
  button can recompute the fusion/identification tail without repeating ASR.
  The current workbench submits a whole new job instead. Doing it properly is a
  second step, not a hidden feature of this one.
- **DER/WER evaluation is not in this PR.** `pyannote.metrics` is available in
  the environment but `jiwer` is not, and there is no gold-standard loader
  yet. The data model and the job results are the groundwork for an `eval`
  page, not the page itself.

---

## 4. REST surface

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | version, data dir, registry path |
| GET, POST | `/api/jobs` | list / create (multipart upload or JSON `source`) |
| GET, DELETE | `/api/jobs/{id}` | state / forget a job (audio kept unless `purge_files=true`) |
| GET | `/api/jobs/{id}/events` | SSE stream with history replay |
| POST | `/api/jobs/{id}/cancel` | cooperative cancel |
| GET | `/api/jobs/{id}/result` | full result JSON |
| GET | `/api/jobs/{id}/summary` | header facts + speaker cards |
| POST | `/api/jobs/{id}/rename` | set `speaker_display`, regenerate text formats |
| GET | `/api/jobs/{id}/export?fmt=json\|txt\|srt\|md` | download |
| GET | `/api/jobs/{id}/audio` | original file with range support |
| GET | `/api/jobs/{id}/clip?start=&seconds=` | decoded WAV slice |
| GET, POST | `/api/registry` | list / enrol |
| POST | `/api/registry/{id}/rename`, `/revoke` | maintain a gallery entry |
| DELETE | `/api/registry/{id}?confirm=true` | hard delete |
| GET | `/api/config/defaults` | effective defaults, token stripped |
| GET, PUT | `/api/config/file?path=` | read / write YAML |

Interactive docs are at `/api/docs`.

---

## 5. Security and limits

- The server binds to loopback by default and **refuses a public bind** without
  `--allow-remote`. There is no authentication, no multi-user isolation and no
  TLS: treat it like a local editor, not a service.
- Every mutating request with an `Origin` header must come from the same origin;
  this stops a page you happen to have open from driving the local API.
- Job config is filtered through a whitelist. A request cannot set `hf_token`
  or point `registry_path` at an arbitrary file. Uploaded names are reduced to
  `Path(name).name`, and audio is confined to the data directory.
- Job records are JSON files under `<data-dir>/jobs/`. A `queued` or `running`
  record left behind by a crash is marked `failed` on restart — the worker
  thread is not resumed.
- The event bus keeps the last 200 events per job. A client connecting late
  gets the replay, so a reload mid-job does not lose the earlier stages.
- Speaker names typed in the job page affect that job's outputs only. Storing
  a voiceprint is a separate, explicit enrolment.
- Config writes are restricted to `.yaml`/`.yml` and validated before being
  written. PyYAML `safe_dump` keeps the file free of Python-specific tags.

---

## 6. Tests

```bash
python -m pytest -q        # 80 tests, no GPU, no model downloads
```

`tests/test_server.py` drives the app in-process with `httpx.ASGITransport`
and a fake `Pipeline` that emits the same stage events as the real one. It
covers the job lifecycle (including failure, cancel and restart recovery),
SSE replay, rename/export consistency, config validation, registry CRUD, the
same-origin check, CLI guard rails, and completeness of the three dictionaries.
`tests/test_integration.py` pins the `on_stage` contract itself.
