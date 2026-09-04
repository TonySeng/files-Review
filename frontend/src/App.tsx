import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  App as AntdApp,
  Button,
  Dropdown,
  Grid,
  Space,
  Spin,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import type { MenuProps } from 'antd'
import {
  AppstoreOutlined,
  AuditOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  DatabaseOutlined,
  FilterOutlined,
  LogoutOutlined,
  MenuOutlined,
  SafetyOutlined,
  SettingOutlined,
  TeamOutlined,
  KeyOutlined,
  TagsOutlined,
  GroupOutlined,
  BookOutlined,
  RobotOutlined,
} from '@ant-design/icons'
import RuleManagementPage from './pages/RuleManagementPage'
import FileTypeConfigPage from './pages/FileTypeConfigPage'
import RuleGroupConfigPage from './pages/RuleGroupConfigPage'
import HistoricalDataPage from './pages/HistoricalDataPage'
import HomePage from './pages/HomePage'
import TaskWizard from './pages/TaskWizard'
import TaskDetailPage from './pages/TaskDetailPage'
import FeedbackPanel from './components/FeedbackPanel'
import type { RuleConfigMode } from './components/RuleSelection'
import SettingsDrawer from './components/SettingsDrawer'
import LoginModal from './components/LoginModal'
import UserManagementPage from './pages/UserManagementPage'
import AdminAuditPage from './pages/AdminAuditPage'
import ApiKeyManagementPage from './pages/ApiKeyManagementPage'
import PromptManagementPage from './pages/PromptManagementPage'
import LegalRulesManagementPage from './pages/LegalRulesManagementPage'
import { useReviewTasks, type StartPayload } from './hooks/useReviewTasks'
import { api, getAuth, clearAuth } from './services/api'
import { deriveReviewMode } from './utils/deriveMode'
import type {
  AppConfig,
  FileType,
  KnowledgeBase,
  RuleGroup,
  RuleSet,
  UploadedFile,
  User,
  UserRole,
} from './types'

const { useBreakpoint } = Grid

type Page = 'home' | 'wizard' | 'task'

export default function App() {
  const { message } = AntdApp.useApp()
  const screens = useBreakpoint()

  const [files, setFiles] = useState<UploadedFile[]>([])
  const [selectedFiles, setSelectedFiles] = useState<string[]>([])
  const [rulesets, setRulesets] = useState<RuleSet[]>([])
  const [activeRulesetId, setActiveRulesetId] = useState('')
  const [selectedRuleIds, setSelectedRuleIds] = useState<string[]>([])
  const [fileTypes, setFileTypes] = useState<FileType[]>([])
  const [ruleGroups, setRuleGroups] = useState<RuleGroup[]>([])
  const [selectedRuleGroupIds, setSelectedRuleGroupIds] = useState<string[]>([])
  const [autoMatch, setAutoMatch] = useState(false)
  // 规则配置策略：按规则组（默认）/ 按规则集 / 按法规文件（自动生成临时规则）。互斥。
  const [ruleConfigMode, setRuleConfigMode] = useState<RuleConfigMode>('group')
  // 法规临时规则集（按法规文件自动生成）：选中的规则集 id 与「是否仅用法规规则」。
  const [selectedLegalRulesetId, setSelectedLegalRulesetId] = useState('')
  const [legalRulesOnly, setLegalRulesOnly] = useState(true)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [config, setConfig] = useState<AppConfig | null>(null)
  const [kbEnabled, setKbEnabled] = useState(true)
  const [kbId, setKbId] = useState('')
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([])
  const [webSearchEnabled, setWebSearchEnabled] = useState(false)
  const [extraInstruction, setExtraInstruction] = useState('')
  // 审核模式不再手动选择：由所选规则集 / 规则组自动推导（见 utils/deriveMode）
  const reviewMode = useMemo(
    () =>
      deriveReviewMode({
        configMode: ruleConfigMode,
        activeRulesetId,
        selectedGroupIds: selectedRuleGroupIds,
        rulesets,
        ruleGroups,
      }),
    [ruleConfigMode, activeRulesetId, selectedRuleGroupIds, rulesets, ruleGroups],
  )
  const [booting, setBooting] = useState(true)
  const [backendStatus, setBackendStatus] = useState<'checking' | 'ok' | 'down'>(
    'checking',
  )
  const [ruleView, setRuleView] = useState<
    'home' | 'rules' | 'filetypes' | 'rulegroups' | 'reviewdata' | 'feedback' | 'usermgmt' | 'audit' | 'prompts' | 'legalmgmt'
  >('home')
  const [page, setPage] = useState<Page>('home')

  // ---------------- 鉴权状态 ----------------
  const [authUser, setAuthUser] = useState<User | null>(null)
  const [authKind, setAuthKind] = useState<UserRole | null>(null)
  const [showLogin, setShowLogin] = useState(false)
  // 刷新时若本地存在会话令牌，先异步校验有效性再决定要不要弹登录框，
  // 避免「令牌其实有效、却先闪一下登录弹窗」的问题。无令牌时初始即为 false，
  // 直接弹登录框、不显示恢复 loading。
  const [restoring, setRestoring] = useState<boolean>(() => getAuth() != null)

  const handleLoginSuccess = useCallback((user: User) => {
    const a = getAuth()
    setAuthUser(user)
    setAuthKind(a?.kind === 'admin' ? 'admin' : 'user')
    // 重置到主页：避免继承上一位登录者停留的视图（例如管理员专属的审计/用户管理页），
    // 否则普通用户会直接渲染管理员专属页并向受保护接口发请求，触发 401/403。
    setRuleView('home')
    setPage('home')
    setShowLogin(false)
  }, [])

  const handleLogout = useCallback(async () => {
    try {
      await api.logout()
    } catch {
      /* 忽略 */
    }
    clearAuth()
    setAuthUser(null)
    setAuthKind(null)
    setRuleView('home')
    setPage('home')
    setShowLogin(true)
  }, [])

  // 初次挂载：优先从持久化会话恢复，并校验有效性
  useEffect(() => {
    const a = getAuth()
    if (!a) {
      setShowLogin(true)
      setRestoring(false)
      return
    }
    api
      .me()
      .then((user) => {
        setAuthUser(user)
        setAuthKind(a.kind === 'admin' ? 'admin' : 'user')
        setRestoring(false)
      })
      .catch(() => {
        clearAuth()
        setShowLogin(true)
        setRestoring(false)
      })
  }, [])

  const isAdmin = authKind === 'admin'

  // 顶部「功能菜单」下拉项：按业务关联性分组，每项统一配 icon，
  // 将多数配置/管理页折叠进下拉，减少顶栏常驻按钮数量。
  const headerMenuItems = useMemo<MenuProps['items']>(() => {
    const items: MenuProps['items'] = [
      {
        type: 'group',
        label: '审核业务',
        children: [
          { key: 'rules', icon: <AppstoreOutlined />, label: '规则管理' },
          { key: 'legalmgmt', icon: <BookOutlined />, label: '法规规则管理' },
          { key: 'reviewdata', icon: <DatabaseOutlined />, label: '历史审核数据' },
          { key: 'feedback', icon: <FilterOutlined />, label: '反馈与训练数据' },
        ],
      },
      {
        type: 'group',
        label: '配置管理',
        children: [
          { key: 'filetypes', icon: <TagsOutlined />, label: '文件类型配置' },
          { key: 'rulegroups', icon: <GroupOutlined />, label: '审核规则组' },
        ],
      },
    ]
    if (isAdmin) {
      items.push({ type: 'divider' })
      items.push({
        type: 'group',
        label: '系统管理',
        children: [
          { key: 'usermgmt', icon: <TeamOutlined />, label: '用户与密钥管理' },
          { key: 'prompts', icon: <RobotOutlined />, label: 'Prompt 管理' },
          { key: 'audit', icon: <SafetyOutlined />, label: '审计日志' },
          { key: 'settings', icon: <SettingOutlined />, label: '服务配置' },
        ],
      })
    }
    return items
  }, [isAdmin])

  const tasks = useReviewTasks()

  // 登录成功后（含刷新恢复会话）主动加载审核任务列表。
  // 否则挂载时的 loadList 无令牌会 401，列表始终为空，用户看不到任何任务/进度。
  // 依赖稳定的 tasks.loadList（useCallback 无依赖），避免每次渲染都重拉列表。
  useEffect(() => {
    if (authUser) void tasks.loadList()
  }, [authUser, tasks.loadList])

  // 加载「三模式内置规则集（投标 / 招标 / 通用）+ 用户自定义规则集」供规则集下拉选择。
  // 后端 list_rulesets 仅暴露单一合并内置集（bid+general），三模式完整规则需经 by-mode 获取。
  const loadRules = useCallback(
    async (keepActiveId?: string) => {
      try {
        const [byBid, byTender, byGeneral, listRes] = await Promise.all([
          api.getRulesByMode('bid'),
          api.getRulesByMode('tender'),
          api.getRulesByMode('general'),
          api.listRulesets(),
        ])
        const modeRulesets: RuleSet[] = [
          {
            id: 'mode-bid',
            name: '投标文档审核规则',
            description: `${byBid.rules.length} 条规则`,
            rules: byBid.rules,
            builtin: true,
          },
          {
            id: 'mode-tender',
            name: '招标文档审核规则',
            description: `${byTender.rules.length} 条规则`,
            rules: byTender.rules,
            builtin: true,
          },
          {
            id: 'mode-general',
            name: '通用文档审核规则',
            description: `${byGeneral.rules.length} 条规则`,
            rules: byGeneral.rules,
            builtin: true,
          },
        ]
        // 排除后端单一合并内置集（已被三套 mode-* 合成集完整覆盖），仅保留自定义规则集
        const custom = (listRes.rulesets || []).filter((rs) => !rs.builtin)
        const all = [...modeRulesets, ...custom]
        setRulesets(all)
        const targetId =
          keepActiveId && all.some((r) => r.id === keepActiveId)
            ? keepActiveId
            : 'mode-bid'
        setActiveRulesetId(targetId)
        const active = all.find((r) => r.id === targetId)
        setSelectedRuleIds(
          active ? active.rules.filter((r) => r.enabled).map((r) => r.id) : [],
        )
      } catch (err) {
        message.error((err as Error).message)
      }
    },
    [message],
  )

  useEffect(() => {
    // 未登录不加载工作台数据
    if (!authUser) return
    let alive = true
    const boot = async () => {
      const [, settingsRes, filesRes, healthRes, ftRes, rgRes] =
        await Promise.allSettled([
          loadRules(),
          api.getSettings(),
          api.listFiles(),
          api.health(),
          api.listFileTypes(),
          api.listRuleGroups(),
        ])
      if (!alive) return
      if (settingsRes.status === 'fulfilled') {
        const cfg = settingsRes.value.config
        setConfig(cfg)
        setKbEnabled(cfg.kb_enabled)
        setKbId(cfg.kb_id || '')
        setWebSearchEnabled(cfg.web_search_enabled)
      }
      if (filesRes.status === 'fulfilled') setFiles(filesRes.value.files)
      if (ftRes.status === 'fulfilled') setFileTypes(ftRes.value.file_types)
      if (rgRes.status === 'fulfilled') setRuleGroups(rgRes.value.rule_groups)
      setBackendStatus(
        healthRes.status === 'fulfilled' && healthRes.value.status === 'ok'
          ? 'ok'
          : 'down',
      )
      setBooting(false)
    }
    boot()
    return () => {
      alive = false
    }
  }, [loadRules, authUser])

  const handleFileTypeChange = useCallback(
    async (fileId: string, fileType: string | null) => {
      try {
        const updated = await api.setFileType(fileId, fileType)
        setFiles((prev) =>
          prev.map((f) => (f.file_id === fileId ? { ...f, ...updated } : f)),
        )
      } catch (err) {
        message.error((err as Error).message)
      }
    },
    [message],
  )

  // 加载可选知识库列表，供「关联检索知识库」下拉使用
  useEffect(() => {
    let alive = true
    api
      .listKnowledgeBases()
      .then(({ knowledge_bases }) => alive && setKnowledgeBases(knowledge_bases))
      .catch(() => alive && setKnowledgeBases([]))
    return () => {
      alive = false
    }
  }, [])

  const handleRulesetChange = (id: string) => {
    setActiveRulesetId(id)
    const rs = rulesets.find((r) => r.id === id)
    setSelectedRuleIds(rs ? rs.rules.filter((r) => r.enabled).map((r) => r.id) : [])
  }

  // 切换规则配置策略：清空另一种策略的选择，避免两套规则并行导致逻辑混乱
  const switchRuleConfigMode = (m: RuleConfigMode) => {
    setRuleConfigMode(m)
    if (m === 'group') {
      setSelectedRuleIds([])
    } else {
      setSelectedRuleGroupIds([])
      handleRulesetChange(activeRulesetId)
    }
  }

  const webSearchConfigured = !!config?.web_search_api_key
  const canStart =
    selectedFiles.length > 0 &&
    (ruleConfigMode === 'legal'
      ? !!selectedLegalRulesetId
      : ruleConfigMode === 'group'
        ? selectedRuleGroupIds.length > 0
        : selectedRuleIds.length > 0)

  // 视图导航：home=历史列表（默认），wizard=向导创建，task=任务详情
  const navigate = useCallback(
    (p: Page, taskId?: string) => {
      // eslint-disable-next-line no-console
      console.log(
        '[navigate]',
        p,
        taskId,
        'from page=',
        page,
        'activeTaskId=',
        tasks.activeTaskId,
        'stack=',
        new Error('navigate-stack').stack,
      )
      if (p === 'home') tasks.deselect()
      else if (taskId) void tasks.select(taskId)
      setPage(p)
    },
    [tasks, page],
  )

  // 从「反馈与训练数据」返回工作台主页
  const goWorkbench = useCallback(() => {
    setRuleView('home')
    navigate('home')
  }, [navigate])

  // 从反馈/校验数据直接跳转到对应的审核任务详情
  const openTaskFromFeedback = useCallback(
    (taskId: string) => {
      setRuleView('home')
      navigate('task', taskId)
    },
    [navigate],
  )

  // 拉取已连接服务的知识库列表（供新建/重跑审核时手动选择）；避免只在启动时拉一次导致服务后连接时列表为空
  const loadKnowledgeBases = useCallback(async () => {
    try {
      const { knowledge_bases } = await api.listKnowledgeBases()
      setKnowledgeBases(knowledge_bases)
      // 默认 kb_id 若已不在已连接列表中则清空，避免提交无效 id
      setKbId((cur) => (cur && !knowledge_bases.some((k) => k.id === cur) ? '' : cur))
    } catch {
      setKnowledgeBases([])
    }
  }, [])

  // 新建审核：清空文件工作区，避免把上一次任务已上传/已选中的文件带过来；并刷新知识库列表
  const startNewReview = useCallback(() => {
    setFiles([])
    setSelectedFiles([])
    setSelectedLegalRulesetId('')
    setLegalRulesOnly(true)
    void loadKnowledgeBases()
    navigate('wizard')
  }, [navigate, loadKnowledgeBases])

  // 创建/重跑任务：有显式 payload 则用（详情页重跑），否则从当前工作台状态组装
  const launchReview = useCallback(
    (payload?: StartPayload) => {
      const p: StartPayload =
        payload ?? (() => {
          const selectedFileObjs = files.filter((f) => selectedFiles.includes(f.file_id))
          const base = {
            file_ids: selectedFiles,
            mode: reviewMode,
            file_types: selectedFileObjs.map((f) => f.file_type ?? null),
            kb_enabled: kbEnabled,
            kb_id: kbId || undefined,
            web_search_enabled: webSearchEnabled,
            extra_instruction: extraInstruction,
          }
          // 按所选策略组装规则参数（三套规则来源互斥，仅其中一种生效）
          if (ruleConfigMode === 'legal') {
            return {
              ...base,
              // 法规临时规则集：纯法规（legal_rules_only=true）或叠加所选规则组
              legal_ruleset_ids: selectedLegalRulesetId ? [selectedLegalRulesetId] : [],
              legal_rules_only: legalRulesOnly,
              ruleset_id: '',
              rule_ids: [],
              rule_group_ids: legalRulesOnly ? [] : selectedRuleGroupIds,
              auto_match: false,
            }
          }
          return ruleConfigMode === 'group'
            ? {
                ...base,
                rule_group_ids: selectedRuleGroupIds,
                auto_match: autoMatch,
                rule_ids: [],
                ruleset_id: activeRulesetId,
              }
            : {
                ...base,
                ruleset_id: activeRulesetId,
                rule_ids: selectedRuleIds,
                rule_group_ids: [],
                auto_match: false,
              }
        })()
      tasks
        .start(p)
        .then((id) => {
          message.success('已提交后台审核任务，可在详情页查看进度')
          navigate('task', id)
        })
        .catch((err) => message.error((err as Error).message))
    },
    [
      files,
      selectedFiles,
      reviewMode,
      activeRulesetId,
      selectedRuleIds,
      selectedRuleGroupIds,
      selectedLegalRulesetId,
      legalRulesOnly,
      autoMatch,
      kbEnabled,
      kbId,
      webSearchEnabled,
      extraInstruction,
      tasks,
      navigate,
      message,
    ],
  )

  return (
    <>
      {authUser && (
        <>
      <div className="app-header">
        <div className="app-title">
          <AuditOutlined style={{ color: '#1668dc', fontSize: 20 }} />
          <span className="app-title-text">文档合规审核工具</span>
        </div>
        <Space size={8} wrap className="app-header-actions">
          {backendStatus === 'ok' ? (
            <Tooltip title="后端服务已连接，可正常发起审核">
              <Tag color="success" icon={<CheckCircleOutlined />} style={{ marginRight: 0 }}>
                后端已连接
              </Tag>
            </Tooltip>
          ) : backendStatus === 'down' ? (
            <Tooltip title="无法连接后端服务，请确认后端已启动（默认 http://localhost:8100）">
              <Tag color="error" icon={<CloseCircleOutlined />} style={{ marginRight: 0 }}>
                后端未连接
              </Tag>
            </Tooltip>
          ) : (
            <Tag color="processing" style={{ marginRight: 0 }}>
              连接中…
            </Tag>
          )}
          {config && screens.sm && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              模型 {config.llm_model} · {new URL(config.llm_base_url).host}
            </Typography.Text>
          )}
          <Dropdown
            menu={{
              items: headerMenuItems,
              onClick: ({ key }) => {
                if (key === 'settings') setSettingsOpen(true)
                else setRuleView(key as typeof ruleView)
              },
              selectedKeys: [ruleView],
            }}
            trigger={['click']}
            placement="bottomRight"
          >
            <Button icon={<MenuOutlined />}>功能菜单</Button>
          </Dropdown>
          {authUser && (
            <Button icon={<LogoutOutlined />} onClick={() => void handleLogout()}>
              退出
            </Button>
          )}
        </Space>
      </div>

      {booting ? (
        <div className="app-boot">
          <Spin size="large" tip="正在加载工作台…" />
        </div>
      ) : ruleView === 'rules' ? (
        <RuleManagementPage
          currentMode={reviewMode}
          initialActiveId={activeRulesetId}
          onBack={() => setRuleView('home')}
          onRulesChanged={() => void loadRules(activeRulesetId)}
        />
      ) : ruleView === 'filetypes' ? (
        <FileTypeConfigPage
          ruleGroups={ruleGroups}
          onBack={() => setRuleView('home')}
          onChanged={() => void api.listFileTypes().then((r) => setFileTypes(r.file_types))}
        />
      ) : ruleView === 'rulegroups' ? (
        <RuleGroupConfigPage
          ruleSets={rulesets}
          onBack={() => setRuleView('home')}
          onChanged={() =>
            void api.listRuleGroups().then((r) => setRuleGroups(r.rule_groups))
          }
        />
      ) : ruleView === 'reviewdata' ? (
        <HistoricalDataPage onBack={() => setRuleView('home')} isAdmin={isAdmin} />
      ) : ruleView === 'feedback' ? (
        <FeedbackPanel onBack={goWorkbench} onOpenTask={openTaskFromFeedback} />
      ) : ruleView === 'usermgmt' && isAdmin ? (
        <div style={{ padding: 16 }}>
          <Tabs
            defaultActiveKey="users"
            items={[
              {
                key: 'users',
                label: (
                  <span>
                    <TeamOutlined /> 用户账号
                  </span>
                ),
                children: <UserManagementPage embedded onBack={() => setRuleView('home')} />,
              },
              {
                key: 'apikeys',
                label: (
                  <span>
                    <KeyOutlined /> API Key 管理
                  </span>
                ),
                children: (
                  <ApiKeyManagementPage
                    currentUser={authUser}
                    isAdmin={isAdmin}
                    embedded
                    onBack={() => setRuleView('home')}
                  />
                ),
              },
            ]}
          />
        </div>
      ) : ruleView === 'prompts' && isAdmin ? (
        <PromptManagementPage onBack={() => setRuleView('home')} />
      ) : ruleView === 'legalmgmt' ? (
        <LegalRulesManagementPage onBack={() => setRuleView('home')} />
      ) : ruleView === 'audit' && isAdmin ? (
        <AdminAuditPage onBack={() => setRuleView('home')} />
      ) : page === 'home' ? (
        <HomePage
          tasks={tasks.tasks}
          onOpen={(id) => navigate('task', id)}
          onNew={() => startNewReview()}
          onCancel={(id) => void tasks.cancel(id)}
          onRemove={(id) => void tasks.remove(id)}
          onClear={() => void tasks.clear()}
        />
      ) : page === 'wizard' ? (
        <TaskWizard
          files={files}
          onFilesChange={setFiles}
          fileTypes={fileTypes}
          onFileTypeChange={handleFileTypeChange}
          selectedFiles={selectedFiles}
          onSelectedFilesChange={setSelectedFiles}
          rulesets={rulesets}
          activeRulesetId={activeRulesetId}
          selectedRuleIds={selectedRuleIds}
          onRulesetChange={handleRulesetChange}
          onSelectedRuleIdsChange={setSelectedRuleIds}
          ruleGroups={ruleGroups}
          selectedRuleGroupIds={selectedRuleGroupIds}
          onSelectedRuleGroupsChange={setSelectedRuleGroupIds}
          autoMatch={autoMatch}
          onAutoMatchChange={setAutoMatch}
          ruleConfigMode={ruleConfigMode}
          onRuleConfigModeChange={switchRuleConfigMode}
          mode={reviewMode}
          selectedLegalRulesetId={selectedLegalRulesetId}
          onSelectedLegalRulesetIdChange={setSelectedLegalRulesetId}
          legalRulesOnly={legalRulesOnly}
          onLegalRulesOnlyChange={setLegalRulesOnly}
          kbEnabled={kbEnabled}
          onKbEnabledChange={setKbEnabled}
          knowledgeBases={knowledgeBases}
          kbId={kbId}
          onKbIdChange={setKbId}
          webSearchEnabled={webSearchEnabled}
          onWebSearchEnabledChange={setWebSearchEnabled}
          webSearchConfigured={webSearchConfigured}
          extraInstruction={extraInstruction}
          onExtraInstructionChange={setExtraInstruction}
          canStart={canStart}
          onOpenManagement={() => setRuleView('rules')}
          onCreate={() => launchReview()}
          onCancel={() => navigate('home')}
        />
      ) : (
        <TaskDetailPage
          task={tasks.activeTask}
          ruleGroups={ruleGroups}
          rulesets={rulesets}
          fileTypes={fileTypes}
          knowledgeBases={knowledgeBases}
          onBack={() => navigate('home')}
          onRerun={(payload) => launchReview(payload)}
          onCancel={(id) => void tasks.cancel(id)}
          onRemove={(id) => {
            void tasks.remove(id)
            // 删除后返回历史任务列表：详情页删除后不再残留死页，列表侧删除维持原视图
            navigate('home')
          }}
          onRefreshKnowledgeBases={loadKnowledgeBases}
        />
      )}

      <SettingsDrawer
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        onSaved={(cfg) => {
          setConfig(cfg)
          setKbEnabled(cfg.kb_enabled)
        }}
        role={authKind ?? undefined}
        currentUser={authUser ?? undefined}
      />
        </>
      )}
      {/* 恢复会话期间不弹登录框，改为居中 loading，避免有效令牌下刷新闪一下登录弹窗 */}
      {restoring && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            background: '#fff',
            zIndex: 1000,
          }}
        >
          <Spin size="large" tip="正在恢复会话…" />
        </div>
      )}
      <LoginModal
        open={showLogin || (!authUser && !restoring)}
        onSuccess={handleLoginSuccess}
      />
    </>
  )
}
