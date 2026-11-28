/* Three-language UI: 中文 / English / 日本語.
 *
 * Design constraints, in order of precedence:
 *
 *  1. No build step. This file is served as-is from `sdts/web/`, so it must be
 *     plain ES2020 that any current browser can run from a static mount.
 *  2. The chosen language survives a page change and a server restart. It goes
 *     to localStorage first and to `<html lang>` for screen readers/CJK fonts.
 *  3. Missing keys fall back to English and then to the key itself, and
 *     `I18N.missing()` lists them - a silently empty label is worse than an
 *     ugly one, and a test asserts the set is empty.
 *
 * Japanese is not machine-shaped from Chinese: the wording differs deliberately
 * (見出し語, 話者, 書き起こし are the terms a Japanese user expects, not
 * translations of the Chinese labels).
 */
export const LANGS = [
  { code: "zh", label: "中文" },
  { code: "en", label: "English" },
  { code: "ja", label: "日本語" },
];

export const STORAGE_KEY = "sdts.lang";
const DEFAULT_LANG = "zh";

const DICT = {
  zh: {
    "app.name": "说话人分离工作台",
    "app.tagline": "本地运行；音频与声纹不离开这台机器",
    "nav.new": "新建任务",
    "nav.jobs": "任务",
    "nav.registry": "声纹库",
    "nav.settings": "设置",
    "nav.docs": "API 文档",
    "common.cancel": "取消",
    "common.close": "关闭",
    "common.save": "保存",
    "common.delete": "删除",
    "common.rename": "改名",
    "common.confirm": "确认",
    "common.loading": "加载中…",
    "common.failed": "出错",
    "common.retry": "重试",
    "common.back": "返回",
    "common.optional": "可选",
    "common.none": "无",
    "common.seconds": "秒",
    "common.minutes": "分钟",
    "common.refresh": "刷新",
    "common.copy": "复制",
    "common.copied": "已复制",

    "index.title": "新建转录任务",
    "index.subtitle": "上传音频或引用服务器上已有的文件，选择配置后开始。",
    "index.source.upload": "上传文件",
    "index.source.path": "服务器路径",
    "index.source.hint": "支持 wav / mp3 / m4a / flac / mp4 / mov / mkv 等容器，由 ffmpeg 解码。",
    "index.drop": "把文件拖到这里，或点击选择",
    "index.path.placeholder": "/data/meetings/2026-11-28.m4a",
    "index.path.submit": "开始转录",
    "index.config": "配置",
    "index.config.default": "使用默认配置",
    "index.config.custom": "常用参数",
    "index.asr.language": "语言",
    "index.asr.language.auto": "自动识别",
    "index.asr.model": "ASR 模型",
    "index.diar.num": "已知说话人数",
    "index.diar.num.hint": "留空则由模型估计；已知人数能显著提升稳定性。",
    "index.ident.enabled": "匹配声纹库",
    "index.ident.hint": "把说话人匹配到声纹库中的姓名；阈值未标定前请逐段试听确认。",
    "index.ident.mode": "匹配方式",
    "index.ident.mode.review": "仅建议（不自动改名）",
    "index.ident.mode.auto": "自动填写姓名",
    "index.submit": "开始转录",
    "index.queue.note": "一次只运行一个任务；后续任务会排队。",
    "index.jobs.title": "最近任务",
    "index.jobs.empty": "还没有任务。",
    "index.jobs.queued": "排队中",
    "index.jobs.running": "运行中",
    "index.jobs.done": "完成",
    "index.jobs.failed": "失败",
    "index.jobs.cancelled": "已取消",
    "index.jobs.columns.file": "文件",
    "index.jobs.columns.status": "状态",
    "index.jobs.columns.progress": "进度",
    "index.jobs.columns.speakers": "说话人",
    "index.jobs.columns.created": "创建时间",
    "index.jobs.open": "打开",
    "index.jobs.delete": "删除记录",
    "index.jobs.delete.confirm": "只从列表移除，音频文件保留。确定？",
    "index.jobs.delete.purge": "同时删除上传的音频",

    "job.title": "转录结果",
    "job.stage.probe": "探测音频",
    "job.stage.chunk": "逐块识别",
    "job.stage.reconcile": "跨块合并说话人",
    "job.stage.fuse": "对齐文本与说话人",
    "job.stage.identify": "声纹匹配",
    "job.stage.done": "完成",
    "job.stage.starting": "启动中",
    "job.stage.failed": "失败",
    "job.stage.cancelled": "已取消",
    "job.stage.queued": "排队中",
    "job.progress.chunk": "第 {done} / {total} 块",
    "job.cancel": "取消任务",
    "job.cancel.note": "正在运行的块结束后才会停止。",
    "job.cancelling": "正在取消…",
    "job.audio": "原始音频",
    "job.filters": "筛选",
    "job.filters.all": "全部",
    "job.filters.search": "搜索文本…",
    "job.speakers.title": "说话人",
    "job.speakers.turns": "{n} 段",
    "job.speakers.name.placeholder": "输入姓名",
    "job.speakers.rename.hint": "改名只作用于本次转录；要长期记住声音请到声纹库注册。",
    "job.speakers.decision.accept": "自动匹配",
    "job.speakers.decision.review": "待确认",
    "job.speakers.decision.unknown": "未知",
    "job.speakers.decision.no_embedding": "无嵌入",
    "job.speakers.suggested": "建议：{name}",
    "job.speakers.apply": "采用",
    "job.speakers.score": "相似度 {score}",
    "job.play": "试听",
    "job.playing": "停止",
    "job.export": "导出",
    "job.export.json": "JSON",
    "job.export.txt": "纯文本",
    "job.export.srt": "字幕 SRT",
    "job.export.md": "Markdown",
    "job.meta.duration": "时长",
    "job.meta.language": "语言",
    "job.meta.speakers": "说话人",
    "job.meta.utterances": "句数",
    "job.meta.rtf": "实时率",
    "job.waiting": "任务完成后这里会显示逐句结果。",
    "job.back": "返回任务列表",

    "registry.title": "本地声纹库",
    "registry.subtitle": "只保存声音的特征向量，不保存音频。仅本机可读。",
    "registry.empty": "库里还没有登记的声音。",
    "registry.enroll": "登记声音",
    "registry.enroll.name": "姓名",
    "registry.enroll.audio": "样本文件路径",
    "registry.enroll.hint": "样本里应当只有一个人说话，长度 3 秒以上。",
    "registry.enroll.submit": "登记",
    "registry.enroll.done": "已登记：{name}",
    "registry.columns.name": "姓名",
    "registry.columns.samples": "质心",
    "registry.columns.consent": "同意范围",
    "registry.columns.created": "登记时间",
    "registry.revoke": "停止匹配",
    "registry.revoke.confirm": "停止匹配后不会再自动识别，记录保留。确定？",
    "registry.delete": "彻底删除",
    "registry.delete.confirm": "这会删除声纹向量，不可恢复。确定？",
    "registry.pending": "待批准",
    "registry.revoked": "已停用",
    "registry.path": "数据库位置",

    "settings.title": "配置",
    "settings.base": "当前使用的配置文件",
    "settings.none": "未指定，使用内置默认值",
    "settings.thresholds": "声纹阈值（未标定前仅供参考）",
    "settings.accept": "接受阈值",
    "settings.reject": "拒绝阈值",
    "settings.margin": "区分余量",
    "settings.save": "写回配置文件",
    "settings.saved": "已写入 {path}",

    "error.network": "无法连接到本地服务，请确认 `sdts serve` 仍在运行。",
    "error.forbidden": "请求被拒绝：只允许来自本机同一来源的写操作。",
    "error.unsupported": "不支持的文件类型。",
  },

  en: {
    "app.name": "Diarization Workbench",
    "app.tagline": "Runs locally; audio and voice prints never leave this machine",
    "nav.new": "New job",
    "nav.jobs": "Jobs",
    "nav.registry": "Voice gallery",
    "nav.settings": "Settings",
    "nav.docs": "API docs",
    "common.cancel": "Cancel",
    "common.close": "Close",
    "common.save": "Save",
    "common.delete": "Delete",
    "common.rename": "Rename",
    "common.confirm": "Confirm",
    "common.loading": "Loading…",
    "common.failed": "Failed",
    "common.retry": "Retry",
    "common.back": "Back",
    "common.optional": "optional",
    "common.none": "none",
    "common.seconds": "s",
    "common.minutes": "min",
    "common.refresh": "Refresh",
    "common.copy": "Copy",
    "common.copied": "Copied",

    "index.title": "New transcription",
    "index.subtitle": "Upload audio or point at a file already on this machine, pick a config, start.",
    "index.source.upload": "Upload a file",
    "index.source.path": "Path on this machine",
    "index.source.hint": "wav / mp3 / m4a / flac / mp4 / mov / mkv and friends; ffmpeg does the decoding.",
    "index.drop": "Drop a file here, or click to choose",
    "index.path.placeholder": "/data/meetings/2026-11-28.m4a",
    "index.path.submit": "Start",
    "index.config": "Config",
    "index.config.default": "Use the default config",
    "index.config.custom": "Common overrides",
    "index.asr.language": "Language",
    "index.asr.language.auto": "Auto-detect",
    "index.asr.model": "ASR model",
    "index.diar.num": "Known speaker count",
    "index.diar.num.hint": "Empty lets the model estimate; a known count is markedly more stable.",
    "index.ident.enabled": "Match against the voice gallery",
    "index.ident.hint": "Matches clusters to names in the voice gallery. Listen before trusting a score until the thresholds are calibrated.",
    "index.ident.mode": "Matching mode",
    "index.ident.mode.review": "Suggest only (do not rename)",
    "index.ident.mode.auto": "Apply names automatically",
    "index.submit": "Start transcription",
    "index.queue.note": "One job runs at a time; the rest queue up.",
    "index.jobs.title": "Recent jobs",
    "index.jobs.empty": "No jobs yet.",
    "index.jobs.queued": "Queued",
    "index.jobs.running": "Running",
    "index.jobs.done": "Done",
    "index.jobs.failed": "Failed",
    "index.jobs.cancelled": "Cancelled",
    "index.jobs.columns.file": "File",
    "index.jobs.columns.status": "Status",
    "index.jobs.columns.progress": "Progress",
    "index.jobs.columns.speakers": "Speakers",
    "index.jobs.columns.created": "Created",
    "index.jobs.open": "Open",
    "index.jobs.delete": "Remove",
    "index.jobs.delete.confirm": "Removes it from the list only; the audio stays. Continue?",
    "index.jobs.delete.purge": "Also delete the uploaded audio",

    "job.title": "Transcript",
    "job.stage.probe": "Probing audio",
    "job.stage.chunk": "Transcribing chunks",
    "job.stage.reconcile": "Reconciling speakers",
    "job.stage.fuse": "Fusing text with speakers",
    "job.stage.identify": "Matching voices",
    "job.stage.done": "Done",
    "job.stage.starting": "Starting",
    "job.stage.failed": "Failed",
    "job.stage.cancelled": "Cancelled",
    "job.stage.queued": "Queued",
    "job.progress.chunk": "chunk {done} / {total}",
    "job.cancel": "Cancel job",
    "job.cancel.note": "Stops after the chunk that is currently running.",
    "job.cancelling": "Cancelling…",
    "job.audio": "Source audio",
    "job.filters": "Filter",
    "job.filters.all": "All",
    "job.filters.search": "Search text…",
    "job.speakers.title": "Speakers",
    "job.speakers.turns": "{n} turns",
    "job.speakers.name.placeholder": "Name this voice",
    "job.speakers.rename.hint": "Renaming applies to this transcript only; enrol the voice to remember it.",
    "job.speakers.decision.accept": "auto-matched",
    "job.speakers.decision.review": "needs review",
    "job.speakers.decision.unknown": "unknown",
    "job.speakers.decision.no_embedding": "no embedding",
    "job.speakers.suggested": "Suggested: {name}",
    "job.speakers.apply": "Apply",
    "job.speakers.score": "score {score}",
    "job.play": "Listen",
    "job.playing": "Stop",
    "job.export": "Export",
    "job.export.json": "JSON",
    "job.export.txt": "Plain text",
    "job.export.srt": "Subtitles (SRT)",
    "job.export.md": "Markdown",
    "job.meta.duration": "Duration",
    "job.meta.language": "Language",
    "job.meta.speakers": "Speakers",
    "job.meta.utterances": "Utterances",
    "job.meta.rtf": "RTF",
    "job.waiting": "Utterances appear here once the job finishes.",
    "job.back": "Back to jobs",

    "registry.title": "Local voice gallery",
    "registry.subtitle": "Stores embedding vectors only, never audio. Readable from this machine only.",
    "registry.empty": "No voices enrolled yet.",
    "registry.enroll": "Enrol a voice",
    "registry.enroll.name": "Name",
    "registry.enroll.audio": "Sample file path",
    "registry.enroll.hint": "The sample should contain one person only, at least 3 seconds.",
    "registry.enroll.submit": "Enrol",
    "registry.enroll.done": "Enrolled: {name}",
    "registry.columns.name": "Name",
    "registry.columns.samples": "Centroids",
    "registry.columns.consent": "Consent",
    "registry.columns.created": "Enrolled",
    "registry.revoke": "Stop matching",
    "registry.revoke.confirm": "The record stays but will no longer match automatically. Continue?",
    "registry.delete": "Delete permanently",
    "registry.delete.confirm": "This removes the voice vectors and cannot be undone. Continue?",
    "registry.pending": "pending",
    "registry.revoked": "revoked",
    "registry.path": "Database",

    "settings.title": "Settings",
    "settings.base": "Config file in use",
    "settings.none": "none specified; built-in defaults",
    "settings.thresholds": "Voice thresholds (placeholders until calibrated)",
    "settings.accept": "Accept threshold",
    "settings.reject": "Reject threshold",
    "settings.margin": "Runner-up margin",
    "settings.save": "Write to the config file",
    "settings.saved": "Written to {path}",

    "error.network": "Cannot reach the local server. Is `sdts serve` still running?",
    "error.forbidden": "Request refused: only same-origin writes are allowed.",
    "error.unsupported": "Unsupported file type.",
  },

  ja: {
    "app.name": "話者分離ワークベンチ",
    "app.tagline": "ローカル実行。音声と声紋はこの端末から出ません",
    "nav.new": "新規ジョブ",
    "nav.jobs": "ジョブ",
    "nav.registry": "声紋ライブラリ",
    "nav.settings": "設定",
    "nav.docs": "API ドキュメント",
    "common.cancel": "キャンセル",
    "common.close": "閉じる",
    "common.save": "保存",
    "common.delete": "削除",
    "common.rename": "名前を変更",
    "common.confirm": "確認",
    "common.loading": "読み込み中…",
    "common.failed": "失敗",
    "common.retry": "再試行",
    "common.back": "戻る",
    "common.optional": "任意",
    "common.none": "なし",
    "common.seconds": "秒",
    "common.minutes": "分",
    "common.refresh": "更新",
    "common.copy": "コピー",
    "common.copied": "コピーしました",

    "index.title": "文字起こしを開始",
    "index.subtitle": "音声をアップロードするか、この端末上のファイルを指定して設定を選びます。",
    "index.source.upload": "ファイルをアップロード",
    "index.source.path": "端末上のパス",
    "index.source.hint": "wav / mp3 / m4a / flac / mp4 / mov / mkv など。デコードは ffmpeg。",
    "index.drop": "ここにファイルをドロップ、またはクリックして選択",
    "index.path.placeholder": "/data/meetings/2026-11-28.m4a",
    "index.path.submit": "開始",
    "index.config": "設定",
    "index.config.default": "既定の設定を使う",
    "index.config.custom": "よく使う上書き項目",
    "index.asr.language": "言語",
    "index.asr.language.auto": "自動判定",
    "index.asr.model": "ASR モデル",
    "index.diar.num": "話者数の指定",
    "index.diar.num.hint": "空欄ならモデルが推定します。人数が分かっていると安定します。",
    "index.ident.enabled": "声紋ライブラリと照合する",
    "index.ident.hint": "クラスタを声紋ライブラリの名前に対応づけます。閾値の校正が済むまでは必ず試聴して確認してください。",
    "index.ident.mode": "照合モード",
    "index.ident.mode.review": "提案のみ（名前は変更しない）",
    "index.ident.mode.auto": "名前を自動入力する",
    "index.submit": "文字起こし開始",
    "index.queue.note": "同時実行は 1 件です。以降は待ち行列に入ります。",
    "index.jobs.title": "最近のジョブ",
    "index.jobs.empty": "ジョブはまだありません。",
    "index.jobs.queued": "待機中",
    "index.jobs.running": "実行中",
    "index.jobs.done": "完了",
    "index.jobs.failed": "失敗",
    "index.jobs.cancelled": "中止",
    "index.jobs.columns.file": "ファイル",
    "index.jobs.columns.status": "状態",
    "index.jobs.columns.progress": "進捗",
    "index.jobs.columns.speakers": "話者",
    "index.jobs.columns.created": "作成",
    "index.jobs.open": "開く",
    "index.jobs.delete": "記録を削除",
    "index.jobs.delete.confirm": "一覧から消えるだけで、音声ファイルは残ります。よろしいですか？",
    "index.jobs.delete.purge": "アップロードした音声も削除する",

    "job.title": "書き起こし結果",
    "job.stage.probe": "音声を解析中",
    "job.stage.chunk": "チャンクごとに認識中",
    "job.stage.reconcile": "話者を統合中",
    "job.stage.fuse": "テキストと話者を対応づけ中",
    "job.stage.identify": "声紋照合中",
    "job.stage.done": "完了",
    "job.stage.starting": "起動中",
    "job.stage.failed": "失敗",
    "job.stage.cancelled": "中止",
    "job.stage.queued": "待機中",
    "job.progress.chunk": "{done} / {total} チャンク",
    "job.cancel": "ジョブを中止",
    "job.cancel.note": "実行中のチャンクが終わってから停止します。",
    "job.cancelling": "中止しています…",
    "job.audio": "元の音声",
    "job.filters": "絞り込み",
    "job.filters.all": "すべて",
    "job.filters.search": "テキストを検索…",
    "job.speakers.title": "話者",
    "job.speakers.turns": "{n} 区間",
    "job.speakers.name.placeholder": "名前を入力",
    "job.speakers.rename.hint": "改名はこの書き起こしだけに適用されます。記憶させるには声紋登録へ。",
    "job.speakers.decision.accept": "自動一致",
    "job.speakers.decision.review": "要確認",
    "job.speakers.decision.unknown": "不明",
    "job.speakers.decision.no_embedding": "埋め込みなし",
    "job.speakers.suggested": "候補：{name}",
    "job.speakers.apply": "採用",
    "job.speakers.score": "類似度 {score}",
    "job.play": "再生",
    "job.playing": "停止",
    "job.export": "書き出し",
    "job.export.json": "JSON",
    "job.export.txt": "テキスト",
    "job.export.srt": "字幕 SRT",
    "job.export.md": "Markdown",
    "job.meta.duration": "長さ",
    "job.meta.language": "言語",
    "job.meta.speakers": "話者",
    "job.meta.utterances": "発話数",
    "job.meta.rtf": "リアルタイム係数",
    "job.waiting": "ジョブが完了すると、ここに発話が並びます。",
    "job.back": "ジョブ一覧へ",

    "registry.title": "ローカル声紋ライブラリ",
    "registry.subtitle": "保存するのは特徴ベクトルのみで音声は保存しません。この端末からのみ参照できます。",
    "registry.empty": "登録された声紋はまだありません。",
    "registry.enroll": "声紋を登録",
    "registry.enroll.name": "名前",
    "registry.enroll.audio": "サンプルのパス",
    "registry.enroll.hint": "1 人だけが話している 3 秒以上のサンプルを指定してください。",
    "registry.enroll.submit": "登録",
    "registry.enroll.done": "登録しました: {name}",
    "registry.columns.name": "名前",
    "registry.columns.samples": "セントロイド",
    "registry.columns.consent": "同意範囲",
    "registry.columns.created": "登録日時",
    "registry.revoke": "照合を停止",
    "registry.revoke.confirm": "記録は残りますが自動照合されなくなります。よろしいですか？",
    "registry.delete": "完全に削除",
    "registry.delete.confirm": "声紋ベクトルを削除します。元に戻せません。よろしいですか？",
    "registry.pending": "承認待ち",
    "registry.revoked": "停止中",
    "registry.path": "データベース",

    "settings.title": "設定",
    "settings.base": "使用中の設定ファイル",
    "settings.none": "指定なし（組み込みの既定値）",
    "settings.thresholds": "声紋の閾値（校正前は暫定値）",
    "settings.accept": "受理閾値",
    "settings.reject": "棄却閾値",
    "settings.margin": "次点との差",
    "settings.save": "設定ファイルに書き戻す",
    "settings.saved": "{path} に書き込みました",

    "error.network": "ローカルサーバーに接続できません。`sdts serve` が動いているか確認してください。",
    "error.forbidden": "拒否されました。同一オリジンからの書き込みのみ許可しています。",
    "error.unsupported": "未対応のファイル形式です。",
  },
};

let current = DEFAULT_LANG;
const missing = new Set();

export function detectLang() {
  const saved = (() => {
    try { return localStorage.getItem(STORAGE_KEY); } catch { return null; }
  })();
  if (saved && DICT[saved]) return saved;
  const nav = (navigator.language || "").toLowerCase();
  if (nav.startsWith("ja")) return "ja";
  if (nav.startsWith("zh")) return "zh";
  if (nav.startsWith("en")) return "en";
  return DEFAULT_LANG;
}

export function getLang() {
  return current;
}

export function setLang(code) {
  if (!DICT[code]) return current;
  current = code;
  try { localStorage.setItem(STORAGE_KEY, code); } catch { /* private mode */ }
  document.documentElement.lang = code === "zh" ? "zh-CN" : code;
  document.documentElement.dataset.lang = code;
  apply();
  return current;
}

/** Translate `key`, substituting `{name}` placeholders from `vars`. */
export function t(key, vars) {
  const table = DICT[current] || {};
  let text = table[key];
  if (text === undefined) {
    text = DICT.en[key];
    if (text !== undefined && current !== "en") missing.add(`${current}:${key}`);
  }
  if (text === undefined) {
    missing.add(`${current}:${key}`);
    return key;
  }
  if (!vars) return text;
  return text.replace(/\{(\w+)\}/g, (whole, name) =>
    Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : whole);
}

export function missingKeys() {
  return [...missing].sort();
}

/** Re-translate everything inside `root`. */
export function apply(root = document) {
  root.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  root.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
    el.setAttribute("placeholder", t(el.dataset.i18nPlaceholder));
  });
  root.querySelectorAll("[data-i18n-title]").forEach((el) => {
    el.setAttribute("title", t(el.dataset.i18nTitle));
  });
  root.querySelectorAll("[data-i18n-aria]").forEach((el) => {
    el.setAttribute("aria-label", t(el.dataset.i18nAria));
  });
  const titleKey = document.body?.dataset.title;
  if (titleKey) document.title = `${t(titleKey)} · ${t("app.name")}`;
}

export function mountSwitcher(host) {
  const box = document.createElement("div");
  box.className = "lang-switch";
  box.setAttribute("role", "group");
  box.setAttribute("aria-label", "Language");
  for (const lang of LANGS) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = lang.label;
    btn.dataset.lang = lang.code;
    btn.className = lang.code === current ? "active" : "";
    btn.addEventListener("click", () => {
      setLang(lang.code);
      box.querySelectorAll("button").forEach((b) =>
        b.classList.toggle("active", b.dataset.lang === lang.code));
      document.dispatchEvent(new CustomEvent("sdts:langchange", { detail: lang.code }));
    });
    box.appendChild(btn);
  }
  host.appendChild(box);
  return box;
}

export function initI18n() {
  setLang(detectLang());
  apply();
  const host = document.querySelector("[data-lang-switch]");
  if (host) mountSwitcher(host);
}
