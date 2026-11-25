# Speaker Diarized Transcription System (SDTS)

> Language: **English** | [日本語](README.ja.md)

Audio in, speaker-attributed transcript out:

```
[00:00:04.120] SPEAKER_00: それでは、本日の議題を始めます。
[00:00:09.480] SPEAKER_01: よろしくお願いします。
[00:00:12.010] SPEAKER_02: 一点だけ確認させてください。
```

Each line answers **who** spoke, **when** they started, **what** they said.
Output is **sentence-level**: original punctuation is kept and one line is one
complete sentence. Word-level timing is optional (`align.enabled: true` together
with `merge.granularity: word`) and off by default.

---

## 1. Architecture

```
            ┌─────────────────────────────────────────────────────────┐
            │  Stage 0  Ingest            ffmpeg                      │
  audio ──► │  any container → 16 kHz mono float32 + chunk planner    │
  (mp4,     └───────────────┬─────────────────────────────────────────┘
   mp3,                     │  (start, end) windows, 30 min, 15 s overlap
   wav,                     ▼
   m4a,     ┌─────────────────────────────────────────────────────────┐
   webm)    │  Stage 1  ASR               faster-whisper (CTranslate2)│
            │  → [{start, end, text, words[], language}]              │
            └───────────────┬─────────────────────────────────────────┘
                            │                    ┌──────────────────────────┐
                            │                    │ Stage 2  Diarization     │
                            │                    │ pyannote community-1     │
                            │                    │ → [{start,end,speaker}]  │
                            │                    └────────────┬─────────────┘
                            ▼                                 │
            ┌─────────────────────────────────────────────┐    │
            │  Stage 3  Alignment   OPTIONAL / off        │    │
            │  wav2vec2 CTC only for per-word timings     │    │
            └───────────────┬─────────────────────────────┘    │
                            └──────────────┬────────────────────┘
                                           ▼
            ┌─────────────────────────────────────────────────────────┐
            │  Stage 4  Fusion     sentence → speaker                │
            │  split only on real speaker changes; keep punctuation; │
            │  gap fill + micro-fragment absorption                   │
            └───────────────┬─────────────────────────────────────────┘
                            ▼
            ┌─────────────────────────────────────────────────────────┐
            │  Stage 5  Render     txt / json / srt / md              │
            └─────────────────────────────────────────────────────────┘
```

### Why two branches and not one "WhisperX --diarize"

WhisperX can do ASR+alignment+diarization in one call, and for a prototype that
is fine. It is the wrong shape for a product because:

- it runs diarization with pyannote 3.1-era defaults and pyannote's own speaker
  numbering, which you cannot control or reconcile across chunks;
- its `assign_word_speakers` picks the speaker with the largest word/turn
  intersection and *drops the word* when nothing overlaps - a silent data-loss
  path on short back-channels;
- you inherit WhisperX's pin on `pyannote-audio` and cannot upgrade diarization
  independently of alignment.

So this repo uses WhisperX **only as an alignment library** (importing
`whisperx.alignment`, never `whisperx.diarize`) and keeps diarization and fusion
in our own code. You get the alignment accuracy without the coupling.

---

## 2. Module map

| Module | Stage | Responsibility | Swappable? |
|---|---|---|---|
| `sdts/audio.py` | 0 | ffprobe/decode via ffmpeg, normalise to 16k mono, chunk planning | ffmpeg is the only dep; no torch |
| `sdts/asr.py` | 1 | ASR engines behind one `AsrEngine` protocol | yes - `faster-whisper`, `sensevoice` |
| `sdts/diarize.py` | 2 | pyannote pipeline, exclusive/regular view selection | yes - same `Turn` contract |
| `sdts/align.py` | 3 | wav2vec2 CTC forced alignment + graceful degradation | yes |
| `sdts/fuse.py` | 4 | **the core**: interval index, word→speaker, utterance building | no (this is the IP) |
| `sdts/render.py` | 5 | txt/json/srt/md renderers | yes |
| `sdts/pipeline.py` | all | orchestration, chunk loop, cross-chunk speaker reconciliation | no |
| `sdts/types.py` | all | `Word`, `Segment`, `Turn`, `Utterance`, `TranscriptResult` | the stable contract |
| `sdts/config.py` | - | dataclass config, YAML loader, token resolution | no |

`sdts/types.py` is the important seam. Every stage boundary exchanges these
dataclasses, never an engine's native dict. Swapping faster-whisper for
SenseVoice or pyannote for NeMo touches exactly one module.

---

## 3. Data flow (concrete)

```
meeting.mp4
  │  ffprobe → duration=3725.4 s, 48 kHz, 2 ch, aac
  ▼
chunks = [(0, 1800), (1785, 3585), (3570, 3725.4)]
  │
  ├─ chunk 0: decode → float32[28_800_000]
  │     diarize → [(0.0,12.4,"L0_SPEAKER_00"), (12.4,19.8,"L0_SPEAKER_01"), ...]
  │     asr      → [{start:0.0, end:9.6, text:"それでは本日は…", words:[…]}]
  │     align    → words get ±30 ms boundaries
  │     offset   → +0.0 s, keep only midpoint < 1785 s
  │
  ├─ chunk 1: same, offset +1785.0 s, keep midpoint in [1785, 3570)
  ├─ chunk 2: same, offset +3570.0 s (last chunk: keep to end)
  │
  ▼
turns = reconcile_speakers(all local turns)   # embeddings → global SPEAKER_0i
  │
  ▼
fuse(segments, turns)
  │  for each word: overlap → midpoint → dominant → snap
  │  group consecutive same-speaker words → utterances
  │
  ▼
TranscriptResult ──► out/meeting_ab12cd.{json,txt,srt,md}
```

### The chunk overlap contract

Chunks overlap by 15 s, but **each chunk only owns `[start, start+stride)`**.
A word or turn is kept by the chunk whose *core* range contains its midpoint.
The overlap exists purely so ASR and diarization have right-context at the
boundary - not so the same speech is transcribed twice. Get this wrong and you
get duplicated sentences at every 30-minute mark.

### Cross-chunk speaker identity

pyannote labels speakers per call (`SPEAKER_00`, `SPEAKER_01` within chunk N).
Those labels are meaningless across chunks. After collecting chunks we:

1. embed up to 5 longest turns per local cluster with
   `pyannote/wespeaker-voxceleb-resnet34-LM` (CC-BY-4.0);
2. average and L2-normalise the embeddings per cluster;
3. run agglomerative clustering on cosine distance with threshold 0.35,
   optionally capped by `num_speakers`;
4. map clusters to `SPEAKER_00..N` in a stable order.

~50 lines, deterministic, and the memory cost is constant per chunk. If the
embedding model cannot be loaded (offline install, blocked download), it falls
back to positional mapping **and logs a warning** - it never silently claims
identity it cannot verify.

---

## 4. Fusion: why the naive approach fails

`whisperx.assign_word_speakers` does roughly:

```python
for word in words:
    hits = [turn for turn in turns if overlaps(turn, word)]
    word["speaker"] = max(hits, key=intersection).speaker if hits else None
```

Three failure modes, all common:

| Failure | Cause | Our fix |
|---|---|---|
| Half a sentence assigned to the wrong person | Whisper segments often span a speaker change; segment-level assignment forces one label | assign per **word**, not per segment |
| Words disappear from the transcript | short back-channels (`うん`, `yeah`) are dropped by pyannote → no overlap → `None` | tier-4 snap to nearest turn within 0.5 s |
| Phantom speaker switch mid-word | ASR and diarization disagree on the boundary by 200 ms | require ≥60 % overlap before accepting, else use midpoint owner |

The four tiers in `_assign_word`:

1. **weighted overlap** ≥60 % of the word duration → that speaker;
2. **midpoint owner** when overlap is ambiguous (robust for Japanese particles);
3. **dominant overlap** as the last resort before giving up;
4. **nearest turn** within `snap_tolerance` for gap words.

Then `_build_utterances` groups runs of the same speaker, splits on pauses
longer than `merge_gap`, and absorbs sub-`min_utterance` fragments. Crucially,
absorption is asymmetric: a *different* speaker's fragment is only absorbed
when it is shorter than half the floor (that is a one-word leak), so genuine
short answers survive.

All of this is covered by 20 unit tests in `tests/test_pipeline.py`.

---

## 5. Technology choices and reasoning

| Layer | Choice | Why | Alternative rejected |
|---|---|---|---|
| Decode | system **ffmpeg** | handles every real-world container/codec; keeps torch out of the ingest path | torchaudio (deprecated I/O API), soundfile (no mp4) |
| ASR | **faster-whisper** + `large-v3-turbo` | same Whisper weights, 4x faster, working int8 quantisation, MIT, `word_timestamps` built in | openai/whisper (slow, no batching), whisper.cpp (weaker Python ecosystem) |
| Align | **wav2vec2 CTC + Viterbi** (WhisperX) | ±30 ms vs ±300 ms; this is what makes boundary attribution trustworthy | Whisper's native DTW on cross-attention (too coarse) |
| Diarization | **pyannote community-1** | best open DER available: AMI-IHM 17.0 %, CALLHOME 26.7 %, VoxConverse 11.2 %; self-contained repo for offline use; `exclusive` view already models overlap | NeMo Sortformer (`cc-by-nc` → **no commercial use**), pyannote 3.1 (worse on every corpus) |
| Speaker embed | **wespeaker-voxceleb-resnet34-LM** | shipped with the pipeline, CC-BY-4.0, good short-utterance behaviour | resemblyzer (weak on Japanese), ECAPA (extra dep) |
| Merge | **own code** | see §4; the libraries' built-in fusion loses data | WhisperX `assign_word_speakers` |

Deliberate non-choices:

- **No LLM post-processing** in the default path. It reads nicely but silently
  rewrites what people actually said, which is unacceptable for meeting minutes
  or compliance recordings. Add it as a clearly-labelled optional layer.
- **No forced `num_speakers` by default.** It is the single biggest accuracy
  lever, but guessing wrong is worse than not guessing. Pass it when known.

---

## 6. Licensing and commercial use

This matters more than the model accuracy, because one wrong assumption here
can sink a product. Verified against Hugging Face's model API on 2026-11-24.

| Component | Licence | Commercial use | Obligations / gotchas |
|---|---|---|---|
| `openai/whisper-large-v3-turbo` (weights) | **MIT** | ✅ yes | keep copyright notice. Note: `large-v3` is Apache-2.0, `turbo` is MIT - both fine |
| `openai/whisper` (code) | MIT | ✅ yes | - |
| `faster-whisper` | MIT | ✅ yes | - |
| `pyannote.audio` (library) | **MIT** | ✅ yes | - |
| `pyannote/speaker-diarization-community-1` (weights) | **CC-BY-4.0** | ✅ yes | **attribution required**; **gated** - accept the user agreement on HF and pass a token |
| `pyannote/wespeaker-voxceleb-resnet34-LM` | CC-BY-4.0 | ✅ yes | attribution required |
| `jonatasgrosman/wav2vec2-large-xlsr-53-japanese` | **Apache-2.0** | ✅ yes | NOTICE file |
| `jonatasgrosman/wav2vec2-large-xlsr-53-english` | Apache-2.0 | ✅ yes | NOTICE file |
| `WAV2VEC2_ASR_BASE_960H` (torchaudio bundle) | MIT (fairseq-derived) | ✅ yes | - |
| `WhisperX` | **BSD-2-Clause** | ✅ yes | keep the notice; do not use its name to endorse your product |
| `nvidia/diar_sortformer_4spk-v1` | **CC-BY-NC-4.0** | ❌ **NO** | non-commercial only - do not ship it |
| `FunAudioLLM/SenseVoiceSmall` (weights) | **FunASR Model Licence v1.1** | ✅ yes, conditionally | attribution + retain model name; community-conduct clause; licence may be revised unilaterally |
| `ffmpeg` (with `--enable-gpl`) | GPL-2.0+ | ⚠️ **build dependent** | see below |

### The three traps

**1. ffmpeg and GPL.** The common Windows/macOS "full" builds are compiled with
`--enable-gpl` (and sometimes `--enable-nonfree`). If you *redistribute* a
product bundled with such a binary, you take on GPL obligations for the
combination. Two clean ways out: (a) build ffmpeg with
`--disable-gpl --disable-nonfree` - libx264/libx265 are the GPL parts and you do
not need encoders, only decoders; or (b) require the user to install ffmpeg
themselves and shell out to it. For a SaaS backend this is a non-issue; for a
shipped desktop app it is not.

**2. pyannote CC-BY-4.0 is attribution, not permission.** You may use it
commercially. You must credit pyannote in your product documentation/about box
and cite the papers (Plaquet & Bredin 2023; Landini et al. 2022). "CC-BY" is
not "public domain" - strip the attribution and you lose the licence.

**3. Model terms can change under you.** Both the HF gated agreement and the
FunASR model licence allow the provider to revise terms. For a long-lived
product, snapshot the weights you shipped (and their licence text) into your own
artifact repository, so a future licence change cannot retroactively affect a
version you already released.

### If you want to drop the gated pyannote dependency

`speaker-diarization-community-1` requires accepting an agreement on HF with a
token. For a fully self-hosted/air-gapped deployment: `git lfs clone` the repo
once (it is self-contained), load it with
`Pipeline.from_pretrained("/path/to/local/dir")`, then never touch the network.

---

## 7. Setup

```bash
# 1. system dependency - decoders only, no GPL encoders needed
#    Debian/Ubuntu
sudo apt install ffmpeg
#    macOS
brew install ffmpeg
#    Windows: install a non-GPL build or use the conda-forge package

# 2. python 3.11 or 3.12 (NOT 3.13)
python3.12 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. accept the pyannote terms, then export a token
#    https://hf.co/pyannote/speaker-diarization-community-1
export HF_TOKEN=hf_xxxxxxxxxxxxxxxxx

# 4. sanity check - no models needed, no network
python tests/test_pipeline.py
python tests/test_integration.py
```

`torchcodec` (a pyannote 4.x dependency) needs a *matching* ffmpeg shared
library. If you see `Could not load libtorchcodec`, install ffmpeg via conda
(`conda install -c conda-forge ffmpeg`) - it ships the shared libs that the
system package may omit.

---

## 8. Usage

```bash
# Japanese meeting, 4 known speakers
python -m sdts meeting.mp4 --language ja --num-speakers 4

# English interview on CPU
python -m sdts interview.wav --language en --device cpu --compute-type int8

# check the speaker timeline before committing to a long ASR job
python -m sdts call.mp3 --diarize-only

# everything from a config file
cp configs/example-ja.yaml config.yaml
python -m sdts meeting.mp4 --config config.yaml
```

Output (`out/meeting_<hash>.txt`):

```
[00:00:04.120] SPEAKER_00: それでは、本日の議題を始めます。
[00:00:09.480] SPEAKER_01: よろしくお願いします。
```

JSON keeps per-word timing and confidence:

```json
{
  "language": "ja",
  "duration": 3725.4,
  "speakers": ["SPEAKER_00", "SPEAKER_01"],
  "speaker_stats": {"SPEAKER_00": {"turns": 42, "speech_seconds": 1204.3, "share": 0.323}},
  "utterances": [
    {"speaker": "SPEAKER_00", "start": 4.12, "end": 9.48,
     "text": "それでは、本日の議題を始めます。",
     "words": [{"start": 4.12, "end": 4.39, "text": "それでは", "score": 0.94}]}
  ]
}
```

---

## 9. Risks and mitigations

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| 1 | **Speaker count wrong** - more/fewer clusters than people | every label after the error is wrong | pass `num_speakers`; run `--diarize-only` first; review `speaker_stats` for implausibly short speakers |
| 2 | **Short back-channels dropped** by diarization | `うん`/`yeah` missing or mis-assigned | tier-4 snap; `snap_tolerance` 0.5 s |
| 3 | **Overlapping speech** (two people at once) | pyannote assigns one speaker; the other's words leak | `mode: exclusive` makes this explicit rather than hidden; consider OSD (`pyannote/overlapped-speech-detection`) to flag regions for human review |
| 4 | **Whisper hallucination** on silence/music | invented sentences | `vad_filter: true`; keep `condition_on_previous_text: true` for punctuation (it is not the main hallucination control) and set `no_repeat_ngram_size` if loops appear; reject segments with `avg_logprob < -1.0` |
| 5 | **Japanese word boundaries** are ill-defined | word-level attribution is fuzzier than English | default `granularity: segment` avoids the problem entirely; enable word mode only when per-word timing is actually needed |
| 6 | **Chunk boundary** cuts an utterance | half-sentence on both sides of a 30-min mark | 15 s overlap + midpoint ownership; increase overlap if you see it |
| 7 | **Cross-chunk speaker flip** | same person gets 2 labels | embedding reconciliation; if embeddings unavailable it degrades *loudly* |
| 8 | **RAM/VRAM growth** on long files | OOM at hour 3 | chunking keeps it flat; diarization is the peak consumer (~4-6 GB at 30 min) |
| 9 | **torch/torchcodec/ctranslate2 version skew** | install failures, runtime crashes | pinned requirements; Python 3.11/3.12 only |
| 10 | **Gated model access revoked / offline** | pipeline cannot start | `git lfs clone` the self-contained pipeline to disk |
| 11 | **Privacy**: audio and transcripts are the most sensitive data you can hold | legal exposure | run fully on-prem; pyannote telemetry off by default via `PYANNOTE_METRICS_ENABLED=0` |
| 12 | **Accuracy expectation mismatch** | users expect 100 % on names/jargon | `initial_prompt` glossary + a review UI; publish measured WER/DER for your languages, not vendor benchmarks |

### Measured expectations

Set these in your own acceptance test rather than trusting marketing numbers.
Reasonable targets for 16 kHz clean meeting audio:

| Audio condition | Expected WER (ja) | Expected DER |
|---|---|---|
| Close-talk, 2-3 speakers, no overlap | 8-15 % | 5-10 % |
| Meeting room, 4-6 speakers, mic array | 15-25 % | 10-20 % |
| Phone/WebRTC, crosstalk | 25-40 % | 20-35 % |

If your audio is 8 kHz telephony or heavily compressed, expect the bottom of
these ranges. Diarization degrades faster than ASR on narrowband audio.

---

## 10. Performance reference

**Actually measured** on an RTX 5090 (Blackwell, sm_120), torch 2.8.0+cu128,
on the 28.2-minute Japanese committee-hearing sample in `data/`:

| Stage | Time | Notes |
|---|---|---|
| decode (m4a → 16 kHz mono) | 1.6 s | ffmpeg, I/O bound |
| diarization (community-1, 4 speakers) | 13 s | RTF 0.008, ~125x realtime |
| ASR (large-v3-turbo, fp16) | ~50 s | VAD removed 27.6 s of silence |
| alignment | skipped | off by default for sentence output |
| fusion + render | <1 s | pure Python, interval index |

End-to-end **90 utterances / 4 speakers in ~83 s, RTF 0.047** (~21x realtime).
Diarization-only: `python -m sdts FILE --config configs/example-ja.yaml --diarize-only`.

A wider A10G / RTX 4090 reference for 1 h of 16 kHz audio, 4 speakers:

| Stage | Time |
|---|---|
| decode + chunk | ~5 s |
| ASR (large-v3-turbo, fp16) | ~55 s |
| diarization | ~110 s |
| fusion + render | <1 s |

CPU-only with int8 lands around 0.6-0.8 RTF, i.e. roughly realtime - usable
for overnight batches, not for interactive use.

### Why alignment and NLTK were removed, and where punctuation comes from

**wav2vec2 forced alignment (WhisperX's `alignment` module)** had one job:
turn a sentence's audio into precise *per-word/per-character* timestamps.
Whisper already emits timestamps, but only for whole segments, with roughly
±200-300 ms error. A CTC aligner trained on phoneme-to-character alignment can
place a boundary at ±30 ms. That matters only when you decide **who said each
individual word** - a sentence straddling a speaker change has to be cut at the
exact word where the person switches. It does not change *what* was said, and
it never adds punctuation: it consumes the ASR text as given. With
`merge.granularity: segment` there is no per-word attribution to do, so the
whole stage is dead weight (a model download plus a Viterbi pass).

**NLTK `punkt_tab`** is not an independent design choice at all. It is an
internal dependency of `whisperx.alignment.align()`, which calls
`nltk.sent_tokenize` on every segment before aligning, even for Japanese
(where it falls back to the *English* Punkt model). When the data is missing,
alignment raises, the exception is caught, and the run silently degrades to
segment timing after the entire file has been transcribed. Since alignment is
now off by default, neither wav2vec2 nor NLTK is needed. If you switch word
timing back on, `_ensure_nltk_data()` checks up front and fails with the
install command (`python -m nltk.downloader punkt_tab`) instead.

**Punctuation is produced by Whisper, not by post-processing.** The 4% rate in
the first run was caused by `condition_on_previous_text: false`: after the
short `initial_prompt` wore off, Whisper settled into an unpunctuated style.
Measured on a 170 s slice of the sample audio:

| Setting | Segments with punctuation |
|---|---|
| `condition_on_previous_text: false` + any prompt variant | 3/42 (7%) |
| `condition_on_previous_text: true` + any prompt variant | 31/31 (100%) |

A longer, more explicit Japanese prompt made no difference. With context
carry-over enabled on the full 28 min file: 97/98 sentences punctuated (99%),
no repeated-substring loops, and slightly fewer duplicate lines than before.
The usual worry about context carry-over - repetition loops - is handled by
`vad_filter` and, if needed, `no_repeat_ngram_size`; it is not a reason to
disable it.

An external punctuation-restoration model is **not** the right fix here. The
popular MIT-licensed `oliverguhr/fullstop-punctuation-multilang-large` supports
only English/Italian/French/German, so it cannot handle Japanese. The few
Japanese punctuation models on the Hub are essentially unmaintained (zero or
tens of downloads, no licence field), which makes them unusable under a
"permissive and commercially safe" requirement. Fixing the ASR setting is
both free and better.

### Real-audio findings worth knowing

- **Chunkiness**: pyannote splits on every pause. The 28-min sample came back
  as 422 fragments, 82% of which were the same speaker split twice. Setting
  `min_duration_off: 1.0` reduced that to 101 turns while keeping all 4
  speakers, including a 37 s chair who only announces names.
- **Sentence granularity**: keeping Whisper's own segments avoids rebuilding
  text from individual characters, which destroys Japanese punctuation.
  A sentence is split only when a second speaker holds ≥25% of it; the text
  break is then placed on a word boundary (or the nearest punctuation when
  word timings are absent).
- **Diarization overlap**: `min_duration_off` can extend one turn into the
  next. `_to_turns` trims the earlier turn at the later one's start so two
  speakers never hold the same instant.
- **Known model-level limit**: a chairman calling a name can be cut mid-name
  ("岡田和" / "也さん") and the tail attributed to the next speaker. This is
  a diarization boundary error, not a fusion bug; it needs a better model or
  `num_speakers` tuning, not more post-processing.
- **NLTK**: WhisperX calls `nltk.sent_tokenize` even for Japanese, using the
  English Punkt model. If alignment is enabled and `punkt_tab` is missing,
  word timing silently degrades to segment timing. `_ensure_nltk_data` now
  fails fast with the install command instead.

---

## 11. Extending

**Add an ASR engine** - subclass the protocol and register it:

```python
# sdts/asr.py
class MyEngine:
    name = "my-engine"
    def transcribe(self, audio, language=None):  # -> (list[Segment], str|None)
        ...

_ENGINES["my-engine"] = MyEngine
```

Nothing downstream changes as long as you return `Segment` with `Word` lists.

**Map clusters to real names.** The pipeline deliberately stops at
`SPEAKER_00`. Attaching names is enrolment, not diarization, and it needs a
consent/retention decision - so it is left out on purpose. When you are ready:
embed 3-5 s of each `SPEAKER_0i`, match against an enrolled voiceprint gallery
with the same wespeaker model, and set `Utterance.speaker_display` (already
plumbed through to the renderers). Guard it behind an explicit opt-in.

**Streaming.** The design is file-batch. For live audio, keep stages 1-3
(sliding window, ~5 s), but diarization must move to an online clusterer -
pyannote's pipeline is not online. Speaker identity would then be maintained by
embedding each new turn and matching against the running centroids from
`_cluster_embeddings`. That is a different project; do not bolt it onto this one.
