# 話者分離文字起こしシステム（SDTS）

> 言語: [English](README.md) | **日本語** ・ バージョン: **0.2.0** ・ [変更履歴](CHANGELOG.md) ・ [リリース手順](docs/versioning.md) ・ [Web ワークベンチ](docs/web-ui.md)

音声を入れると、話者が紐づいた文字起こしが出てきます：

```
[00:00:04.120] SPEAKER_00: それでは、本日の議題を始めます。
[00:00:09.480] SPEAKER_01: よろしくお願いします。
[00:00:12.010] SPEAKER_02: 一点だけ確認させてください。
```

1行ごとに「**誰が**、**いつ**、**何を**話したか」が分かります。出力は**文単位**で、元の句読点はそのまま残り、1行が1つの完全な文になります。単語単位のタイムスタンプは任意（`align.enabled: true` と `merge.granularity: word` を併用）で、デフォルトはオフです。

---

## 1. アーキテクチャ

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

### なぜ単一の "WhisperX --diarize" にしないのか

WhisperX は1回の呼び出しで ASR + アライメント + 話者分離をまとめて実行でき、プロトタイプならそれでも十分です。しかし製品にするには形が適切ではありません。

- pyannote 3.1 時代のデフォルト設定と pyannote 独自の話者番号付けで話者分離が走るため、チャンク間で制御したり照合したりできません。
- `assign_word_speakers` は単語とターンの重なりが最大の話者を選び、重なりがなければ**単語を捨てます**。短い相槌で気づかないうちにデータが失われる経路です。
- WhisperX が固定している `pyannote-audio` のバージョンをそのまま継承するため、アライメントとは独立して話者分離だけをアップグレードできません。

そのため本リポジトリでは WhisperX を**アライメントライブラリとしてだけ**使い（`whisperx.alignment` を import し、`whisperx.diarize` は決して使いません）、話者分離と統合は自前のコードに置いています。結合を避けつつ、アライメントの精度だけを受け取れます。

---

## 2. モジュール構成

| モジュール | ステージ | 責務 | 差し替え可能か |
|---|---|---|---|
| `sdts/audio.py` | 0 | ffprobe による情報取得と ffmpeg デコード、16k モノラルへの正規化、チャンク分割 | ffmpeg のみ依存、torch 不要 |
| `sdts/asr.py` | 1 | 単一の `AsrEngine` プロトコル背後の ASR エンジン | 可 — `faster-whisper`、`sensevoice` |
| `sdts/diarize.py` | 2 | pyannote パイプライン、exclusive / regular ビューの選択 | 可 — `Turn` 契約は同じ |
| `sdts/align.py` | 3 | wav2vec2 CTC 強制アライメントと graceful degradation | 可 |
| `sdts/fuse.py` | 4 | **コア**：区間インデックス、単語→話者の割当て、発言の構築 | 不可（ここが本プロジェクトの肝） |
| `sdts/render.py` | 5 | txt / json / srt / md のレンダラー | 可 |
| `sdts/pipeline.py` | 全体 | オーケストレーション、チャンクループ、チャンク間話者の照合 | 不可 |
| `sdts/types.py` | 全体 | `Word`、`Segment`、`Turn`、`Utterance`、`TranscriptResult` | 安定した契約 |
| `sdts/config.py` | - | dataclass 設定、YAML ローダー、トークン解決 | 不可 |

`sdts/types.py` が重要な境界面です。ステージ境界ではエンジン固有の dict ではなく、必ずこれらのデータクラスをやり取りします。faster-whisper を SenseVoice に、pyannote を NeMo に差し替えても、触るのは1つのモジュールだけで済みます。

---

## 3. データフロー（具体例）

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

### チャンクオーバーラップの契約

チャンクは15秒ずつ重なっていますが、**各チャンクが所有するのは `[start, start+stride)` だけ**です。単語やターンは、その**中心点**を含むチャンクにのみ残されます。オーバーラップは境界で ASR と話者分離に右文脈を持たせるためだけで、同じ発言を2回文字起こしするためではありません。ここを間違えると、30分ごとに重複文が出現します。

### チャンクをまたぐ話者の同一性

pyannote は呼び出しごとに話者をラベル付けします（チャンク N 内では `SPEAKER_00`、`SPEAKER_01`）。これらのラベルはチャンクをまたいでは無意味です。全チャンクを集めた後で、次の処理を行います。

1. 各ローカルクラスターについて、最大5件の最長ターンを `pyannote/wespeaker-voxceleb-resnet34-LM`（CC-BY-4.0）で埋め込み表現化する。
2. クラスターごとに埋め込みを平均し、L2正規化する。
3. コサイン距離に対して凝集型クラスタリングを実行し、閾値0.35で分割する（必要なら `num_speakers` で上限を指定）。
4. クラスターを安定した順序で `SPEAKER_00..N` にマッピングする。

約50行で、決定的に動作し、チャンクごとのメモリコストは一定です。埋め込みモデルが読み込めない場合（オフライン環境やダウンロードがブロックされた場合）は、位置情報によるマッピングにフォールバックし、**警告をログに出します**。検証できない同一性を黙って主張することはありません。

---

## 4. 統合：素朴な方法が失敗する理由

`whisperx.assign_word_speakers` はおおよそ次のような処理をします。

```python
for word in words:
    hits = [turn for turn in turns if overlaps(turn, word)]
    word["speaker"] = max(hits, key=intersection).speaker if hits else None
```

よくある失敗が3つあります。

| 失敗 | 原因 | 本実装での対処 |
|---|---|---|
| 文の半分が別の人のものになる | Whisper のセグメントは話者交代またぐことが多く、セグメント単位の割当てだと1つのラベルに決め打ちされる | **単語単位**で割り当てる |
| 単語が文字起こしから消える | `うん` や `yeah` のような短い相槌が pyannote で検出されず、重なりが取れず `None` になる | 4段目のスナップで0.5秒以内の直近ターンに割り当てる |
| 単語の途中で話者が切り替わる | ASR と話者分離の境界が200msずれる | 重なりが60%以上なければ受け付けず、中点の所有者を使う |

`_assign_word` の4段階は次のとおりです。

1. **重み付き重なり**が単語長の60%以上 → その話者に確定。
2. 重なりが曖昧な場合は**中点の所有者**（日本語の助詞に強い）。
3. 最後の手段として**最大重なり**を使う。
4. ギャップ中の単語は `snap_tolerance` 以内の**直近ターン**にスナップ。

その後 `_build_utterances` が同じ話者の連続をまとめ、`merge_gap` より長い休止で分割し、`min_utterance` 未満の断片を吸収します。重要なのは吸収が非対称な点で、**別の話者**の断片は下限の半分より短い場合（1単語の漏れ）にだけ吸収されます。そのため短い本気の返答は残ります。

これらは `tests/test_pipeline.py` の20個のユニットテストでカバーされています。

---

## 5. 技術選定とその理由

| 層 | 選択 | 理由 | 見送った代替 |
|---|---|---|---|
| デコード | システムの **ffmpeg** | 実世界のあらゆるコンテナ / コーデックを扱え、入力経路に torch を混入させない | torchaudio（I/O API が非推奨）、soundfile（mp4 非対応） |
| ASR | **faster-whisper** + `large-v3-turbo` | Whisper 重みは同じで約4倍高速、int8 量子化も動作、MIT、`word_timestamps` 標準搭載 | openai/whisper（遅い・バッチなし）、whisper.cpp（Python エコシステムが弱い） |
| アライメント | **wav2vec2 CTC + Viterbi**（WhisperX） | ±30ms（Whisper 単体は ±300ms）。境界の帰属を信用できるようにする鍵 | Whisper の cross-attention 上 DTW（粗すぎる） |
| 話者分離 | **pyannote community-1** | 公開モデル中最良の DER：AMI-IHM 17.0%、CALLHOME 26.7%、VoxConverse 11.2%。単体リポジトリでオフライン利用可。`exclusive` ビューが重なりを初期から表現 | NeMo Sortformer（`cc-by-nc` → **商用不可**）、pyannote 3.1（全コーパスで劣る） |
| 話者埋め込み | **wespeaker-voxceleb-resnet34-LM** | パイプラインに同梱、CC-BY-4.0、短い発言に強い | resemblyzer（日本語に弱い）、ECAPA（依存が増える） |
| 統合 | **自前コード** | §4 参照。ライブラリ標準の統合はデータを失う | WhisperX `assign_word_speakers` |

意図的に選ばなかったもの：

- デフォルト経路に **LLM による後処理を入れていません**。読みやすくはなりますが、実際に言われた内容を黙って書き換えます。議事録やコンプライアンス録音には許容できません。入れるなら、明確にラベル付きの任意レイヤーにしてください。
- デフォルトで `num_speakers` を**強制していません**。精度に効く最大のレバーですが、誤った指定は指定しない場合より厄介です。分かっているときだけ渡してください。

---

## 6. ライセンスと商用利用

モデルの精度より重要です。ここでの誤解1つで製品が潰れます。2026-11-24 に Hugging Face の model API で確認済みです。

| コンポーネント | ライセンス | 商用利用 | 義務 / 注意点 |
|---|---|---|---|
| `openai/whisper-large-v3-turbo`（重み） | **MIT** | ✅ 可 | 著作権表示を残す。`large-v3` は Apache-2.0、`turbo` は MIT — どちらも問題なし |
| `openai/whisper`（コード） | MIT | ✅ 可 | - |
| `faster-whisper` | MIT | ✅ 可 | - |
| `pyannote.audio`（ライブラリ） | **MIT** | ✅ 可 | - |
| `pyannote/speaker-diarization-community-1`（重み） | **CC-BY-4.0** | ✅ 可 | **クレジット表記必須**。**ゲート付き** — HF で利用規約に同意しトークンを渡す必要あり |
| `pyannote/wespeaker-voxceleb-resnet34-LM` | CC-BY-4.0 | ✅ 可 | クレジット表記必須 |
| `jonatasgrosman/wav2vec2-large-xlsr-53-japanese` | **Apache-2.0** | ✅ 可 | NOTICE ファイル |
| `jonatasgrosman/wav2vec2-large-xlsr-53-english` | Apache-2.0 | ✅ 可 | NOTICE ファイル |
| `WAV2VEC2_ASR_BASE_960H`（torchaudio バンドル） | MIT（fairseq 由来） | ✅ 可 | - |
| `WhisperX` | **BSD-2-Clause** | ✅ 可 | 通知を残す。製品名に WhisperX の名前を推奨表示として使わない |
| `nvidia/diar_sortformer_4spk-v1` | **CC-BY-NC-4.0** | ❌ **不可** | 非商用専用 — 出荷しないこと |
| `FunAudioLLM/SenseVoiceSmall`（重み） | **FunASR Model Licence v1.1** | ✅ 条件付き可 | 帰属表示とモデル名の保持、コミュニティ規約条項、ライセンスが一方的に改訂される可能性 |
| `ffmpeg`（`--enable-gpl` 付き） | GPL-2.0+ | ⚠️ **ビルド依存** | 下記参照 |

### 3つの落とし穴

**1. ffmpeg と GPL。** Windows / macOS の一般的な「full」ビルドは `--enable-gpl`（さらに `--enable-nonfree` の場合も）でコンパイルされています。そのバイナリを同梱した製品を**再配布**すると、組み合わせ全体が GPL の義務を負います。回避策は2つです。(a) `--disable-gpl --disable-nonfree` で ffmpeg をビルドする — GPL 部分は libx264 / libx265 で、エンコーダではなくデコーダだけが必要です。または (b) ユーザー自身に ffmpeg をインストールしてもらい、外部プロセスとして呼び出す。SaaS バックエンドなら問題になりませんが、同梱デスクトップアプリでは問題になります。

**2. pyannote の CC-BY-4.0 は「帰属」であって「無償利用」ではない。** 商用利用は可能です。ただし製品ドキュメントや about 画面に pyannote のクレジットを記載し、論文を引用する必要があります（Plaquet & Bredin 2023、Landini ら 2022）。「CC-BY」は「パブリックドメイン」ではありません。帰属を外せばライセンスも失われます。

**3. モデルの規約は後から変わる。** HF のゲート利用規約も FunASR のモデルライセンスも、提供者による改訂を認めています。長期運用する製品では、出荷した重みとそのライセンス文書を自社のアーティファクトリポジトリにスナップショットとして保存してください。将来ライセンスが変わっても、リリース済みバージョンに遡及して影響しなくなります。

### ゲート付き pyannote 依存を外したい場合

`speaker-diarization-community-1` は HF で規約に同意しトークンを渡す必要があります。完全自己ホスト型 / エアギャップ環境では、まず `git lfs clone` でリポジトリを一度ローカルに取得し（単体で完結しています）、`Pipeline.from_pretrained("/path/to/local/dir")` で読み込めば、以後ネットワークに触れる必要はありません。

---

## 7. セットアップ

```bash
# 1. システム依存関係 — デコーダのみ、GPL エンコーダは不要
#    Debian/Ubuntu
sudo apt install ffmpeg
#    macOS
brew install ffmpeg
#    Windows: GPL 以外のビルドを入れるか conda-forge パッケージを使う

# 2. Python 3.11 または 3.12（3.13 は不可）
python3.12 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. pyannote の利用規約に同意してからトークンを export
#    https://hf.co/pyannote/speaker-diarization-community-1
export HF_TOKEN=hf_xxxxxxxxxxxxxxxxx

# 4. 動作確認 — モデルもネットワークも不要
python tests/test_pipeline.py
python tests/test_integration.py
```

`torchcodec`（pyannote 4.x の依存）は**対応する** ffmpeg 共有ライブラリを必要とします。`Could not load libtorchcodec` が出た場合は、conda 経由で ffmpeg をインストールしてください（`conda install -c conda-forge ffmpeg`）。システムパッケージには含まれない共有ライブラリが同梱されています。

---

## 8. 使い方

```bash
# 日本語の会議、話者数が4と分かっている場合
python -m sdts meeting.mp4 --language ja --num-speakers 4

# 英語のインタビューを CPU で処理
python -m sdts interview.wav --language en --device cpu --compute-type int8

# 長い ASR ジョブを走らせる前に話者のタイムラインを確認
python -m sdts call.mp3 --diarize-only

# 設定ファイルですべて指定
cp configs/example-ja.yaml config.yaml
python -m sdts meeting.mp4 --config config.yaml
```

出力例（`out/meeting_<hash>.txt`）：

```
[00:00:04.120] SPEAKER_00: それでは、本日の議題を始めます。
[00:00:09.480] SPEAKER_01: よろしくお願いします。
```

JSON には単語単位のタイムスタンプと確信度も入ります。

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

## 9. リスクと緩和策

| # | リスク | 影響 | 緩和策 |
|---|---|---|---|
| 1 | **話者数が違う** — 実際より多い / 少ないクラスター | 以降のすべてのラベルが誤る | `num_speakers` を渡す。まず `--diarize-only` を実行し、`speaker_stats` で不自然に短い話者がいないか確認 |
| 2 | 短い相槌が話者分離で**落とされる** | `うん` / `yeah` が欠落または誤割当て | 4段目のスナップ、`snap_tolerance` 0.5秒 |
| 3 | **発言の重なり**（2人が同時に話す） | pyannote は片方の話者を割り当て、もう片方の単語が漏れる | `mode: exclusive` で暗黙ではなく明示的に扱う。OSD（`pyannote/overlapped-speech-detection`）で区間にフラグを立てて人手レビューに回すことも検討 |
| 4 | 無音・音楽に対する **Whisper の幻覚** | 存在しない文が生成される | `vad_filter: true`。句読点のために `condition_on_previous_text: true` を維持（これは幻覚の主な制御ではない）。ループが出たら `no_repeat_ngram_size` を設定。`avg_logprob < -1.0` のセグメントを除外 |
| 5 | **日本語の単語境界**が曖昧 | 単語単位の帰属が英語より曖昧 | デフォルトの `granularity: segment` で問題を回避。単語タイミングが本当に必要なときだけ word モードを有効化 |
| 6 | **チャンク境界**で発言が切れる | 30分地点の前後で文が半分になる | 15秒のオーバーラップと中点所有。見かけたらオーバーラップを増やす |
| 7 | **チャンク間で話者が入れ替わる** | 同じ人が2つのラベルを持つ | 埋め込みによる照合。埋め込みが使えない場合は**大きく警告**して劣化する |
| 8 | 長時間ファイルで **RAM / VRAM が増加** | 3時間目で OOM | チャンク化で一定に維持。話者分離が最大の消費元（30分で約4〜6GB） |
| 9 | **torch / torchcodec / ctranslate2 のバージョン不整合** | インストール失敗、実行時クラッシュ | requirements をピン留め、Python 3.11 / 3.12 のみ |
| 10 | **ゲート付きモデルのアクセスが取り消される / オフライン** | パイプラインが起動しない | 単体パイプラインを `git lfs clone` してディスクに保存 |
| 11 | **プライバシー**：音声と文字起こしは最も機微なデータ | 法的リスク | 完全オンプレで実行。`PYANNOTE_METRICS_ENABLED=0` で pyannote のテレメトリはデフォルトでオフ |
| 12 | **精度の期待値ズレ** | 固有名詞や専門用語で100%を期待される | `initial_prompt` に用語集、レビュー UI を用意。ベンダー値ではなく、自言語で計測した WER / DER を公開する |

### 現実的な期待値

ベンダーの数字を信じず、自社の受け入れテストで以下を設定してください。16kHz のクリーンな会議音声に対する目安です。

| 音声条件 | WER 目安（ja） | DER 目安 |
|---|---|---|
| 接話マイク、2〜3人、重なりなし | 8〜15% | 5〜10% |
| 会議室、4〜6人、マイクアレイ | 15〜25% | 10〜20% |
| 電話 / WebRTC、クロストーク | 25〜40% | 20〜35% |

8kHz の電話音声や強く圧縮された音声では、レンジの下側になります。狭帯域音声では ASR より話者分離の方が早く劣化します。

---

## 10. パフォーマンス参考値

RTX 5090（Blackwell、sm_120）、torch 2.8.0+cu128 で、`data/` の28.2分の日本語委員会音声サンプルを使って**実際に計測**した値です。

| ステージ | 時間 | 備考 |
|---|---|---|
| デコード（m4a → 16kHz モノラル） | 1.6秒 | ffmpeg、I/O 律速 |
| 話者分離（community-1、4人） | 13秒 | RTF 0.008、約125倍速 |
| ASR（large-v3-turbo、fp16） | 約50秒 | VAD が27.6秒の無音を除去 |
| アライメント | スキップ | 文出力ではデフォルトでオフ |
| 統合 + レンダリング | 1秒未満 | 純 Python、区間インデックス |

全体で **90発言 / 4話者を約83秒、RTF 0.047**（約21倍速）。話者分離のみ：`python -m sdts FILE --config configs/example-ja.yaml --diarize-only`。

1時間の16kHz音声・4話者における、やや広めの A10G / RTX 4090 の参考値：

| ステージ | 時間 |
|---|---|
| デコード + チャンク分割 | 約5秒 |
| ASR（large-v3-turbo、fp16） | 約55秒 |
| 話者分離 | 約110秒 |
| 統合 + レンダリング | 1秒未満 |

CPU のみで int8 を使うと RTF は 0.6〜0.8、つまりおおよそリアルタイムです。一晩バッチには使えますが、対話用途には向きません。

### なぜアライメントと NLTK を外したのか、句読点はどこから来るのか

**wav2vec2 強制アライメント（WhisperX の `alignment` モジュール）**の役割は1つだけでした。文の音声を正確な**単語・文字単位**のタイムスタンプに変換することです。Whisper もタイムスタンプを出しますが、セグメント単位だけで誤差は約 ±200〜300ms です。音素と文字の対応で学習した CTC アライナーなら ±30ms で境界を置けます。これは**個々の単語を誰が話したか**を決めるときにだけ意味があります。話者交代またぐ文は、人が切り替わる正確な単語で切る必要があるからです。何が言われたかは変わりませんし、句読点を足すこともありません。与えられた ASR テキストをそのまま消費します。`merge.granularity: segment` なら単語単位の帰属処理は不要なので、このステージ全体が無駄な重量（モデルのダウンロードと Viterbi パス）になります。

**NLTK の `punkt_tab`** は設計上の選択ではなく、`whisperx.alignment.align()` の内部依存です。この関数はアライメント前に各セグメントへ `nltk.sent_tokenize` を呼びます。日本語でも例外ではなく（英語の Punkt モデルにフォールバックします）、データが無いとアライメントが例外を投げ、例外が捕捉されて、ファイル全体の文字起こしが終わった後に黙ってセグメント単位のタイムスタンプへ劣化していました。アライメントは現在デフォルトでオフなので、wav2vec2 も NLTK も不要です。単語タイミングを再び有効にした場合、`_ensure_nltk_data()` が事前にチェックし、インストールコマンド（`python -m nltk.downloader punkt_tab`）付きで即座に失敗します。

**句読点は後処理ではなく Whisper が生成しています。** 初回の4%という数字は `condition_on_previous_text: false` が原因でした。短い `initial_prompt` が効かなくなると、Whisper は句読点のない文体に収束します。サンプル音声の170秒区間で計測：

| 設定 | 句読点のあるセグメント |
|---|---|
| `condition_on_previous_text: false` + プロンプトはどのバリエーションでも | 3/42（7%） |
| `condition_on_previous_text: true` + プロンプトはどのバリエーションでも | 31/31（100%） |

より長く明示的な日本語プロンプトでも差はありませんでした。28分全体で文脈持ち越しを有効にした結果、97/98文に句読点（99%）、繰り返し部分列ループはなく、重複行も以前よりわずかに減少しました。文脈持ち越しの懸念である繰り返しループは `vad_filter` と、必要なら `no_repeat_ngram_size` で扱えます。無効化する理由にはなりません。

外部の句読点復元モデルは、ここでは適切な解決策**ではありません**。人気のある MIT ライセンスの `oliverguhr/fullstop-punctuation-multilang-large` は英語 / イタリア語 / フランス語 / ドイツ語のみで、日本語を扱えません。Hub 上の少数の日本語句読点モデルは事実上メンテナンス停止（ダウンロード数がゼロか数十、ライセンス欄なし）で、「緩いライセンスで商用安全」という要件では使えません。ASR 設定を直す方が無料で、しかも良い結果になります。

### 実音声で分かった注意点

- **細かく切りすぎる**：pyannote は休止ごとに分割します。28分のサンプルは422断片で、82%が同じ話者の二重分割でした。`min_duration_off: 1.0` にすると101ターンに減り、名前を告げるだけの37秒の議長を含め4話者すべてが残りました。
- **文単位の粒度**：Whisper 自身のセグメントを残すことで、文字単位にバラしてテキストを再構築し、日本語の句読点を壊す問題を避けられます。別の話者が25%以上を占める場合だけ文を分割し、その位置は単語境界（単語タイミングがなければ最寄りの句読点）に置きます。
- **話者分離の重なり**：`min_duration_off` で1つのターンが次のターンに食い込むことがあります。`_to_turns` が後のターンの開始時刻で前のターンをトリミングし、同じ瞬間を2人が占有しないようにします。
- **モデルレベルの既知の限界**：議長が名前を呼ぶと、名前の途中（"岡田和" / "也さん"）で切れて末尾が次の話者に割り当てられることがあります。これは話者分離の境界エラーであり、統合のバグではありません。より良いモデルか `num_speakers` の調整が必要で、後処理を足す話ではありません。
- **NLTK**：WhisperX は日本語でも英語の Punkt モデルで `nltk.sent_tokenize` を呼びます。アライメントを有効にして `punkt_tab` が無いと、単語タイミングが黙ってセグメントタイミングに劣化します。`_ensure_nltk_data` がインストールコマンド付きで即座に失敗するようになりました。

---

## 11. 話者身份登録（任意機能）

デフォルトはオフです。有効にすると、ローカルの声紋レジストリを使って
`SPEAKER_0i` を実際の表示名に変換できます。

手順：

```bash
# 1. 通常どおり文字起こし
python -m sdts meeting.mp4 --config config.yaml

# 2. ラベル雛形・短い音声クリップ・ローカル確認ページを生成
python -m sdts labels init out/meeting_ab12cd.json
#   -> out/meeting_ab12cd.labels.json
#   -> out/meeting_ab12cd.review.html
#   -> out/meeting_ab12cd_clips/

# 3. review.html を開いて試聴し、氏名を入力。永続化したい声にチェックを入れて
#    labels JSON をエクスポート

# 4. ラベルを適用し、全形式を再出力して選択した声を登録
python -m sdts labels apply out/meeting_ab12cd.json \
    --labels out/meeting_ab12cd.labels.json
```

以降は `identification.enabled: true` の新しい音声で登録済みの声が自動照合され、
`speaker_display` に反映されます。未登録のクラスタは `SPEAKER_0i` のままです。
`mode: review`（既定）は候補を JSON の meta に記録するだけ、`mode: auto` は
`accept_threshold` 以上の一致を書き込みます。2つの閾値と margin で曖昧な一致を
除外し、すべてローカルに監査記録が残ります。

```yaml
identification:
  enabled: false
  registry_path: "~/.sdts/registry.db"
  mode: review
  accept_threshold: 0.70   # 暫定値。auto の前に較正してください
  reject_threshold: 0.55
  margin: 0.10
  adaptation:
    enabled: false
    mode: review
```

レジストリ操作：`python -m sdts registry list|show|rename|delete|
adapt-queue|adapt-approve`、および `enroll` / `calibrate` コマンド。
レジストリは単一ユーザーの信頼できるローカル環境向けの平文 SQLite で、
暗号化や複数ユーザー対応は行いません。設計の詳細は
`docs/speaker-identity-plan.md` を参照してください。

---

## 12. 拡張する

**ASR エンジンを追加する** — プロトコルをサブクラス化して登録します。

```python
# sdts/asr.py
class MyEngine:
    name = "my-engine"
    def transcribe(self, audio, language=None):  # -> (list[Segment], str|None)
        ...

_ENGINES["my-engine"] = MyEngine
```

`Segment` と `Word` のリストを返す限り、下流は一切変わりません。

---

## 13. ローカル Web ワークベンチ（任意機能）

端末では扱いにくい2つの作業——長いジョブの進捗確認と、書き起こしをクリックしながら話者に名前を付ける作業——のためのブラウザ UI です。Web 関連パッケージは**任意依存**で、CLI の動作は変わりません。

```bash
pip install "sdts[web]"
python -m sdts serve --open-browser       # http://127.0.0.1:8765/
```

画面は4つです。新規ジョブ（アップロードまたはローカルパスと主要な上書き項目、声紋照合は「提案のみ」か「名前を自動入力」を明示選択）、ジョブ詳細（SSE による段階別進捗、話者カードと一致候補のワンクリック採用、その場での改名、WAV クリップ、書き出し）、声紋ライブラリ（登録・改名・無効化・削除）、設定（閾値と YAML 編集）。画面は **中文 / English / 日本語** の3言語に対応し、各ページ上部の切替で選択できます。選択は保存され、リロードや再起動後も引き継がれます。

既定では `127.0.0.1` にバインドし、`--allow-remote` なしでの公開アドレスバインドは拒否します。書き込みリクエストは `Origin` を検査し、声紋ベクトルがブラウザに送られることはありません。ジョブ画面での改名はそのジョブだけに効き、声紋の登録は別の明示的な操作です。詳しくは [`docs/web-ui.md`](docs/web-ui.md) を参照してください。

**クラスターを実名にマッピングする**。パイプラインは意図的に `SPEAKER_00` で止めています。名前を付けるのは話者分離ではなく登録（enrolment）であり、同意と保存期間の判断が必要なので、意図的に省いています。準備ができたら：`SPEAKER_0i` ごとに3〜5秒を埋め込み表現化し、同じ wespeaker モデルで登録済み声紋ギャラリーと照合し、`Utterance.speaker_display`（すでにレンダラーまで配線済み）を設定します。明示的な opt-in の背後に置いてください。

**ストリーミング**。本設計はファイルバッチです。ライブ音声ではステージ1〜3を（約5秒のスライディングウィンドウで）残せますが、話者分離はオンラインクラスタラーに置き換える必要があります。pyannote のパイプラインはオンラインではありません。話者同一性は、新しいターンごとに埋め込みを作り、`_cluster_embeddings` の現在の重心と照合して維持することになります。これは別プロジェクトです。無理に本プロジェクトに載せないでください。
