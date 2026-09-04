export type FileRole = 'tender' | 'bid' | 'attachment' | 'legal'

/** 由法规文件自动生成的临时审核规则（dict 形态，与引擎消费的规则同形）。 */
/** 确定性可执行条件（白名单形态，与后端 rules_store._normalize_structured 对齐）。 */
export interface StructuredCondition {
  forbid_keywords?: string[]
  require_elements?: string[]
  regex_patterns?: { pattern: string; must_match?: boolean }[]
  amount_thresholds?: { field: string; max: number }[]
  amount_pair_diff?: {
    a_field: string[]
    b_field: string[]
    max_abs_diff: number
    fail_when?: string
  }[]
  consistency_elements?: unknown[]
}

/** 从法规文本头部提取的版本/时效性元数据。 */
export interface LegalSourceMeta {
  file_id: string
  filename: string
  law_name?: string
  doc_number?: string
  latest_date?: string
}

export interface LegalRulesetRule {
  id: string
  name: string
  category?: string
  severity?: string
  description?: string
  checkpoints?: string[]
  legal_basis?: string
  applies_to?: string
  enabled?: boolean
  /** 模型抽取时给出的结构化建议（未激活，需用户确认后才转 deterministic）。 */
  structured_hint?: StructuredCondition | null
  /** 用户确认后的确定性条件：审核时由确定性引擎锁定结论，LLM 不可推翻。 */
  structured?: StructuredCondition | null
  [key: string]: unknown
}

/** 法规临时规则集（独立存储，不污染正式规则库）。 */
export interface LegalRuleset {
  id: string
  name: string
  status: 'pending' | 'mining' | 'ready' | 'failed'
  progress?: number
  progress_message?: string
  version?: number
  owner_id?: string | null
  is_shared?: boolean
  source_files?: { file_id?: string; filename?: string; md5?: string; char_count?: number }[]
  /** 法规文本头部提取的元数据（名称/文号/最近版本日期），用于时效性提示。 */
  source_meta?: LegalSourceMeta[]
  sources_fingerprint?: string
  params?: Record<string, unknown>
  rules?: LegalRulesetRule[]
  /** 列表概要接口返回的规则条数（列表已裁掉 rules 明细以减小负载）。 */
  rule_count?: number
  warnings?: string[]
  /** 解析/抽取过程日志（与审核任务日志同构，供 ProgressPanel 直接渲染）。 */
  logs?: LogEntry[]
  stats?: Record<string, unknown>
  /** 失败原因（仅 status=failed 时有值）。 */
  error?: string | null
  created_at?: string
  updated_at?: string
}

/** 任务中记录的法规临时规则集溯源元数据。 */
export interface LegalRuleMeta {
  id: string
  name?: string
  version?: number
  rule_count?: number
  source_files?: string[]
  sources_fingerprint?: string
  llm_model?: string
  generated_at?: string
}
export type ReviewMode = 'bid' | 'tender' | 'general'
export type Severity = 'critical' | 'major' | 'minor' | 'info'
export type FindingStatus = 'pass' | 'fail' | 'warn' | 'unknown'

export interface UploadedFile {
  file_id: string
  filename: string
  size: number
  ext: string
  role: FileRole
  file_type?: string | null
  char_count: number
  page_count: number
  used_ocr: boolean
  parse_error: string | null
  validation?: FileValidation | null
}

/** 单文件校验结论（格式/大小/内容规则）。 */
export interface FileValidation {
  ok: boolean
  level: 'ok' | 'warn' | 'error'
  messages: string[]
}

/** 可审核的文件类型，关联规则组用于自动匹配。 */
export interface FileType {
  id: string
  name: string
  description: string
  extensions: string[]
  rule_group_ids: string[]
  builtin?: boolean
}

/** 审核规则组：规则的命名组合，可多选套用。 */
export interface RuleGroup {
  id: string
  name: string
  description: string
  rule_ids: string[]
  builtin?: boolean
}

/** 跨文件一致性核查的「核心要素」：规范名 + 同义写法 + 比对要求 */
export interface ConsistencyElement {
  name: string
  synonyms: string[]
  note?: string
  /** 仅要素库接口返回，用于下拉分组 */
  group?: string
}

export interface RuleStructured {
  forbid_keywords?: string[]
  require_elements?: string[]
  regex_patterns?: { pattern: string; must_match: boolean }[]
  amount_thresholds?: { field: string; max: number }[]
  /** 一致性类规则声明的比要素；未声明时引擎按规则文本回查要素库推断 */
  consistency_elements?: ConsistencyElement[]
}

export interface Rule {
  id: string
  name: string
  category: string
  description: string
  checkpoints: string[]
  severity: Severity
  enabled: boolean
  need_legal_basis: boolean
  /** 关联的文档类型；为空表示适用于全部文件，否则仅对匹配类型的文件执行本规则审核 */
  doc_types: string[]
  builtin?: boolean
  structured?: RuleStructured | null
}

/** 一组规则聚合出的一致性核查规格 */
export interface ConsistencyPreview {
  rule_ids: string[]
  rule_names: string[]
  elements: ConsistencyElement[]
  focus_points: string[]
  explicit: boolean
}

export interface RuleSet {
  id: string
  name: string
  description: string
  rules: Rule[]
  builtin?: boolean
}

export interface RuleImportResult {
  ruleset_id: string
  ruleset_name: string
  imported: number
  skipped: number
  errors: string[]
  /** 按「规则集名称」分组建集时的逐集明细（单集导入时仅一条） */
  rulesets?: { ruleset_id: string; ruleset_name: string; imported: number }[]
}

export interface TypoInfo {
  wrong: string
  correct: string
  context?: string
}

export interface Finding {
  rule_id: string
  rule_name: string
  category: string
  severity: Severity
  status: FindingStatus
  title: string
  detail: string
  evidence: string
  location: string
  suggestion: string
  legal_basis: string
  involved_files: string[]
  confidence: number
  typo?: TypoInfo | null
}

export type FeedbackJudgment = 'adopt' | 'reject'

export interface FeedbackRecord {
  id: string
  task_id: string | null
  rule_id: string
  rule_name: string
  category: string
  severity: string
  status: string
  title: string
  detail: string
  evidence: string
  judgment: FeedbackJudgment
  reject_reason: string | null
  is_typo: boolean
  typo_wrong: string | null
  typo_correct: string | null
  created_at: string
  source?: string
  /** 归属用户 id；管理员可见，用于按用户区分反馈与训练数据 */
  user_id?: string | null
}

export interface ValidationItem {
  id: string
  finding_fingerprint: string
  task_id: string | null
  rule_id: string
  typo_wrong: string | null
  typo_correct: string | null
  original_finding: Finding | null
  status: 'pending' | 'accepted' | 'rejected' | 'filtered'
  created_at: string
  updated_at: string
}

export interface ValidationFilter {
  id: string
  typo_wrong: string
  typo_correct: string | null
  reject_reason: string | null
  feedback_id: string
  active: boolean
  hit_count: number
  created_at: string
  updated_at: string
  last_hit_at: string | null
  deactivate_reason: string | null
}

export interface FeedbackStats {
  total: number
  adopt: number
  reject: number
  typo_total: number
  active_filters: number
  validation_items: number
}

// ---------------- 权限管理（用户 / 登录） ----------------
export type UserRole = 'admin' | 'user'

/** 用户状态机：待审核 / 已激活 / 已停用 / 已驳回。 */
export type UserStatus = 'pending' | 'active' | 'disabled' | 'rejected'

/** API Key 权限范围候选。 */
export type ApiKeyScope =
  | 'review'
  | 'kb'
  | 'feedback'
  | 'audit'
  | 'rules'
  | 'settings'

/** 密钥状态：启用 / 禁用 / 已吊销。 */
export type ApiKeyStatus = 'active' | 'disabled' | 'revoked'

/** 脱敏后的 API Key（不含完整密钥明文）。 */
export interface ApiKey {
  key_id: string
  prefix: string
  name: string | null
  status: ApiKeyStatus
  scopes: string[]
  created_at: string | null
  last_used_at: string | null
  created_by: string | null
}

/** 创建密钥时一次性返回的完整密钥。 */
export interface ApiKeyCreated extends ApiKey {
  api_key: string
}

/** 当前登录用户（脱敏；完整密钥仅由密钥创建接口返回）。 */
export interface User {
  id: string
  username: string
  display_name: string
  role: UserRole
  status: UserStatus
  api_key_prefix: string | null
  api_key?: string | null
  is_active: boolean
  created_at: string
  created_by: string
  last_login_at: string | null
  approved_by?: string | null
  approved_at?: string | null
  api_keys?: ApiKey[]
}

export interface RegisterRequest {
  username: string
  password: string
  display_name?: string | null
}

export interface ApiKeyCreate {
  name?: string | null
  scopes?: string[] | null
}

export interface ApiKeyUpdate {
  name?: string | null
  status?: ApiKeyStatus | null
  scopes?: string[] | null
}

export interface LoginRequest {
  username: string
  password: string
}

export interface LoginResponse {
  token: string
  user: User
}

export interface UserCreate {
  username: string
  display_name?: string | null
  role: UserRole
  password?: string | null
}

export interface UserUpdate {
  display_name?: string | null
  is_active?: boolean | null
  password?: string | null
}

export interface ApiKeyRotateResponse {
  user_id: string
  api_key: string
  api_key_prefix: string
}

/** 调用审计记录（按用户区分）。 */
export interface AuditCallRecord {
  id?: string
  req_id?: string
  ts: number
  method: string
  path: string
  status: number
  duration_ms: number
  client: string
  has_error: boolean
  resp_size: number | null
  user_id: string | null
  role: string | null
  api_key_prefix: string | null
}

export interface ConsistencyIssue {
  field: string
  severity: Severity
  description: string
  values: { file: string; location: string; value: string }[]
  suggestion: string
}

export interface KBSource {
  source_file: string | null
  page: number | null
  text: string
  score: number | null
}

// ---------------- 审核数据管理（历史关联记录 / 回流 / 一致性校验） ----------------
export interface ReviewDataDecision {
  id: string
  record_id: string
  rule_id: string
  rule_name: string
  field: string
  status: string
  severity: string
  title: string
  detail: string
  evidence: string
  suggestion: string
  historical_judgment: 'adopt' | 'reject' | null
  reject_reason: string | null
  source: string
  created_at: string
  updated_at: string
}

export interface ReviewDataRecord {
  id: string
  file_group_fp: string
  rule_group_fp: string
  file_md5s: string[]
  file_names: string[]
  rule_ids: string[]
  rule_names: string[]
  task_id: string
  finding_count: number
  adopt_count: number
  reject_count: number
  created_at: string
  updated_at: string
  decisions?: ReviewDataDecision[]
  files?: { md5: string; filename: string; role: string }[]
}

export interface ReviewDataStats {
  records: number
  decisions: number
  adopted: number
  rejected: number
  files: number
  audits: number
}

export interface ReviewDataVerifyResult {
  ok: boolean
  record_id: string
  total: number
  stale: number
  healthy: boolean
  items: {
    decision_id: string
    rule_id: string
    rule_name: string | null
    field: string
    historical_judgment: string | null
    status: 'ok' | 'stale'
    note: string
  }[]
}

// ---------------- 版本清单 / 确定性审计存证 ----------------
export interface VersionManifest {
  engine_version: string
  engine_build_time: string
  rule_set_version: string
  data_snapshot_version: string
  parser_version: string
  parsed_content_hash: string
  environment: { encoding: string; timezone: string; locale: string }
  generated_at: string
  deterministic: boolean
}

export interface AuditRecord {
  id: string
  task_id: string | null
  file_group_fp: string
  rule_group_fp: string
  file_md5s: string[]
  rule_ids: string[]
  rule_set_version: string
  engine_version: string
  data_snapshot_version: string
  parser_version: string
  parsed_content_hash: string
  environment: { encoding: string; timezone: string; locale: string }
  deterministic: boolean
  config_json: Record<string, unknown>
  operator: string
  created_at: string
}

export interface AuditConsistencyResult {
  sampled: number
  inconsistent: number
  inconsistency_rate: number
  items: {
    task_id: string | null
    engine_version: string
    rule_set_version: string
    data_snapshot_version: string
    parsed_content_hash: string
    deterministic: boolean
    historical_fingerprint: string
    replay_ready: boolean
    diff_rate: number
    note: string
  }[]
}

export interface KBTrace {
  query: string
  reason: string
  answer: string
  sources: KBSource[]
}

export interface ReviewSummary {
  score: number
  conclusion: string
  total_rules: number
  status_counts: Record<FindingStatus, number>
  severity_counts: Record<Severity, number>
  consistency_issue_count: number
  critical_items: string[]
}

export type ReviewEvent =
  | { type: 'stage'; stage: string; message: string; progress?: number }
  | { type: 'tender_summary'; data: Record<string, unknown> }
  | { type: 'kb_query'; query: string; reason: string }
  | { type: 'kb_result'; query: string; answer: string; sources: KBSource[] }
  | { type: 'kb_error'; query: string; message: string }
  | { type: 'kb_skip'; message: string }
  | { type: 'web_search'; query: string; reason: string }
  | { type: 'web_search_result'; query: string; result_count: number }
  | { type: 'web_search_error'; query: string; message: string }
  | { type: 'finding'; finding: Finding }
  | { type: 'consistency_issue'; issue: ConsistencyIssue }
  | { type: 'warning'; message: string }
  | { type: 'error'; message: string }
  | {
      type: 'done'
      summary: ReviewSummary
      findings: Finding[]
      consistency_issues: ConsistencyIssue[]
      kb_traces: KBTrace[]
      progress?: number
    }

export interface AppConfig {
  llm_base_url: string
  llm_model: string
  llm_api_key: string
  llm_timeout: number
  llm_temperature: number
  llm_max_tokens: number
  ocr_provider: string
  ocr_base_url: string
  ocr_path: string
  ocr_category: string
  ocr_api_key: string
  ocr_secret_key: string
  ocr_token_path: string
  ocr_timeout: number
  kb_base_url: string
  kb_id: string
  kb_api_key: string
  kb_timeout: number
  kb_enabled: boolean
  kb_max_queries: number
  web_search_enabled: boolean
  web_search_api: 'tavily' | 'bing' | 'serper'
  web_search_api_key: string
  web_search_max_results: number
  web_search_timeout: number
  max_chars_per_doc: number
  concurrency: number
  consistency_enabled: 'auto' | 'on' | 'off'
  findings_cache_enabled: boolean
  consistency_cache_enabled: boolean
  deterministic_mode: boolean
  deterministic_temperature: number
  data_snapshot_version: string
  use_current_time_as_basis: boolean
  /** 法规解析默认抽取的规则条数上限（默认 30，可在服务配置调整） */
  legal_max_rules?: number
}

export interface KnowledgeBase {
  id: string
  name: string
  description: string | null
  document_count: number
}

/** 审核任务状态 */
export type TaskStatus = 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'

/** 单条规则的聚合审核结果（按 rule_id 把分段/多文档并行产生的 N 条结论合并为一条） */
export interface RuleResult {
  rule_id: string
  rule_name: string
  category: string
  severity: string
  /** fail | warn | unknown | pass（取该规则下所有结论的最严重项） */
  status: FindingStatus
  /** 该规则下的问题条数（fail/warn/unknown 计为问题） */
  issue_count: number
  /** 前几条问题标题样例，便于结果卡片快速预览 */
  samples: string[]
}

/** 过程日志条目（与后端任务存储对齐） */
export interface LogEntry {
  time: string
  text: string
  level: 'info' | 'kb' | 'warn' | 'error' | 'ok'
}

/** 历史任务列表项（概览） */
export interface ReviewTaskSummary {
  task_id: string
  status: TaskStatus
  created_at: string | null
  finished_at: string | null
  /** epoch 秒时间戳（后端原始值），前端据此按本地时区格式化与计算时长 */
  created_at_ts?: number | null
  finished_at_ts?: number | null
  /** 归属用户 id；管理员可见，用于按用户区分审核任务 */
  user_id?: string | null
  mode: ReviewMode
  file_names: string[]
  rule_count: number
  progress: number
  progress_message: string
  findings_count: number
  /** 每规则一条的聚合结果（N 条并行结论已合并去重，20 规则 → 20 条） */
  rule_results: RuleResult[]
  score: number | null
  error: string | null
  /** 本次审核引用的法规临时规则集（id 列表，供详情页溯源）。 */
  legal_ruleset_ids?: string[]
  legal_rule_meta?: LegalRuleMeta[]
}

/** 任务详情（含完整结果与日志） */
export interface ReviewTaskDetail extends ReviewTaskSummary {
  request: {
    file_ids: string[]
    mode: ReviewMode
    ruleset_id?: string | null
    rule_ids?: string[] | null
    file_types?: (string | null)[]
    rule_group_ids?: string[]
    auto_match?: boolean
    kb_enabled?: boolean | null
    kb_id?: string | null
    web_search_enabled?: boolean | null
    extra_instruction?: string
    legal_ruleset_ids?: string[] | null
    legal_rules_only?: boolean | null
    /** 串行等待法规解析的规则集 id（解析完成后自动审核的任务才有） */
    waiting_legal_rulesets?: string[] | null
  }
  logs: LogEntry[]
  findings: Finding[]
  consistency_issues: ConsistencyIssue[]
  /** 每规则一条的聚合结果（长文档拆分并行审核后合并去重，每个规则仅一条） */
  rule_results: RuleResult[]
  kb_traces: KBTrace[]
  summary: ReviewSummary | null
  reflow_applied: boolean
}

/** 原文定位命中结果 */
export interface LocateMatch {
  file_id: string
  filename: string
  role: FileRole
  match_type: 'exact' | 'normalized' | 'fuzzy'
  confidence: number
  start: number
  end: number
  location: string
  /** 模型标注的位置（如「第3页」），作为参照展示 */
  model_location?: string
  /** 是否按 model_location 限定到对应页面/表格内检索命中 */
  page_constrained?: boolean
  context_before: string
  matched: string
  context_after: string
  truncated_before: boolean
  truncated_after: boolean
}

/** 原文分页预览（GET /api/files/{id}/preview） */
export interface FilePreview {
  file_id: string
  filename: string
  ext: string | null
  page: number
  page_label: string
  page_count: number
  is_paged: boolean
  page_text: string
  highlights: { start: number; end: number }[]
  match_type?: string
  confidence?: number
  source?: string
}

// --------------------------- Prompt 统一管理 --------------------------- //

/** 提示词模板版本 */
export interface PromptVersion {
  version: number
  content: string
  comment: string
  updated_by: string
  updated_at: string
}

/** 提示词模板（列表项元数据） */
export interface PromptMeta {
  key: string
  name: string
  category: string
  description: string
  current_version: number
  version_count?: number
  updated_at: string
  customized?: boolean
  placeholders?: string[]
}

/** 提示词模板详情（含全部版本） */
export interface PromptDetail extends PromptMeta {
  versions: PromptVersion[]
}

/** 版本对比结果（统一 diff） */
export interface PromptDiffResult {
  v1: number
  v2: number
  added: number
  removed: number
  diff: string[]
}

/** 提示词测试结果 */
export interface PromptTestResult {
  rendered: string
  unresolved_placeholders: string[]
  chars: number
  llm_reply?: string
  llm_error?: string
}

/** 数据存储层状态（GET /api/settings/storage） */
export interface StorageStatus {
  config_path: string
  config_file_exists: boolean
  structured_driver: string
  collections_backend: string
  files_driver: string
  files_base_dir: string
  upload_dir: string
  hot_reload: { enabled: boolean; interval_seconds: number }
  last_reload_at: string | null
  last_error: string | null
  config_masked: Record<string, unknown> | null
}
