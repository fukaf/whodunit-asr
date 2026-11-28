# 说话人身份注册与识别：开发规划

日期：2026-11-27
状态：设计已定稿，尚未实现

---

## 0. 已拍板的决策

| # | 问题 | 决策 |
|---|---|---|
| 1 | 交互方式 | 不做带后端的 Web UI。第一版用「结果文件 + 标注模板 JSON + 本地静态 HTML 试听页 + CLI 命令」完成标记与注册，保证方便编辑和批量操作。 |
| 2 | 插入点 | 按正确流程：身份映射在 `fuse()` **之后**应用到 `Utterance.speaker_display`，不在 `reconcile_speakers()` 后直接写。 |
| 3 | embedding 来源 | 抽出可复用的 `embed_turns()`；识别阶段独立取 embedding，不依赖 `_reconcile_speakers()` 的输出；多 chunk 时复用缓存。 |
| 4 | 阈值 | 按推荐：accept/reject 两阈值，中间灰区进 review；margin 仅在候选人数 ≥2 时生效；阈值必须标定，默认值只作占位。 |
| 5 | 注册库运维 | 本地可信环境、单用户。暂不做加密、密钥管理和复杂同意流程；只保留最小同意标记与审计表。明确标注为本地方案，不适合共享/合规部署。 |

---

## 1. 范围与非目标

### 目标

- 对一段已转录的结果，按 `SPEAKER_0i` 选择说话人并标记真实姓名。
- 标记可只作用于本次结果（会话别名），也可显式持久化到声纹库。
- 处理新音频时，自动查询持久库，把识别出的说话人显示为真实姓名。
- 声纹库可随新的高质量样本受控更新，以适应通道/环境/声音变化，同时防止漂移。

### 非目标

- 不从转写文本里抽人名（NER/共指）。这是不可靠的另一条路。
- 不做带后端的 Web UI、不做多用户/权限系统；本地静态 HTML 试听页是允许的，不依赖服务。
- 不做加密存储、KMS、GDPR/PIPL 级合规设施；定位为本地可信环境工具。
- 不重新训练模型；识别是推理时对 centroid 的查表。
- 不自动把会议里任意声音注册成某个人；必须显式确认。

---

## 2. 总体数据流

```
输入音频
  │
  ├─ 现有链路：chunk → diarize → ASR → fuse()
  │
  ├─ [新增] embed_turns()：按全局 SPEAKER_0i 聚合 embedding
  │
  ├─ [新增] identify_speakers()：与 registry 比对，得到 speaker → display 映射
  │           ├─ 高分       → display_name
  │           ├─ 灰区/低分  → UNKNOWN / 保留 SPEAKER_0i / review 候选
  │           └─ 失败降级   → 不识别，仅告警
  │
  ├─ fuse() 之后：按 u.speaker 写 u.speaker_display
  │
  └─ 现有 renderer：txt / json / srt / md（无需改动）
```

关键约束：识别发生在 `fuse()` 之后，因为 `speaker_display` 属于 `Utterance`；但 embedding 取自 `Turn`，所以 `embed_turns()` 需要同时拿到 turns 与原始 chunk 音频。

---

## 3. 模块划分与代码改动点

| 文件 | 改动 | 说明 |
|---|---|---|
| `sdts/embedding.py` | 新增 | `SpeakerEmbedder`：封装 wespeaker `Inference`；提供 `embed_turns(turns, chunk_audio, chunks, stride, max_turns=5, min_duration=0.3)`。失败返回 `None`。 |
| `sdts/registry.py` | 新增 | SQLite schema、CRUD、匹配、自适应缓冲、审计日志。 |
| `sdts/labels.py` | 新增 | 标注模板生成、短片段裁剪、`review.html` 生成、labels apply 解析与幂等写回。 |
| `sdts/identify.py` | 新增 | `identify_speakers(result, turns, embedder, cfg)` → `speaker → display` 映射 + 分数明细；`adapt_registry()`。 |
| `sdts/pipeline.py` | 修改 | `_cluster_embedding()` 迁到 `embedding.py` 并复用；`run()` 在 `fuse()` 后应用识别映射；`meta` 增加 `identification` 明细。 |
| `sdts/config.py` | 修改 | `PipelineConfig` 增加 `identification` 段；`from_dict()` 的 `sections` 同步加入。 |
| `sdts/__main__.py` | 修改 | 增加 `labels` / `enroll` / `registry` / `calibrate` 子命令（或独立入口）。 |
| `sdts/render.py` | 不改 | 已使用 `u.label`，`to_dict()` 已输出 `speaker_display`。 |
| `sdts/types.py` | 不改（或仅补 meta 类型） | `Utterance.speaker_display` 与 `label` 已存在。 |

注意：`_reconcile_speakers()` 在单 chunk 或单簇时早返回（`pipeline.py:144`），不产生 embedding。因此识别**不能**复用它的输出；`embed_turns()` 必须能独立工作。多 chunk 场景下可把它算过的 embedding 缓存进 `Pipeline`，避免重复加载模型。

---

## 4. 声纹库数据结构（SQLite）

默认路径：`~/.sdts/registry.db`，由 `identification.registry_path` 覆盖。

```sql
CREATE TABLE speakers (
    id            TEXT PRIMARY KEY,         -- UUID，稳定标识
    display_name  TEXT NOT NULL,             -- 可重复、可改名
    anchor        BLOB NOT NULL,             -- float32 向量，冻结不更新
    created_at    TEXT NOT NULL,
    consent_scope TEXT,                      -- 例如 "persistent,adaptive"
    consented_at  TEXT,
    revoked_at    TEXT
);

CREATE TABLE centroids (
    id            TEXT PRIMARY KEY,
    speaker_id    TEXT NOT NULL REFERENCES speakers(id),
    vector        BLOB NOT NULL,             -- L2 归一化 float32
    source_file   TEXT,
    source_start  REAL,
    source_end    REAL,
    score         REAL,                      -- 加入时与 anchor 的相似度
    condition     TEXT,                      -- JSON: sample_rate/codec/channels/snr
    created_at    TEXT NOT NULL,
    consented_at  TEXT
);

CREATE TABLE audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    action      TEXT NOT NULL,               -- enroll/rename/revoke/adapt/delete/identify
    speaker_id  TEXT,
    centroid_id TEXT,
    source_file TEXT,
    score       REAL,
    detail      TEXT                         -- JSON
);
```

- `speaker_id` 与 `display_name` 分离，允许同名不同人。
- 删除单条 adapted centroid 只删 `centroids` 行；删除整个人则同时删 `speakers` 与其所有 `centroids`，并写审计墓碑。
- 加载时过滤 `revoked_at IS NOT NULL`。
- 本地可信环境假设：SQLite 文件明文存储，**不做加密**；文档需明确说明不能用于共享主机或受监管场景。

---

## 5. 配置 schema

```yaml
identification:
  enabled: false                 # 总开关
  registry_path: "~/.sdts/registry.db"
  mode: review                   # off | review | auto
  unknown_label: null            # null -> 保留 SPEAKER_0i

  accept_threshold: 0.70         # 占位值，必须标定
  reject_threshold: 0.55         # 占位值，必须标定
  margin: 0.10                   # 仅候选 >=2 时生效

  max_centroids: 6               # 每个 speaker 的 adapted 上限（不含 anchor）
  adaptation:
    enabled: false
    mode: review                 # review | auto
    update_margin: 0.05          # T_update = accept_threshold + update_margin
    min_turns: 3
    min_seconds: 30.0
    drift_window: 5              # 连续 N 份音频
    drift_threshold: 0.60
```

`PipelineConfig.from_dict()` 的 `sections` 字典需要加入 `identification`，否则 YAML 会被判为未知键。阈值均为**未标定占位值**；首次运行应打警告，提示执行 `calibrate`。

---

## 6. CLI 交互设计（本地静态 HTML，无后端 Web UI）

### 6.1 结果标注 workflow（主路径）

```bash
# 1) 从已有结果生成标注模板、试听片段和本地静态页面
python -m sdts labels init out/meeting_ab12cd.json
#    -> out/meeting_ab12cd.labels.json      标注模板
#    -> out/meeting_ab12cd.review.html      本地试听/填名页面
#    -> out/meeting_ab12cd_clips/           每个说话人 3-5 段短音频

# 2) 用户编辑模板（只需填 display_name，想入库的设 persist=true）
#    {
#      "source": "out/meeting_ab12cd.json",
#      "speakers": [
#        {
#          "id": "SPEAKER_00",
#          "display_name": "张三",
#          "persist": true,
#          "hint": "[00:00:04.120] それでは、本日の議題を始めます。 | [00:01:12.300] ..."
#        }
#      ]
#    }

# 3) 应用标注：写 speaker_display、可选注册入库、重新渲染全部格式
python -m sdts labels apply out/meeting_ab12cd.json \
    --labels out/meeting_ab12cd.labels.json --registry ~/.sdts/registry.db
```

`labels init` 必须把每个说话人的若干代表语句（含时间）写进模板，并同时裁出每个说话人 3–5 段短音频（建议 5–10 秒）和 `review.html`。页面只做三件事：分组列出说话人及其代表语句、点击播放对应短片段、填写 `display_name` 与 `persist` 勾选，最后导出/写回 labels JSON。浏览器不参与 embedding 和写库。`labels apply` 从 `result.meta["source"]` 指向的原音频按时间戳裁剪样本，走 `embed_turns()` 生成 anchor。使用短片段而不是整段音频，是为了绕开浏览器本地文件访问限制，并让试听更快；同一批片段可直接复用于 embedding。

### 6.2 快速单条标记

```bash
python -m sdts labels set out/meeting_ab12cd.json --speaker SPEAKER_00 --name 张三
```

### 6.3 直接注册（备用）

```bash
python -m sdts enroll --name 张三 --audio sample.wav --consent "persistent,adaptive"
```

### 6.4 声纹库维护

```bash
python -m sdts registry list
python -m sdts registry show <speaker_id>
python -m sdts registry rename <speaker_id> --name 李四
python -m sdts registry delete <speaker_id>
python -m sdts registry adapt-queue        # 查看待确认的自适应 centroid
python -m sdts registry adapt-approve <centroid_id>
```

### 6.5 阈值标定

```bash
python -m sdts calibrate --pairs pairs.csv --out identification.yaml
# pairs.csv: file_a,file_b,same_speaker(0/1)
# 输出 EER、目标 FAR 下的建议阈值与 margin
```

---

## 7. 匹配与判定规则

相似度：L2 归一化向量之间的余弦相似度，范围 [-1, 1]。

1. 计算 cluster embedding 与库中所有 centroid（anchor + adapted）的相似度，取每个 speaker 的最大分。
2. 按分数降序排列，得到 best 与 second。
3. 判定：
   - `best >= accept_threshold` 且（候选数 < 2 或 `best - second >= margin`）→ 接受，写 `display_name`。
   - `reject_threshold <= best < accept_threshold` → 灰区：进 review 队列；`mode=review` 时不写 display，`mode=auto` 时仍不写（灰区永不自动接受）。
   - `best < reject_threshold` → UNKNOWN。
4. `unknown_label` 为 `null` 时保留原 `SPEAKER_0i`；非 null 时写该标签，但不能与任何 display_name 冲突。
5. registry 缺失、损坏、embedding 不可用 → fail-safe：不识别，记录 warning，流程继续。

`mode`:
- `off`：完全不识别。
- `review`：只产出候选与置信度到 `result.meta["identification"]`，人工确认后通过 `labels apply` 写入。
- `auto`：满足 accept 规则时直接写 `speaker_display`。

---

## 8. 自适应更新机制

每个 speaker 有：

- **anchor**：注册时的向量，冻结永不更新。
- **adapted centroids**：最多 `max_centroids` 个，新增时若满则替换最旧或质量最低的一个。
- 匹配时取所有 centroid 的最大相似度。

写入 adapted centroid 的条件（全部满足）：

1. 本次匹配分数 `>= accept_threshold + update_margin`；
2. 领先第二名满足 margin（候选 ≥2 时）；
3. turn 通过质量门：VAD、无重叠、SNR 达标、时长足够；
4. 累积至少 `min_turns` 个 turn 且 `min_seconds` 秒音频，取均值后 L2 归一化；
5. `adaptation.mode=auto` 直接写入；`review` 进队列等人工批准。

漂移检测：滚动统计已接受匹配的分数；连续 `drift_window` 份音频低于 `drift_threshold` 时标记“需要重新注册”，**不再继续吸纳**，由人确认。

审计：每次 adapt 写 `audit` 表，含来源文件、分数、时间、consent scope。删除任一 adapted centroid 不得影响 anchor。

---

## 9. 分阶段实施

| 阶段 | 内容 | 交付物 / 验收 |
|---|---|---|
| P1 基础层 | `embedding.py` 抽离 `embed_turns()`；`registry.py` schema + CRUD | 单测通过；单 chunk 也能取到 embedding；旧 `_reconcile_speakers()` 行为不变 |
| P2 识别集成 | `identification` 配置；`identify.py`；`pipeline.run()` 在 fuse 后写 display；meta 明细 | 用 mock embedding 跑通端到端；JSON/TXT/SRT/MD 显示姓名；失败降级有 warning |
| P3 标注 CLI + 静态页 | `labels init/apply/set`；生成 labels JSON、每个说话人的短片段和 `review.html`；用户在页面试听、填名、勾选 persist，导出 JSON 后由 `labels apply` 完成注册 | 用户无需回听整段音频即可完成标记；HTML 只是本地文件，无服务、无新依赖；重新渲染结果一致 |
| P4 自适应 | anchor + adapted；更新缓冲；review 队列；漂移检测 | 单测验证 anchor 不变、上限替换、漂移报警；auto/review 两种模式 |
| P5 标定与文档 | `calibrate` 命令；README（英/日）新增章节；安全边界说明 | 有阈值输出；文档明确“本地可信环境、未加密、需标定” |

---

## 10. 测试与验收

- **绝不把真实声纹放进仓库**。测试注入 `MockEmbedder`，返回确定性向量。
- 单元测试覆盖：
  - 余弦匹配、accept/reject 灰区、margin（单候选与多候选）、UNKNOWN 输出；
  - registry CRUD、撤销过滤、改名、删除语义；
  - 自适应缓冲累积、anchor 不变、上限替换、漂移标记；
  - `labels init/apply` 的 JSON 读写与幂等性。
- 集成测试：`run()` 在小音频 + mock embedding 下，验证四种输出格式都使用 display name，且 `speaker` 原始字段仍存在。
- 标定脚本：给定合成分数对，能输出可复现的 EER/FAR 与建议阈值。
- 验收指标（需真实数据，由用户后续提供）：top-1 准确率、FAR/FRR、EER、UNKNOWN 率。

---

## 11. 风险与已知限制

- 阈值未标定就启用 `auto` 会导致误认；默认 `review` 且阈值占位值必须打警告。
- 通道/设备差异会掉分；adapted centroid 与漂移检测只能缓解，不能消除。
- 本方案不做加密与权限控制，仅适合本地单用户可信环境；不能用于共享主机、云多租户或受监管场景。
- 会话别名不跨文件；只有 `persist=true` 才写入 registry。
- `TranscriptResult.speakers` 保持原始 `SPEAKER_0i`；`speaker_stats()` 通过 `utt.label` 会显示 display name。需在文档中说明这一区分。
- 带后端的 Web UI 不在本期；标注入口是 labels JSON + 本地静态 HTML 试听页 + CLI，HTML 不承担 embedding/写库。
