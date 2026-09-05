import type {
  AppConfig,
  FeedbackJudgment,
  FeedbackRecord,
  FeedbackStats,
  FileType,
  Finding,
  KnowledgeBase,
  LocateMatch,
  ReviewDataDecision,
  ReviewDataRecord,
  ReviewDataStats,
  ReviewDataVerifyResult,
  AuditRecord,
  AuditConsistencyResult,
  AuditCallRecord,
  ReviewEvent,
  ReviewTaskDetail,
  ReviewTaskSummary,
  RuleGroup,
  RuleSet,
  LegalRuleset,
  LegalRulesetRule,
  LegalSourceMeta,
  ConsistencyElement,
  UploadedFile,
  ValidationFilter,
  ValidationItem,
  RuleImportResult,
  User,
  LoginRequest,
  LoginResponse,
  UserCreate,
  UserUpdate,
  ApiKeyRotateResponse,
  RegisterRequest,
  ApiKey,
  ApiKeyCreated,
  ApiKeyCreate,
  ApiKeyUpdate,
  PromptDetail,
  PromptDiffResult,
  PromptMeta,
  FilePreview,
  PromptTestResult,
  StorageStatus,
} from '../types'

const BASE = '/api'

// --------------------------------------------------------------------------- //
// 鉴权状态：管理员与普通用户均以会话令牌（X-Session-Token）标识；token 含角色。
// 程序化调用可改用用户的某个 API Key（X-API-Key 头）。所有请求据此注入身份头。
// 持久化到 localStorage，刷新页面免重复登录；登录态失效时由 App 清除并重登。
// --------------------------------------------------------------------------- //
type AuthState =
  | { kind: 'admin'; token: string; user: User }
  | { kind: 'user'; token: string; user: User }
  | null

const AUTH_KEY = 'bcr_auth'

let AUTH: AuthState = loadAuth()

function loadAuth(): AuthState {
  try {
    const raw = localStorage.getItem(AUTH_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as AuthState
    if (!parsed || !parsed.token) return null
    return parsed
  } catch {
    return null
  }
}

export function getAuth(): AuthState {
  return AUTH
}

export function setAuth(a: AuthState): void {
  AUTH = a
  if (a) {
    try {
      localStorage.setItem(AUTH_KEY, JSON.stringify(a))
    } catch {
      /* 忽略持久化失败 */
    }
  } else {
    try {
      localStorage.removeItem(AUTH_KEY)
    } catch {
      /* 忽略 */
    }
  }
}

export function clearAuth(): void {
  setAuth(null)
}

function authHeaders(): Record<string, string> {
  if (!AUTH || !AUTH.token) return {}
  return { 'X-Session-Token': AUTH.token }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const baseHeaders = authHeaders()
  const headers: Record<string, string> =
    init?.body instanceof FormData
      ? { ...baseHeaders, ...(init?.headers as Record<string, string> | undefined) }
      : {
          'Content-Type': 'application/json',
          ...baseHeaders,
          ...(init?.headers as Record<string, string> | undefined),
        }
  const resp = await fetch(`${BASE}${path}`, { ...init, headers })
  if (!resp.ok) {
    let message = `请求失败 (${resp.status})`
    try {
      const body = await resp.json()
      const detail = body.detail
      message =
        typeof detail === 'string'
          ? detail
          : Array.isArray(detail?.errors)
            ? detail.errors.map((e: { message: string }) => e.message).join('；')
            : JSON.stringify(detail ?? body)
    } catch {
      /* 保留默认错误信息 */
    }
    // 仅 401 视为登录态失效（无鉴权头/令牌无效），清除后交由 UI 重新登录。
    // 403 为「已登录但权限不足」，不应清空登录态，否则会误把普通用户登出、
    // 并导致后续请求因缺失令牌而连锁 401。
    if (resp.status === 401) {
      clearAuth()
    }
    throw new Error(message)
  }
  return resp.json() as Promise<T>
}

export const api = {
  // ---------------- 鉴权 ----------------
  /** 账号密码登录（管理员或已激活的普通用户）。返回会话令牌。 */
  login(payload: LoginRequest) {
    return request<LoginResponse>('/auth/login', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },
  /** 普通用户自助注册：创建待审核账号。 */
  register(payload: RegisterRequest) {
    return request<User>('/auth/register', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },
  /** 获取当前调用方身份（会话令牌）。 */
  me() {
    return request<User>('/auth/me')
  },
  logout() {
    return request<{ ok: boolean }>('/auth/logout', { method: 'POST' })
  },

  // ---------------- 自助 API Key 管理（调用方自身） ----------------
  listMyKeys() {
    return request<ApiKey[]>('/users/keys')
  },
  createMyKey(payload: ApiKeyCreate) {
    return request<ApiKeyCreated>('/users/keys', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },
  updateMyKey(keyId: string, payload: ApiKeyUpdate) {
    return request<ApiKey>(`/users/keys/${keyId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    })
  },
  revokeMyKey(keyId: string) {
    return request<{ ok: boolean }>(`/users/keys/${keyId}`, { method: 'DELETE' })
  },

  // ---------------- 管理员的用户审批 ----------------
  approveUser(userId: string) {
    return request<User>(`/admin/users/${userId}/approve`, { method: 'POST' })
  },
  rejectUser(userId: string, reason?: string) {
    return request<User>(`/admin/users/${userId}/reject`, {
      method: 'POST',
      body: JSON.stringify({ reason: reason ?? null }),
    })
  },

  // ---------------- 管理员跨用户密钥管理 ----------------
  listUserKeys(userId: string) {
    return request<ApiKey[]>(`/admin/users/${userId}/keys`)
  },
  adminCreateKey(userId: string, payload: ApiKeyCreate) {
    return request<ApiKeyCreated>(`/admin/users/${userId}/keys`, {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },
  adminUpdateKey(userId: string, keyId: string, payload: ApiKeyUpdate) {
    return request<ApiKey>(`/admin/users/${userId}/keys/${keyId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    })
  },
  adminRevokeKey(userId: string, keyId: string) {
    return request<{ ok: boolean }>(`/admin/users/${userId}/keys/${keyId}`, {
      method: 'DELETE',
    })
  },

  // ---------------- 用户管理（管理员） ----------------
  listUsers() {
    return request<User[]>('/admin/users')
  },
  createUser(payload: UserCreate) {
    return request<User>('/admin/users', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },
  updateUser(userId: string, payload: UserUpdate) {
    return request<User>(`/admin/users/${userId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    })
  },
  deleteUser(userId: string) {
    return request<{ deleted: boolean }>(`/admin/users/${userId}`, {
      method: 'DELETE',
    })
  },
  rotateKey(userId: string) {
    return request<ApiKeyRotateResponse>(`/admin/users/${userId}/rotate-key`, {
      method: 'POST',
    })
  },

  // ---------------- 管理员聚合视图（按用户区分） ----------------
  listAdminTasks(user_id?: string) {
    const qs = user_id ? `?user_id=${encodeURIComponent(user_id)}` : ''
    return request<{ tasks: ReviewTaskSummary[]; total: number; limit: number; offset: number }>(
      `/admin/tasks${qs}`,
    )
  },
  listAdminFeedback(params: {
    user_id?: string
    judgment?: FeedbackJudgment
    rule_id?: string
    is_typo?: boolean
    q?: string
    limit?: number
    offset?: number
  } = {}) {
    const qs = new URLSearchParams()
    if (params.user_id) qs.set('user_id', params.user_id)
    if (params.judgment) qs.set('judgment', params.judgment)
    if (params.rule_id) qs.set('rule_id', params.rule_id)
    if (params.is_typo !== undefined) qs.set('is_typo', String(params.is_typo))
    if (params.q) qs.set('q', params.q)
    if (params.limit !== undefined) qs.set('limit', String(params.limit))
    if (params.offset !== undefined) qs.set('offset', String(params.offset))
    const s = qs.toString()
    return request<{ records: FeedbackRecord[]; total: number; limit: number; offset: number }>(
      `/admin/feedback${s ? `?${s}` : ''}`,
    )
  },
  exportAdminFeedback(params: {
    user_id?: string
    judgment?: FeedbackJudgment
    rule_id?: string
    is_typo?: boolean
    q?: string
  } = {}) {
    const qs = new URLSearchParams()
    if (params.user_id) qs.set('user_id', params.user_id)
    if (params.judgment) qs.set('judgment', params.judgment)
    if (params.rule_id) qs.set('rule_id', params.rule_id)
    if (params.is_typo !== undefined) qs.set('is_typo', String(params.is_typo))
    if (params.q) qs.set('q', params.q)
    const s = qs.toString()
    return fetch(`${BASE}/admin/feedback/export${s ? `?${s}` : ''}`, {
      headers: authHeaders(),
    })
  },
  listAuditCalls(params: {
    user_id?: string
    path?: string
    since_hours?: number
    only_error?: boolean
    limit?: number
    offset?: number
  } = {}) {
    const qs = new URLSearchParams()
    if (params.user_id) qs.set('user_id', params.user_id)
    if (params.path) qs.set('path', params.path)
    if (params.since_hours !== undefined) qs.set('since_hours', String(params.since_hours))
    if (params.only_error) qs.set('only_error', 'true')
    if (params.limit !== undefined) qs.set('limit', String(params.limit))
    if (params.offset !== undefined) qs.set('offset', String(params.offset))
    const s = qs.toString()
    return request<{ items: AuditCallRecord[]; stats: Record<string, unknown>; limit: number; offset: number }>(
      `/audit/calls${s ? `?${s}` : ''}`,
    )
  },

  uploadFiles(files: File[], roles: string[], fileTypes?: string[]) {
    const form = new FormData()
    files.forEach((f) => form.append('files', f))
    form.append('roles', roles.join(','))
    if (fileTypes) form.append('file_types', fileTypes.join(','))
    return request<{ files: UploadedFile[]; errors: { filename: string; message: string }[] }>(
      '/files/upload',
      { method: 'POST', body: form },
    )
  },

  listFiles() {
    return request<{ files: UploadedFile[] }>('/files')
  },

  setFileRole(fileId: string, role: string) {
    return request<UploadedFile>(`/files/${fileId}/role`, {
      method: 'PATCH',
      body: JSON.stringify({ role }),
    })
  },

  setFileType(fileId: string, fileType: string | null) {
    return request<UploadedFile>(`/files/${fileId}/type`, {
      method: 'PATCH',
      body: JSON.stringify({ file_type: fileType }),
    })
  },

  // ---------------- 文件类型配置 ----------------
  listFileTypes() {
    return request<{ file_types: FileType[] }>('/file-types')
  },

  saveFileType(payload: Partial<FileType>) {
    return request<FileType>('/file-types', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },

  deleteFileType(id: string) {
    return request<{ deleted: boolean }>(`/file-types/${id}`, { method: 'DELETE' })
  },

  // ---------------- 审核规则组 ----------------
  listRuleGroups() {
    return request<{ rule_groups: RuleGroup[] }>('/rule-groups')
  },

  saveRuleGroup(payload: Partial<RuleGroup>) {
    return request<RuleGroup>('/rule-groups', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },

  deleteRuleGroup(id: string) {
    return request<{ deleted: boolean }>(`/rule-groups/${id}`, { method: 'DELETE' })
  },

  // ---------------- 法规临时规则集（上传法规文件自动生成） ----------------
  previewLegalRules(payload: {
    file_ids: string[]
    mode?: string
    max_rules?: number
    reuse?: boolean
  }) {
    return request<{
      files: { file_id: string; filename?: string; char_count: number; segments: number; chunks: number; split_mode: string }[]
      total_chars: number
      chunks: number
      split_mode: string
      chunk_chars: number
      max_source_chars: number
      truncated: boolean
      concurrency: number
      max_rules: number
      llm_model: string | null
      source_meta: LegalSourceMeta[]
    }>('/legal-rules/preview', { method: 'POST', body: JSON.stringify(payload) })
  },

  generateLegalRules(payload: {
    file_ids: string[]
    name?: string
    description?: string
    mode?: string
    max_rules?: number
    is_shared?: boolean
    reuse?: boolean
  }) {
    return request<LegalRuleset>('/legal-rules/generate', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },

  listLegalRulesets() {
    return request<{ rulesets: LegalRuleset[] }>('/legal-rules')
  },

  getLegalRuleset(id: string) {
    return request<LegalRuleset>(`/legal-rules/${id}`)
  },

  updateLegalRuleset(
    id: string,
    payload: {
      name?: string
      description?: string
      is_shared?: boolean
      rules?: LegalRulesetRule[]
    },
  ) {
    return request<LegalRuleset>(`/legal-rules/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    })
  },

  deleteLegalRuleset(id: string) {
    return request<{ deleted: boolean; id: string }>(`/legal-rules/${id}`, {
      method: 'DELETE',
    })
  },

  promoteLegalRuleset(id: string, name?: string) {
    return request<RuleSet>(`/legal-rules/${id}/promote`, {
      method: 'POST',
      body: JSON.stringify({ name: name ?? null }),
    })
  },

  // ------------------------- Prompt 统一管理（管理员） -------------------------

  listPrompts() {
    return request<{ prompts: PromptMeta[] }>('/prompts')
  },

  getPrompt(key: string) {
    return request<PromptDetail>(`/prompts/${key}`)
  },

  updatePrompt(key: string, content: string, comment: string) {
    return request<PromptDetail>(`/prompts/${key}`, {
      method: 'PUT',
      body: JSON.stringify({ content, comment }),
    })
  },

  rollbackPrompt(key: string, version: number) {
    return request<PromptDetail>(`/prompts/${key}/rollback`, {
      method: 'POST',
      body: JSON.stringify({ version }),
    })
  },

  resetPrompt(key: string) {
    return request<PromptDetail>(`/prompts/${key}/reset`, { method: 'POST' })
  },

  diffPrompt(key: string, v1: number, v2: number) {
    return request<PromptDiffResult>(
      `/prompts/${key}/diff?v1=${v1}&v2=${v2}`,
    )
  },

  testPrompt(key: string, variables: Record<string, string>, sendToLlm: boolean) {
    return request<PromptTestResult>(`/prompts/${key}/test`, {
      method: 'POST',
      body: JSON.stringify({ variables, send_to_llm: sendToLlm }),
    })
  },

  getFileText(fileId: string, limit = 20000) {
    return request<{
      filename: string
      char_count: number
      truncated: boolean
      text: string
    }>(`/files/${fileId}/text?limit=${limit}`)
  },

  deleteFile(fileId: string) {
    return request<{ deleted: boolean }>(`/files/${fileId}`, { method: 'DELETE' })
  },

  listRulesets() {
    return request<{ rulesets: RuleSet[] }>('/rulesets')
  },

  getRulesByMode(mode: 'bid' | 'tender' | 'general') {
    return request<{ mode: string; rules: RuleSet['rules'] }>(`/rulesets/by-mode/${mode}`)
  },

  saveRuleset(payload: Partial<RuleSet>) {
    return request<RuleSet>('/rulesets', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },

  listConsistencyElements() {
    return request<{ elements: ConsistencyElement[]; groups: string[] }>(
      '/rulesets/consistency-elements',
    )
  },

  deleteRuleset(id: string) {
    return request<{ deleted: boolean }>(`/rulesets/${id}`, { method: 'DELETE' })
  },

  importRules(
    file: File,
    opts: { rulesetName?: string; appendTo?: string },
  ) {
    const form = new FormData()
    form.append('file', file)
    if (opts.rulesetName) form.append('ruleset_name', opts.rulesetName)
    if (opts.appendTo) form.append('append_to', opts.appendTo)
    return request<RuleImportResult>('/rulesets/import', {
      method: 'POST',
      body: form,
    })
  },

  /** 导出规则集。format: json（全量保真）/ csv / xlsx（可编辑后回导）。rulesetId 为空则导出当前用户全部可访问规则集。 */
  exportRulesets(rulesetId?: string, format: 'json' | 'csv' | 'xlsx' = 'xlsx') {
    const params = new URLSearchParams()
    if (rulesetId) params.set('ruleset_id', rulesetId)
    params.set('format', format)
    const qs = params.toString() ? `?${params.toString()}` : ''
    return fetch(`${BASE}/rulesets/export${qs}`, {
      headers: authHeaders(),
    }).then(async (resp) => {
      if (!resp.ok) {
        let msg = `导出失败 (${resp.status})`
        try {
          const body = await resp.json()
          if (typeof body.detail === 'string') msg = body.detail
        } catch {
          /* 保留默认错误 */
        }
        throw new Error(msg)
      }
      const blob = await resp.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      const disp = resp.headers.get('Content-Disposition') || ''
      const m = disp.match(/filename\*?=(?:UTF-8'')?["']?([^"';]+)/i)
      a.download = m ? m[1] : `rulesets_export.${format}`
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    })
  },

  downloadRuleTemplate(format: 'csv' | 'xlsx' = 'csv') {
    return fetch(`${BASE}/rulesets/template?format=${format}`, {
      headers: authHeaders(),
    }).then(async (resp) => {
      if (!resp.ok) throw new Error(`模板下载失败 (${resp.status})`)
      const blob = await resp.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = format === 'xlsx' ? 'rules_template.xlsx' : 'rules_template.csv'
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    })
  },

  /** 下载历史任务中的原始上传文件（带鉴权头，按 file_id 取字节）。 */
  downloadFile(fileId: string, filename: string) {
    return fetch(`${BASE}/files/${encodeURIComponent(fileId)}`, {
      headers: authHeaders(),
    }).then(async (resp) => {
      if (!resp.ok) {
        let msg = `文件下载失败 (${resp.status})`
        try {
          const body = await resp.json()
          if (typeof body.detail === 'string') msg = body.detail
        } catch {
          /* 保留默认错误 */
        }
        throw new Error(msg)
      }
      const blob = await resp.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename || 'download'
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    })
  },

  getSettings() {
    return request<{ config: AppConfig; defaults: AppConfig }>('/settings')
  },

  getStorageStatus() {
    return request<StorageStatus>('/settings/storage')
  },

  reloadStorage() {
    return request<StorageStatus & { changed: boolean }>('/settings/storage/reload', {
      method: 'POST',
    })
  },

  updateSettings(patch: Partial<AppConfig>) {
    return request<{ config: AppConfig }>('/settings', {
      method: 'PATCH',
      body: JSON.stringify(patch),
    })
  },

  resetSettings() {
    return request<{ config: AppConfig }>('/settings/reset', { method: 'POST' })
  },

  testConnection(
    target: 'llm' | 'ocr' | 'kb' | 'web_search',
    baseUrl?: string,
    apiKey?: string,
    secretKey?: string,
    provider?: string,
    appId?: string,
  ) {
    return request<{ ok: boolean; message: string; detail?: unknown }>('/settings/test', {
      method: 'POST',
      body: JSON.stringify({
        target,
        base_url: baseUrl,
        api_key: apiKey,
        secret_key: secretKey,
        provider,
        app_id: appId,
      }),
    })
  },

  listKnowledgeBases() {
    return request<{ knowledge_bases: KnowledgeBase[] }>('/settings/knowledge-bases')
  },

  health() {
    return request<{ status: string; services: Record<string, string> }>('/health')
  },

  // ---------------- 后台审核任务 ----------------
  createReviewTask(payload: {
    file_ids: string[]
    mode?: string
    ruleset_id?: string
    rule_ids?: string[]
    file_types?: (string | null)[]
    rule_group_ids?: string[]
    auto_match?: boolean
    kb_enabled?: boolean
    kb_id?: string
    web_search_enabled?: boolean
    extra_instruction?: string
    legal_ruleset_ids?: string[]
    legal_rules_only?: boolean
  }) {
    return request<{ task_id: string; status: string }>('/review/tasks', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },

  listTasks() {
    return request<{ tasks: ReviewTaskSummary[] }>('/review/tasks')
  },

  getTask(taskId: string) {
    return request<ReviewTaskDetail>(`/review/tasks/${taskId}`)
  },

  cancelTask(taskId: string) {
    return request<ReviewTaskDetail>(`/review/tasks/${taskId}/cancel`, {
      method: 'POST',
    })
  },

  deleteTask(taskId: string) {
    return request<{ deleted: boolean }>(`/review/tasks/${taskId}`, {
      method: 'DELETE',
    })
  },

  clearTasks() {
    return request<{ cleared: boolean }>('/review/tasks?confirm=yes', { method: 'DELETE' })
  },

  getFilePreview(
    fileId: string,
    params: { page?: number; start?: number; end?: number; snippet?: string } = {},
  ) {
    const qs = new URLSearchParams()
    if (params.page != null) qs.set('page', String(params.page))
    if (params.start != null) qs.set('start', String(params.start))
    if (params.end != null) qs.set('end', String(params.end))
    if (params.snippet) qs.set('snippet', params.snippet)
    return request<FilePreview>(`/files/${fileId}/preview?${qs.toString()}`)
  },

  locateSnippet(
    snippet: string,
    fileIds?: string[],
    contextChars = 240,
    location?: string,
    candidates?: string[],
  ) {
    return request<{ snippet: string; matches: LocateMatch[] }>('/files/locate', {
      method: 'POST',
      body: JSON.stringify({
        snippet,
        file_ids: fileIds && fileIds.length ? fileIds : null,
        context_chars: contextChars,
        location: location || null,
        candidates: candidates && candidates.length ? candidates : null,
      }),
    })
  },

  exportReport(data: {
    findings: unknown[]
    consistency_issues: unknown[]
    kb_traces: unknown[]
    summary: unknown
    rule_results?: unknown[]
    format: 'word' | 'pdf'
  }) {
    return fetch(`${BASE}/export/report`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify(data),
    })
  },

  // ---------------- 反馈 / 训练数据 / 错别字校验集 ----------------
  submitFeedback(payload: {
    task_id: string | null
    rule_id: string
    finding: Finding
    judgment: FeedbackJudgment
    reject_reason?: string | null
  }) {
    return request<{ record: FeedbackRecord; ok: boolean }>('/feedback', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },

  listFeedback(params: {
    judgment?: FeedbackJudgment
    rule_id?: string
    is_typo?: boolean
    q?: string
    limit?: number
    offset?: number
  } = {}) {
    const qs = new URLSearchParams()
    if (params.judgment) qs.set('judgment', params.judgment)
    if (params.rule_id) qs.set('rule_id', params.rule_id)
    if (params.is_typo !== undefined) qs.set('is_typo', String(params.is_typo))
    if (params.q) qs.set('q', params.q)
    if (params.limit !== undefined) qs.set('limit', String(params.limit))
    if (params.offset !== undefined) qs.set('offset', String(params.offset))
    const s = qs.toString()
    return request<{ records: FeedbackRecord[]; total: number; limit: number; offset: number }>(
      `/feedback${s ? `?${s}` : ''}`,
    )
  },

  feedbackStats() {
    return request<FeedbackStats>('/feedback/stats')
  },

  exportFeedback(params: {
    judgment?: FeedbackJudgment
    rule_id?: string
    is_typo?: boolean
    q?: string
  } = {}) {
    const qs = new URLSearchParams()
    if (params.judgment) qs.set('judgment', params.judgment)
    if (params.rule_id) qs.set('rule_id', params.rule_id)
    if (params.is_typo !== undefined) qs.set('is_typo', String(params.is_typo))
    if (params.q) qs.set('q', params.q)
    const s = qs.toString()
    return fetch(`${BASE}/feedback/export${s ? `?${s}` : ''}`, {
      headers: authHeaders(),
    })
  },

  listValidationItems(params: { status?: string; q?: string; limit?: number; offset?: number } = {}) {
    const qs = new URLSearchParams()
    if (params.status) qs.set('status', params.status)
    if (params.q) qs.set('q', params.q)
    if (params.limit !== undefined) qs.set('limit', String(params.limit))
    if (params.offset !== undefined) qs.set('offset', String(params.offset))
    const s = qs.toString()
    return request<{ items: ValidationItem[]; total: number; limit: number; offset: number }>(
      `/feedback/validation${s ? `?${s}` : ''}`,
    )
  },

  listFilters() {
    return request<{ filters: ValidationFilter[] }>('/feedback/validation/filters')
  },

  deactivateFilter(id: string, deactivate_reason: string) {
    return request<{ filter: ValidationFilter; ok: boolean }>(
      `/feedback/validation/filters/${id}`,
      { method: 'DELETE', body: JSON.stringify({ deactivate_reason }) },
    )
  },

  updateValidationItemStatus(
    id: string,
    status: 'accepted' | 'rejected' | 'pending',
    reject_reason?: string | null,
  ) {
    return request<{ id: string; status: string; ok: boolean }>(
      `/feedback/validation/${id}`,
      {
        method: 'PATCH',
        body: JSON.stringify({ status, reject_reason: reject_reason ?? null }),
      },
    )
  },

  // ---------------- 审核数据管理（历史关联记录 / 回流 / 一致性校验） ----------------
  reviewDataStats() {
    return request<ReviewDataStats>('/reviewdata/stats')
  },

  listReviewData(params: {
    q?: string
    judgment?: 'adopt' | 'reject' | 'mixed'
    limit?: number
    offset?: number
  } = {}) {
    const qs = new URLSearchParams()
    if (params.q) qs.set('q', params.q)
    if (params.judgment) qs.set('judgment', params.judgment)
    if (params.limit !== undefined) qs.set('limit', String(params.limit))
    if (params.offset !== undefined) qs.set('offset', String(params.offset))
    const s = qs.toString()
    return request<{
      records: ReviewDataRecord[]
      total: number
      limit: number
      offset: number
    }>(`/reviewdata/records${s ? `?${s}` : ''}`)
  },

  getReviewData(recordId: string) {
    return request<ReviewDataRecord>(`/reviewdata/records/${recordId}`)
  },

  updateReviewDataDecision(
    recordId: string,
    decisionId: string,
    payload: { judgment?: 'adopt' | 'reject'; reject_reason?: string | null },
  ) {
    return request<{ decision: ReviewDataDecision; ok: boolean }>(
      `/reviewdata/records/${recordId}/decision?decision_id=${decisionId}`,
      { method: 'PATCH', body: JSON.stringify(payload) },
    )
  },

  verifyReviewData(recordId: string) {
    return request<ReviewDataVerifyResult>(`/reviewdata/verify`, {
      method: 'POST',
      body: JSON.stringify({ record_id: recordId }),
    })
  },

  // ---------------- 审计存证与可重放 ----------------
  listAudit(params: { q?: string; limit?: number; offset?: number } = {}) {
    const qs = new URLSearchParams()
    if (params.q) qs.set('q', params.q)
    if (params.limit !== undefined) qs.set('limit', String(params.limit))
    if (params.offset !== undefined) qs.set('offset', String(params.offset))
    const s = qs.toString()
    return request<{
      records: AuditRecord[]
      total: number
      limit: number
      offset: number
    }>(`/audit/records${s ? `?${s}` : ''}`)
  },

  getAudit(taskId: string) {
    return request<AuditRecord>(`/audit/records/${taskId}`)
  },

  auditReplayBaseline(payload: { file_md5s: string[]; rule_ids: string[] }) {
    return request<{ hit: boolean; baseline: AuditRecord | null }>(`/audit/replay-baseline`, {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },

  auditConsistency(sample = 5) {
    return request<AuditConsistencyResult>(`/audit/consistency?sample=${sample}`)
  },
}

/** 审核流：SSE 走 POST，需用 fetch 手动读流。 */
export async function streamReview(
  payload: {
    file_ids: string[]
    mode?: string
    ruleset_id?: string
    rule_ids?: string[]
    file_types?: (string | null)[]
    rule_group_ids?: string[]
    auto_match?: boolean
    kb_enabled?: boolean
    kb_id?: string
    web_search_enabled?: boolean
    extra_instruction?: string
    legal_ruleset_ids?: string[]
    legal_rules_only?: boolean
  },
  onEvent: (event: ReviewEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch(`${BASE}/review/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(payload),
    signal,
  })

    if (!resp.ok) {
      let message = `审核启动失败 (${resp.status})`
      try {
        const body = await resp.json()
        message = typeof body.detail === 'string' ? body.detail : message
      } catch {
        /* 保留默认错误信息 */
      }
      // 仅 401（未登录/令牌失效）清除登录态；403 为权限不足，不应登出，
      // 否则会在审核进行中误把用户踢下线并连锁失败。
      if (resp.status === 401) clearAuth()
      throw new Error(message)
    }
  if (!resp.body) throw new Error('浏览器不支持流式响应')

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    // SSE 以空行分隔事件
    const parts = buffer.split('\n\n')
    buffer = parts.pop() ?? ''
    for (const part of parts) {
      const line = part.split('\n').find((l) => l.startsWith('data:'))
      if (!line) continue
      try {
        onEvent(JSON.parse(line.slice(5).trim()) as ReviewEvent)
      } catch {
        /* 忽略无法解析的心跳或分片 */
      }
    }
  }
}
