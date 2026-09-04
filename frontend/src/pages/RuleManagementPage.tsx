import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  App as AntdApp,
  Button,
  Dropdown,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import {
  AppstoreOutlined,
  ArrowLeftOutlined,
  CopyOutlined,
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  ExportOutlined,
  EyeOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  MoreOutlined,
  PlusOutlined,
  SearchOutlined,
  UploadOutlined,
} from '@ant-design/icons'
import { api } from '../services/api'
import type { FileType, ReviewMode, Rule, RuleSet, Severity } from '../types'
import {
  CATEGORY_LABEL,
  SEVERITY_META,
  categoryLabel,
  severityMeta,
} from '../components/ruleMeta'
import RuleEditorModal from '../components/RuleEditorModal'
import RuleImportModal from '../components/RuleImportModal'
import RuleDetailDrawer from '../components/RuleDetailDrawer'

interface Props {
  currentMode: ReviewMode
  initialActiveId?: string
  onBack: () => void
  onRulesChanged: () => void
}

const PRESET_DEF: { id: string; mode: ReviewMode; name: string; desc: string }[] = [
  { id: 'preset-bid', mode: 'bid', name: '投标文档审核规则（预置）', desc: '适用于投标文档的合规性审核' },
  { id: 'preset-tender', mode: 'tender', name: '招标文档审核规则（预置）', desc: '适用于招标文档的质量与合规性审核' },
  { id: 'preset-general', mode: 'general', name: '通用审核规则（预置）', desc: '通用文档质量与规范性审核' },
]

export default function RuleManagementPage({
  currentMode,
  initialActiveId,
  onBack,
  onRulesChanged,
}: Props) {
  const { message, modal } = AntdApp.useApp()
  const [loading, setLoading] = useState(true)
  const [presetSets, setPresetSets] = useState<RuleSet[]>([])
  const [customSets, setCustomSets] = useState<RuleSet[]>([])
  const [activeTab, setActiveTab] = useState<'preset' | 'custom'>('preset')
  const [activeSetId, setActiveSetId] = useState<string>('')

  // 详情抽屉
  const [detailRule, setDetailRule] = useState<Rule | null>(null)
  const [detailOpen, setDetailOpen] = useState(false)

  // 编辑器（仅自定义规则集）
  const [editorOpen, setEditorOpen] = useState(false)
  const [editorRule, setEditorRule] = useState<Rule | null>(null)
  const [editorSet, setEditorSet] = useState<RuleSet | null>(null)

  // 导入
  const [importOpen, setImportOpen] = useState(false)

  // 新建 / 重命名规则集
  const [modalOpen, setModalOpen] = useState(false)
  const [modalMode, setModalMode] = useState<'create' | 'rename'>('create')
  const [modalName, setModalName] = useState('')
  const [modalDesc, setModalDesc] = useState('')
  const [modalTarget, setModalTarget] = useState<RuleSet | null>(null)

  // 筛选
  const [query, setQuery] = useState('')
  const [catFilter, setCatFilter] = useState<string>('all')
  const [sevFilter, setSevFilter] = useState<Severity | 'all'>('all')
  // 文档类型 id→name 映射，用于规则卡片展示「关联文档类型」
  const [fileTypeMap, setFileTypeMap] = useState<Record<string, string>>({})

  useEffect(() => {
    let alive = true
    api
      .listFileTypes()
      .then((r) => {
        if (!alive) return
        const m: Record<string, string> = {}
        for (const ft of (r.file_types || []) as FileType[]) m[ft.id] = ft.name
        setFileTypeMap(m)
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [])

  const loadPreset = useCallback(async () => {
    const results = await Promise.all(
      PRESET_DEF.map(async (def) => {
        const res = await api.getRulesByMode(def.mode)
        const rules: Rule[] = res.rules.map((r) => ({
          ...r,
          enabled: true,
          builtin: true,
        }))
        return {
          id: def.id,
          name: def.name,
          description: def.desc,
          builtin: true,
          rules,
        } as RuleSet
      }),
    )
    setPresetSets(results)
  }, [])

  const loadCustom = useCallback(async () => {
    const res = await api.listRulesets()
    const custom = (res.rulesets || []).filter((rs) => !rs.builtin)
    setCustomSets(custom)
    return custom
  }, [])

  const boot = useCallback(async () => {
    setLoading(true)
    try {
      const [custom] = await Promise.all([loadCustom(), loadPreset()])
      if (initialActiveId && custom.some((rs) => rs.id === initialActiveId)) {
        setActiveTab('custom')
        setActiveSetId(initialActiveId)
      } else {
        const presetId =
          PRESET_DEF.find((p) => p.mode === currentMode)?.id || PRESET_DEF[0].id
        setActiveTab('preset')
        setActiveSetId(presetId)
      }
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setLoading(false)
    }
  }, [loadCustom, loadPreset, initialActiveId, currentMode, message])

  useEffect(() => {
    void boot()
  }, [boot])

  const activeSet = useMemo<RuleSet | undefined>(() => {
    if (activeTab === 'preset') return presetSets.find((s) => s.id === activeSetId)
    return customSets.find((s) => s.id === activeSetId)
  }, [activeTab, presetSets, customSets, activeSetId])

  const isPreset = activeTab === 'preset'
  const rules = activeSet?.rules ?? []

  const sevCounts = useMemo(() => {
    const m: Record<string, number> = {}
    rules.forEach((r) => {
      m[r.severity] = (m[r.severity] || 0) + 1
    })
    return m
  }, [rules])

  const catOptions = useMemo(
    () => [
      { value: 'all', label: '全部类别' },
      ...Object.entries(CATEGORY_LABEL).map(([v, l]) => ({ value: v, label: l })),
    ],
    [],
  )

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return rules.filter((r) => {
      if (catFilter !== 'all' && r.category !== catFilter) return false
      if (sevFilter !== 'all' && r.severity !== sevFilter) return false
      if (
        q &&
        !(
          r.name.toLowerCase().includes(q) ||
          (r.description || '').toLowerCase().includes(q)
        )
      )
        return false
      return true
    })
  }, [rules, catFilter, sevFilter, query])

  const refreshAfterMutation = useCallback(
    async (selectId?: string) => {
      const custom = await loadCustom()
      onRulesChanged()
      if (selectId) setActiveSetId(selectId)
      return custom
    },
    [loadCustom, onRulesChanged],
  )

  const selectSet = (tab: 'preset' | 'custom', id: string) => {
    setActiveTab(tab)
    setActiveSetId(id)
    setQuery('')
    setCatFilter('all')
    setSevFilter('all')
    setDetailOpen(false)
  }

  // ---------- 规则集操作 ----------
  const openCreateSet = () => {
    setModalMode('create')
    setModalName('')
    setModalDesc('')
    setModalTarget(null)
    setModalOpen(true)
  }

  const openRenameSet = (set: RuleSet) => {
    setModalMode('rename')
    setModalName(set.name)
    setModalDesc(set.description || '')
    setModalTarget(set)
    setModalOpen(true)
  }

  const handleSetModalOk = async () => {
    const name = modalName.trim()
    if (!name) {
      message.warning('请输入规则集名称')
      return
    }
    try {
      if (modalMode === 'create') {
        const saved = await api.saveRuleset({
          name,
          description: modalDesc.trim(),
          rules: [],
        })
        await refreshAfterMutation(saved.id)
        setActiveTab('custom')
        setActiveSetId(saved.id)
        message.success('已创建规则集')
      } else if (modalTarget) {
        const saved = await api.saveRuleset({
          id: modalTarget.id,
          name,
          description: modalDesc.trim(),
          rules: modalTarget.rules,
        })
        await refreshAfterMutation(saved.id)
        message.success('规则集已重命名')
      }
      setModalOpen(false)
    } catch (err) {
      message.error((err as Error).message)
    }
  }

  const handleDeleteSet = async (set: RuleSet) => {
    try {
      await api.deleteRuleset(set.id)
      const custom = await loadCustom()
      onRulesChanged()
      if (activeSetId === set.id) {
        if (custom.length) {
          setActiveTab('custom')
          setActiveSetId(custom[0].id)
        } else {
          setActiveTab('preset')
          setActiveSetId(presetSets[0].id)
        }
      }
      message.success('规则集已删除')
    } catch (err) {
      message.error((err as Error).message)
    }
  }

  const handleClonePreset = async () => {
    if (!activeSet) return
    try {
      const saved = await api.saveRuleset({
        name: `${activeSet.name.replace('（预置）', '')}（自定义）`,
        description: activeSet.description,
        rules: activeSet.rules.map((r) => ({ ...r, builtin: false })),
      })
      await refreshAfterMutation(saved.id)
      setActiveTab('custom')
      setActiveSetId(saved.id)
      message.success('已另存为自定义规则集')
    } catch (err) {
      message.error((err as Error).message)
    }
  }

  // ---------- 规则操作 ----------
  const openAddRule = (set: RuleSet) => {
    setEditorSet(set)
    setEditorRule(null)
    setEditorOpen(true)
  }

  const openEditRule = (set: RuleSet, rule: Rule) => {
    setEditorSet(set)
    setEditorRule(rule)
    setEditorOpen(true)
  }

  const handleDeleteRule = async (set: RuleSet, ruleId: string) => {
    try {
      const nextRules = set.rules.filter((r) => r.id !== ruleId)
      const saved = await api.saveRuleset({
        id: set.id,
        name: set.name,
        description: set.description,
        rules: nextRules,
      })
      setCustomSets((prev) => prev.map((rs) => (rs.id === saved.id ? saved : rs)))
      onRulesChanged()
      message.success('规则已删除')
    } catch (err) {
      message.error((err as Error).message)
    }
  }

  const onEditorSaved = (saved: RuleSet) => {
    setEditorOpen(false)
    setCustomSets((prev) => prev.map((rs) => (rs.id === saved.id ? saved : rs)))
    setActiveSetId(saved.id)
    onRulesChanged()
  }

  const onImported = (rulesetId: string) => {
    setImportOpen(false)
    void refreshAfterMutation(rulesetId).then(() => {
      setActiveTab('custom')
      setActiveSetId(rulesetId)
    })
  }

  const handleExport = async (rulesetId: string, format: 'json' | 'csv' | 'xlsx' = 'xlsx') => {
    try {
      await api.exportRulesets(rulesetId, format)
      message.success(`已导出 ${format.toUpperCase()} 文件`)
    } catch (err) {
      message.error((err as Error).message)
    }
  }

  const exportMenu = {
    items: [
      { key: 'xlsx', label: '导出为 Excel (.xlsx)' },
      { key: 'csv', label: '导出为 CSV (.csv)' },
      { key: 'json', label: '导出为 JSON（全量保真）' },
    ],
    onClick: ({ key }: { key: string }) => handleExport(activeSet?.id ?? '', key as 'json' | 'csv' | 'xlsx'),
  }

  const openDetail = (rule: Rule) => {
    setDetailRule(rule)
    setDetailOpen(true)
  }

  const onKeySelect = (fn: () => void) => (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      fn()
    }
  }

  return (
    <div className="rm-page">
      <div className="rm-topbar">
        <button className="rm-back" onClick={onBack} type="button">
          <ArrowLeftOutlined />
          返回工作台
        </button>
        <div className="rm-title-wrap">
          <div className="rm-title">
            <AppstoreOutlined className="rm-title-icon" />
            规则管理
          </div>
          <div className="rm-subtitle">
            集中管理系统预置规则与自定义规则，支持查看完整配置与增删改查
          </div>
        </div>
      </div>

      {loading ? (
        <div className="rm-loading">
          <Spin size="large" tip="加载规则中…" />
        </div>
      ) : (
        <div className="rm-layout">
          {/* 左侧规则集导航 */}
          <aside className="rm-sidebar">
            <div className="rm-side-group">
              <div className="rm-side-group-head">
                <FolderOpenOutlined />
                系统预置规则
              </div>
              {presetSets.map((set) => (
                <div
                  key={set.id}
                  className={`rm-set-item ${
                    activeTab === 'preset' && activeSetId === set.id ? 'is-active' : ''
                  }`}
                  role="button"
                  tabIndex={0}
                  aria-current={activeTab === 'preset' && activeSetId === set.id}
                  onClick={() => selectSet('preset', set.id)}
                  onKeyDown={onKeySelect(() => selectSet('preset', set.id))}
                >
                  <FileTextOutlined className="rm-set-icon" />
                  <span className="rm-set-name">{set.name}</span>
                  <span className="rm-set-count">{set.rules.length}</span>
                </div>
              ))}
            </div>

            <div className="rm-side-group">
              <div className="rm-side-group-head">
                我的规则集
                <Button
                  className="rm-new-btn"
                  type="primary"
                  icon={<PlusOutlined />}
                  onClick={openCreateSet}
                >
                  新建
                </Button>
              </div>
              {customSets.length === 0 ? (
                <div className="rm-side-empty">
                  暂无自定义规则集，点击「新建」开始创建
                </div>
              ) : (
                customSets.map((set) => (
                  <div
                    key={set.id}
                    className={`rm-set-item ${
                      activeTab === 'custom' && activeSetId === set.id ? 'is-active' : ''
                    }`}
                    role="button"
                    tabIndex={0}
                    aria-current={activeTab === 'custom' && activeSetId === set.id}
                    onClick={() => selectSet('custom', set.id)}
                    onKeyDown={onKeySelect(() => selectSet('custom', set.id))}
                  >
                    <FileTextOutlined className="rm-set-icon" />
                    <span className="rm-set-name">{set.name}</span>
                    <span className="rm-set-count">{set.rules.length}</span>
                    <Dropdown
                      trigger={['click']}
                      menu={{
                        items: [
                          { key: 'rename', icon: <EditOutlined />, label: '重命名' },
                          {
                            key: 'delete',
                            icon: <DeleteOutlined />,
                            label: '删除',
                            danger: true,
                          },
                        ],
                        onClick: ({ key, domEvent }) => {
                          domEvent.stopPropagation()
                          if (key === 'rename') openRenameSet(set)
                          if (key === 'delete') {
                            modal.confirm({
                              title: `删除规则集「${set.name}」？`,
                              content: '该规则集及其下所有规则将被永久删除，不可恢复。',
                              okText: '删除',
                              okButtonProps: { danger: true },
                              cancelText: '取消',
                              onOk: () => handleDeleteSet(set),
                            })
                          }
                        },
                      }}
                    >
                      <Button
                        type="text"
                        size="small"
                        className="rm-set-more"
                        icon={<MoreOutlined />}
                        onClick={(e) => e.stopPropagation()}
                      />
                    </Dropdown>
                  </div>
                ))
              )}
            </div>
          </aside>

          {/* 右侧内容区 */}
          <section className="rm-content">
            {!activeSet ? (
              <div className="rm-empty">
                <Empty description="请选择左侧的规则集" />
              </div>
            ) : (
              <>
                {/* 头部卡片 */}
                <div className="rm-headcard">
                  <div className="rm-headcard-top">
                    <div>
                      <div className="rm-headcard-title">
                        <span>{activeSet.name}</span>
                        <Tag color={isPreset ? 'gold' : 'geekblue'}>
                          {isPreset ? '系统预置' : '自定义'}
                        </Tag>
                        <Tag>{activeSet.rules.length} 条规则</Tag>
                      </div>
                      {activeSet.description && (
                        <div className="rm-headcard-desc">{activeSet.description}</div>
                      )}
                      {isPreset && (
                        <div className="rm-headcard-note">
                          系统预置规则为内置标准规则，仅供查看；如需调整，请点击「另存为自定义规则集」。
                        </div>
                      )}
                    </div>
                    <div className="rm-headcard-actions">
                      {isPreset ? (
                        <>
                          <Button icon={<CopyOutlined />} onClick={handleClonePreset}>
                            另存为自定义规则集
                          </Button>
                          <Dropdown menu={exportMenu}>
                            <Button icon={<ExportOutlined />}>导出</Button>
                          </Dropdown>
                        </>
                      ) : (
                        <>
                          <Button
                            type="primary"
                            icon={<PlusOutlined />}
                            onClick={() => openAddRule(activeSet)}
                          >
                            新增规则
                          </Button>
                          <Button
                            icon={<UploadOutlined />}
                            onClick={() => setImportOpen(true)}
                          >
                            批量导入
                          </Button>
                          <Button
                            icon={<DownloadOutlined />}
                            onClick={() =>
                              api
                                .downloadRuleTemplate('csv')
                                .then(() => message.success('已下载 CSV 模板'))
                            }
                          >
                            模板
                          </Button>
                          <Dropdown menu={exportMenu}>
                            <Button icon={<ExportOutlined />}>导出</Button>
                          </Dropdown>
                        </>
                      )}
                    </div>
                  </div>
                </div>

                {/* 筛选条 */}
                <div className="rm-filterbar">
                  <div className="rm-sevbar">
                    <button
                      type="button"
                      className={`rm-chip ${sevFilter === 'all' ? 'is-active' : ''}`}
                      onClick={() => setSevFilter('all')}
                    >
                      全部
                      <span className="rm-chip-num">{rules.length}</span>
                    </button>
                    {Object.entries(SEVERITY_META).map(([key, meta]) => (
                      <button
                        type="button"
                        key={key}
                        className={`rm-chip ${sevFilter === key ? 'is-active' : ''}`}
                        onClick={() => setSevFilter(key as Severity)}
                      >
                        <span className="rm-chip-dot" style={{ background: meta.tone }} />
                        {meta.label}
                        <span className="rm-chip-num">{sevCounts[key] || 0}</span>
                      </button>
                    ))}
                  </div>
                  <div className="rm-filterbar-right">
                    <Input
                      className="rm-search"
                      prefix={<SearchOutlined />}
                      allowClear
                      placeholder="搜索规则名称或说明"
                      value={query}
                      onChange={(e) => setQuery(e.target.value)}
                    />
                    <Select
                      style={{ minWidth: 150 }}
                      value={catFilter}
                      onChange={setCatFilter}
                      options={catOptions}
                    />
                  </div>
                </div>

                {/* 规则网格 */}
                {filtered.length === 0 ? (
                  <div className="rm-empty">
                    {rules.length === 0 ? (
                      <Empty
                        description={
                          isPreset ? '该规则集暂无规则' : '该规则集还没有规则'
                        }
                      >
                        {!isPreset && (
                          <Button
                            type="primary"
                            icon={<PlusOutlined />}
                            onClick={() => openAddRule(activeSet)}
                          >
                            新增第一条规则
                          </Button>
                        )}
                      </Empty>
                    ) : (
                      <Empty description="未找到匹配的规则">
                        <Button onClick={() => selectSet(activeTab, activeSetId)}>
                          清除筛选
                        </Button>
                      </Empty>
                    )}
                  </div>
                ) : (
                  <div className="rm-rule-grid">
                    {filtered.map((rule) => {
                      const meta = severityMeta(rule.severity)
                      return (
                        <div
                          key={rule.id}
                          className="rm-rule-card"
                          style={{ '--rm-accent': meta.tone } as any}
                          role="button"
                          tabIndex={0}
                          aria-label={`查看规则「${rule.name}」详情`}
                          onClick={() => openDetail(rule)}
                          onKeyDown={onKeySelect(() => openDetail(rule))}
                        >
                          <div className="rm-rule-card-top">
                            <div className="rm-rule-card-name">{rule.name}</div>
                            <Tag color={meta.color} style={{ marginInlineEnd: 0 }}>
                              {meta.label}
                            </Tag>
                          </div>
                          <div className="rm-rule-card-tags" style={{ marginTop: -4 }}>
                            <Tag style={{ marginInlineEnd: 0 }}>
                              {categoryLabel(rule.category)}
                            </Tag>
                            {rule.need_legal_basis && (
                              <Tag color="blue" style={{ marginInlineEnd: 0 }}>
                                需法规依据
                              </Tag>
                            )}
                            {rule.doc_types?.length ? (
                              <Tag color="geekblue" style={{ marginInlineEnd: 0 }}>
                                仅限：{rule.doc_types
                                  .map((dt) => fileTypeMap[dt] || dt)
                                  .join('、')}
                              </Tag>
                            ) : null}
                          </div>
                          {rule.description && (
                            <div className="rm-rule-card-desc">{rule.description}</div>
                          )}
                          <div className="rm-rule-card-foot">
                            <span className="rm-rule-card-meta">
                              审核要点 {rule.checkpoints.length} 条
                            </span>
                            {rule.category === 'consistency' &&
                              rule.structured?.consistency_elements?.length ? (
                              <span className="rm-rule-card-meta" style={{ marginLeft: 8 }}>
                                要素 {rule.structured.consistency_elements.length} 项
                              </span>
                            ) : null}
                            <div
                              className="rm-rule-card-actions"
                              onClick={(e) => e.stopPropagation()}
                            >
                              {isPreset ? (
                                <Tooltip title="查看完整配置与说明">
                                  <Button
                                    type="text"
                                    size="small"
                                    icon={<EyeOutlined />}
                                    onClick={() => openDetail(rule)}
                                  />
                                </Tooltip>
                              ) : (
                                <>
                                  <Tooltip title="编辑">
                                    <Button
                                      type="text"
                                      size="small"
                                      icon={<EditOutlined />}
                                      onClick={() => openEditRule(activeSet, rule)}
                                    />
                                  </Tooltip>
                                  <Popconfirm
                                    title="删除该规则？"
                                    okText="删除"
                                    cancelText="取消"
                                    okButtonProps={{ danger: true }}
                                    onConfirm={() => handleDeleteRule(activeSet, rule.id)}
                                  >
                                    <Button
                                      type="text"
                                      size="small"
                                      danger
                                      icon={<DeleteOutlined />}
                                    />
                                  </Popconfirm>
                                </>
                              )}
                            </div>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                )}
              </>
            )}
          </section>
        </div>
      )}

      <RuleDetailDrawer
        open={detailOpen}
        rule={detailRule}
        isPreset={isPreset}
        onClose={() => setDetailOpen(false)}
        onEdit={(r) => {
          setDetailOpen(false)
          if (activeSet) openEditRule(activeSet, r)
        }}
        onClone={() => {
          setDetailOpen(false)
          void handleClonePreset()
        }}
      />

      {editorSet && (
        <RuleEditorModal
          open={editorOpen}
          ruleset={editorSet}
          rule={editorRule}
          onCancel={() => setEditorOpen(false)}
          onSaved={onEditorSaved}
        />
      )}

      <RuleImportModal
        open={importOpen}
        customRulesets={customSets.map((rs) => ({ id: rs.id, name: rs.name }))}
        activeRulesetId={activeTab === 'custom' ? activeSetId : ''}
        onCancel={() => setImportOpen(false)}
        onImported={onImported}
      />

      <Modal
        title={modalMode === 'create' ? '新建规则集' : '重命名规则集'}
        open={modalOpen}
        okText="确定"
        cancelText="取消"
        onOk={handleSetModalOk}
        onCancel={() => setModalOpen(false)}
        destroyOnClose
      >
        <Space direction="vertical" size={12} style={{ width: '100%', marginTop: 8 }}>
          <div>
            <Typography.Text strong>规则集名称</Typography.Text>
            <Input
              style={{ marginTop: 6 }}
              placeholder="例如：某项目专用规则集"
              value={modalName}
              onChange={(e) => setModalName(e.target.value)}
              onPressEnter={handleSetModalOk}
            />
          </div>
          <div>
            <Typography.Text strong>说明（选填）</Typography.Text>
            <Input.TextArea
              style={{ marginTop: 6 }}
              rows={2}
              placeholder="简要描述该规则集的适用范围"
              value={modalDesc}
              onChange={(e) => setModalDesc(e.target.value)}
            />
          </div>
        </Space>
      </Modal>
    </div>
  )
}
