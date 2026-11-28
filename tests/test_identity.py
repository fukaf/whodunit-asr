import json

import numpy as np

from sdts.config import AdaptationConfig, IdentificationConfig
from sdts.identify import identify_speakers
from sdts.labels import apply_labels, init_labels, load_result
from sdts.registry import SpeakerRegistry
from sdts.types import TranscriptResult, Turn, Utterance


class MockEmbedder:
    def __init__(self, vec=(1.0, 0.0, 0.0)):
        self.vec = np.asarray(vec, dtype=np.float32)
        self.vec = self.vec / np.linalg.norm(self.vec)

    def embed_audio(self, audio, sample_rate=16000):
        return self.vec.copy()

    def embed_turns(self, turns, chunk_audio, chunks, stride, **kwargs):
        return self.vec.copy()


def _result(speaker="SPEAKER_00"):
    return TranscriptResult(
        utterances=[Utterance(speaker=speaker, start=0.0, end=1.0, text="こんにちは")],
        speakers=[speaker], duration=2.0,
        meta={"source": "dummy.wav", "sample_rate": 16000, "codec": "wav", "channels": 1},
    )


def _cfg(mode="auto", adapt=None, registry_path="~/.sdts/registry.db", **kw):
    defaults = dict(enabled=True, mode=mode, accept_threshold=0.7,
                    reject_threshold=0.5, margin=0.1, registry_path=registry_path)
    defaults.update(kw)
    defaults["adaptation"] = adapt or AdaptationConfig()
    return IdentificationConfig(**defaults)


def test_registry_crud(tmp_path):
    reg = SpeakerRegistry(tmp_path / "r.db")
    sid = reg.add_speaker("张三", np.array([1, 0, 0], dtype=np.float32),
                          consent_scope="persistent")
    assert reg.get_speaker(sid)["display_name"] == "张三"
    assert len(reg.active_candidates()[0]["vectors"]) == 1

    cid = reg.add_centroid(sid, np.array([0.9, 0.1, 0], dtype=np.float32), status="pending")
    assert len(reg.list_centroids(status="pending")) == 1
    reg.approve_centroid(cid)
    assert len(reg.list_centroids(sid, "active")) == 1

    reg.rename_speaker(sid, "李四")
    assert reg.get_speaker(sid)["display_name"] == "李四"
    reg.delete_speaker(sid)
    assert reg.list_speakers() == []
    reg.close()


def test_identify_auto_writes_display(tmp_path):
    reg = SpeakerRegistry(tmp_path / "r.db")
    reg.add_speaker("张三", np.array([1, 0, 0], dtype=np.float32), consent_scope="persistent")
    reg.close()
    result = _result()
    meta = identify_speakers(result, turns=[Turn(0.0, 1.0, "SPEAKER_00")],
                             embedder=MockEmbedder(), cfg=_cfg(registry_path=str(tmp_path / "r.db")),
                             chunk_audio=[np.zeros(32000, dtype=np.float32)],
                             chunks=[(0.0, 2.0)], stride=0.5)
    assert result.utterances[0].speaker_display == "张三"
    assert meta["matches"][0]["decision"] == "accept"


def test_identify_review_does_not_write(tmp_path):
    reg = SpeakerRegistry(tmp_path / "r.db")
    reg.add_speaker("张三", np.array([1, 0, 0], dtype=np.float32), consent_scope="persistent")
    reg.close()
    result = _result()
    meta = identify_speakers(result, turns=[Turn(0.0, 1.0, "SPEAKER_00")],
                             embedder=MockEmbedder(), cfg=_cfg(mode="review", registry_path=str(tmp_path / "r.db")),
                             chunk_audio=[np.zeros(32000, dtype=np.float32)],
                             chunks=[(0.0, 2.0)], stride=0.5)
    assert result.utterances[0].speaker_display is None
    assert meta["matches"][0]["best"] == "张三"


def test_identify_unknown(tmp_path):
    reg = SpeakerRegistry(tmp_path / "r.db")
    reg.add_speaker("张三", np.array([1, 0, 0], dtype=np.float32), consent_scope="persistent")
    reg.close()
    result = _result()
    meta = identify_speakers(result, turns=[Turn(0.0, 1.0, "SPEAKER_00")],
                             embedder=MockEmbedder(vec=(0, 1, 0)), cfg=_cfg(registry_path=str(tmp_path / "r.db")),
                             chunk_audio=[np.zeros(32000, dtype=np.float32)],
                             chunks=[(0.0, 2.0)], stride=0.5)
    assert result.utterances[0].speaker_display is None
    assert meta["matches"][0]["decision"] == "unknown"


def test_adaptation_buffer_creates_pending(tmp_path):
    reg = SpeakerRegistry(tmp_path / "r.db")
    sid = reg.add_speaker("张三", np.array([1, 0, 0], dtype=np.float32),
                          consent_scope="persistent,adaptive")
    adapt = AdaptationConfig(enabled=True, mode="auto", min_turns=2, min_seconds=1.0)
    result = _result()
    identify_speakers(
        result,
        turns=[Turn(0.0, 0.6, "SPEAKER_00"), Turn(0.7, 1.3, "SPEAKER_00")],
        embedder=MockEmbedder(vec=(0.99, 0.01, 0)), cfg=_cfg(adapt=adapt, registry_path=str(tmp_path / "r.db")),
        chunk_audio=[np.zeros(32000, dtype=np.float32)],
        chunks=[(0.0, 2.0)], stride=0.5)
    reg2 = SpeakerRegistry(tmp_path / "r.db")
    assert len(reg2.list_centroids(sid, "pending")) == 1
    reg2.close()
    reg.close()


def test_labels_init_and_apply(tmp_path, monkeypatch):
    from sdts import audio as audio_mod

    source = tmp_path / "source.wav"
    audio_mod.write_wav(np.zeros(32000, dtype=np.float32), source)
    result_path = tmp_path / "meeting.json"
    result_path.write_text(json.dumps({
        "language": "ja", "duration": 2.0,
        "speakers": ["SPEAKER_00"],
        "utterances": [{"speaker": "SPEAKER_00", "start": 0.0, "end": 1.5,
                        "text": "こんにちは", "words": [], "speaker_display": "SPEAKER_00"}],
        "meta": {"source": str(source)},
    }, ensure_ascii=False), encoding="utf-8")

    paths = init_labels(result_path, clip_count=1)
    assert paths["labels"].is_file()
    assert paths["html"].is_file()
    html = paths["html"].read_text(encoding="utf-8")
    assert "application/json" in html and "<audio" in html

    labels = load_result(paths["labels"])
    labels["speakers"][0]["display_name"] = "张三"
    labels["speakers"][0]["persist"] = True
    paths["labels"].write_text(json.dumps(labels, ensure_ascii=False, indent=2),
                               encoding="utf-8")

    reg_path = tmp_path / "registry.db"
    out = apply_labels(result_path, paths["labels"], registry_path=str(reg_path),
                       embedder=MockEmbedder())
    assert out["persisted"]["SPEAKER_00"]
    assert any(p.suffix == ".srt" for p in map(lambda s: __import__('pathlib').Path(s),
                                               out["written"]))
    assert load_result(result_path)["utterances"][0]["speaker_display"] == "张三"
    reg = SpeakerRegistry(reg_path)
    assert reg.list_speakers()[0]["display_name"] == "张三"
    reg.close()


def test_labels_init_from_cli_passes_arguments(tmp_path, monkeypatch):
    """Regression: `labels init` must forward --registry under the arg name
    init_labels() actually accepts."""
    from sdts import __main__ as cli
    from sdts import audio as audio_mod

    source = tmp_path / "source.wav"
    audio_mod.write_wav(np.zeros(32000, dtype=np.float32), source)
    result_path = tmp_path / "meeting.json"
    result_path.write_text(json.dumps({
        "language": "ja", "duration": 2.0, "speakers": ["SPEAKER_00"],
        "utterances": [{"speaker": "SPEAKER_00", "start": 0.0, "end": 1.5,
                        "text": "こんにちは", "words": []}],
        "meta": {"source": str(source)},
    }, ensure_ascii=False), encoding="utf-8")

    reg_path = tmp_path / "custom.db"
    monkeypatch.chdir(tmp_path)
    code = cli.main(["labels", "init", str(result_path),
                     "--clips", "1", "--registry", str(reg_path)])
    assert code == 0
    assert (tmp_path / "meeting.labels.json").is_file()
    assert load_result(tmp_path / "meeting.labels.json")["registry"] == str(reg_path)


def test_labels_init_resolves_relative_source(tmp_path, monkeypatch):
    """Older results stored a CWD-relative meta.source; resolve it against the
    result file's directory instead of the current working directory."""
    from sdts import audio as audio_mod

    audio_mod.write_wav(np.zeros(32000, dtype=np.float32), tmp_path / "src.wav")
    result_path = tmp_path / "out" / "meeting.json"
    result_path.parent.mkdir()
    result_path.write_text(json.dumps({
        "language": "ja", "duration": 2.0, "speakers": ["SPEAKER_00"],
        "utterances": [{"speaker": "SPEAKER_00", "start": 0.0, "end": 1.5,
                        "text": "こんにちは", "words": []}],
        "meta": {"source": "src.wav"},
    }, ensure_ascii=False), encoding="utf-8")

    monkeypatch.chdir(tmp_path / "out")
    paths = init_labels(result_path, clip_count=1)
    assert paths["labels"].is_file()
    assert any((tmp_path / "out").glob("meeting_clips/*.wav"))


def test_clip_windows_spread_over_utterances():
    from sdts.labels import _clip_windows

    utts = [{"start": 0.0, "end": 31.0, "text": "a"}, {"start": 33.0, "end": 69.0, "text": "b"}]
    windows = _clip_windows(utts, clip_count=4, clip_seconds=8.0)
    assert len(windows) == 4
    # Both long turns contribute, instead of all windows coming from one.
    assert {id(utt) for utt, _, _ in windows} == {id(u) for u in utts}
    for _, start, end in windows:
        assert 0 < end - start <= 8.0


def test_clip_windows_skips_name_callouts():
    from sdts.labels import _clip_windows

    utts = [{"start": 0.0, "end": 40.0, "text": "long answer"},
            {"start": 50.0, "end": 50.9, "text": "岡田さん"}]
    windows = _clip_windows(utts, clip_count=2, clip_seconds=8.0)
    assert all(utt["text"] == "long answer" for utt, _, _ in windows)


def test_clip_windows_falls_back_when_all_short():
    from sdts.labels import _clip_windows

    utts = [{"start": 0.0, "end": 0.9, "text": "はい"}, {"start": 2.0, "end": 2.8, "text": "どうぞ"}]
    assert len(_clip_windows(utts, clip_count=2, clip_seconds=8.0)) == 2


def test_apply_labels_keeps_result_json_clean(tmp_path):
    """A speaker left unnamed must stay null in JSON, not echo the raw label."""
    from sdts import audio as audio_mod

    source = tmp_path / "source.wav"
    audio_mod.write_wav(np.zeros(32000, dtype=np.float32), source)
    result_path = tmp_path / "meeting.json"
    result_path.write_text(json.dumps({
        "language": "ja", "duration": 4.0, "speakers": ["SPEAKER_00", "SPEAKER_01"],
        "utterances": [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 1.5, "text": "はい", "words": []},
            {"speaker": "SPEAKER_01", "start": 1.6, "end": 3.5, "text": "どうぞ", "words": []},
        ],
        "meta": {"source": str(source)},
    }, ensure_ascii=False), encoding="utf-8")

    paths = init_labels(result_path, clip_count=1)
    labels = load_result(paths["labels"])
    labels["speakers"][0]["display_name"] = "张三"
    paths["labels"].write_text(json.dumps(labels, ensure_ascii=False), encoding="utf-8")

    apply_labels(result_path, paths["labels"], embedder=MockEmbedder())
    raw = load_result(result_path)["utterances"]
    assert raw[0]["speaker_display"] == "张三"
    assert raw[1]["speaker_display"] is None


def test_identification_mode_typo_is_rejected():
    """`mode: Review` must not degrade silently into "nothing identified"."""
    import pytest

    from sdts.config import PipelineConfig

    with pytest.raises(ValueError, match="identification.mode"):
        PipelineConfig.from_dict({"identification": {"enabled": True, "mode": "Review"}})
