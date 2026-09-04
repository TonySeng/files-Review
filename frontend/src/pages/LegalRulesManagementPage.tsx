import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  App as AntdApp,
  Button,
  Drawer,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
} from 'antd'
import {
  ArrowLeftOutlined,
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  ReadOutlined,
  ReloadOutlined,
  RocketOutlined,
} from '@ant-design/icons'
import { api } from '../services/api'
import type { LegalRuleset, LegalRulesetRule, LegalSourceMeta } from '../types'

interface Props {
  onBack: () => void
}

const STATUS_META: Record<string, { color: string; text: string }> = {
  pending: { color: 'default', text: '待生成' },
  mining: { color: 'processing', text: '生成中' },
  ready: { color: 'green', text: '已就绪' },
  failed: { color: 'red', text: '失败' },
}

const SEVERITY_META: Record<string, { color: string; text: string }> = {
  critical: { color: 'red', text: '严重' },
  major: { color: 'orange', text: '重要' },
  minor: { color: 'gold', text: '一般' },
  info: { color: 'default', text: '提示' },
}

const CATEGORY_OPTIONS = [
  { value: 'qualification', label: '资格资质' },
  { value: 'commercial', label: '商务报价与保证金' },
  { value: 'technical', label: '技术规格' },
  { value: 'format', label: '形式与签章' },
  { value: 'legal', label: '法律合规' },
  { value: 'tender_quality', label: '招标文件质量' },
  { value: 'general_quality', label: '通用质量' },
]

const SEVERITY_OPTIONS = Object.entries(SEVERITY_META).map(([value, m]) => ({
  value,
  label: m.text,
}))

const APPLIES_OPTIONS = [
  { value: 'bid', label: '投标文件' },
  { value: 'tender', label: '招标文件' },
  { value: 'both', label: '两者通用' },
]

function formatTime(iso?: string): string {
  if (!iso) return '-'
  try {
    return new Date(iso).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

export default function LegalRulesManagementPage({ onBack }: Props) {
  const { message } = AntdApp.useApp()
  const [list, setList] = useState<LegalRuleset[]>([])
  const [loading, setLoading] = useState(false)
  const [statusFilter, setStatusFilter] = useState<string>('all')
  const [keyword, setKeyword] = useState('')
  const [detail, setDetail] = useState<LegalRuleset | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [promoteTarget, setPromoteTarget] = useState<LegalRuleset | null>(null)

  const loadList = useCallback(async () => {
    setLoading(true)
    try {
      const res = await api.listLegalRulesets()
      setList(res.rulesets)
    } catch (e) {
      message.error((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [message])

  useEffect(() => {
    void loadList()
  }, [loadList])

  const openDetail = useCallback(
    async (id: string) => {
      setDetailLoading(true)
      try {
        const d = await api.getLegalRuleset(id)
        setDetail(d)
      } catch (e) {
        message.error((e as Error).message)
      } finally {
        setDetailLoading(false)
      }
    },
    [message],
  )

  const remove = async (r: LegalRuleset) => {
    try {
      await api.deleteLegalRuleset(r.id)
      message.success('已删除')
      if (detail?.id === r.id) setDetail(null)
      void loadList()
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const filtered = useMemo(
    () =>
      list.filter((r) => {
        if (statusFilter !== 'all' && r.status !== statusFilter) return false
        if (keyword.trim()) {
          const kw = keyword.trim().toLowerCase()
          const hay = `${r.name} ${(r.source_meta ?? [])
            .map((m) => m.law_name ?? m.filename ?? '')
            .join(' ')}`.toLowerCase()
          if (!hay.includes(kw)) return false
        }
        return true
      }),
    [list, statusFilter, keyword],
  )

  const columns = [
    {
      title: '规则集',
      key: 'name',
      render: (_: unknown, r: LegalRuleset) => (
        <div>
          <Typography.Text strong>{r.name}</Typography.Text>
          {r.is_shared && (
            <Tag color="purple" style={{ marginLeft: 6 }}>
              共享
            </Tag>
          )}
        </div>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 90,
      render: (s: string) => <Tag color={STATUS_META[s]?.color}>{STATUS_META[s]?.text ?? s}</Tag>,
    },
    {
      title: '适用模式',
      key: 'mode',
      width: 100,
      render: (_: unknown, r: LegalRuleset) => {
        const mode = String((r.params as { mode?: string } | undefined)?.mode ?? '-')
        return (
          <Tag>{mode === 'bid' ? '投标审核' : mode === 'tender' ? '招标审核' : mode === 'general' ? '通用' : mode}</Tag>
        )
      },
    },
    {
      title: '规则数',
      key: 'rule_count',
      width: 80,
      render: (_: unknown, r: LegalRuleset) =>
        r.status === 'ready' ? <Typography.Text strong>{r.rule_count ?? r.rules?.length ?? 0}</Typography.Text> : '-',
    },
    {
      title: '来源法规',
      key: 'sources',
      ellipsis: true,
      render: (_: unknown, r: LegalRuleset) => {
        const names = (r.source_meta ?? []).map((m) => m.law_name || m.filename).filter(Boolean)
        return names.length ? (
          <Typography.Text type="secondary" ellipsis={{ tooltip: names.join('；') }}>
            {names.join('；')}
          </Typography.Text>
        ) : (
          <Typography.Text type="secondary">{(r.source_files ?? []).map((f) => f.filename).join('；') || '-'}</Typography.Text>
        )
      },
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 160,
      render: (t: string) => <Typography.Text type="secondary">{formatTime(t)}</Typography.Text>,
    },
    {
      title: '操作',
      key: 'action',
      width: 240,
      render: (_: unknown, r: LegalRuleset) => (
        <Space size={0}>
          <Button size="small" type="link" onClick={() => void openDetail(r.id)}>
            查看
          </Button>
          {r.status === 'ready' && (
            <Button
              size="small"
              type="link"
              icon={<RocketOutlined />}
              onClick={() => setPromoteTarget(r)}
            >
              转正
            </Button>
          )}
          <Popconfirm
            title="删除该规则集？"
            description="已用于审核任务的历史记录不受影响（任务存有快照）。"
            onConfirm={() => void remove(r)}
          >
            <Button size="small" type="link" danger icon={<DeleteOutlined />}>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <div className="rm-page">
      <div className="rm-topbar">
        <button className="rm-back" type="button" onClick={onBack}>
          <ArrowLeftOutlined />
          返回工作台
        </button>
        <div className="rm-title-wrap">
          <div className="rm-title">
            <ReadOutlined className="rm-title-icon" />
            法规规则管理
          </div>
          <div className="rm-subtitle">
            上传的法律法规文件与解析出的审核规则的统一管理：关联查看、编辑、启停、转正与删除
          </div>
        </div>
        <Space wrap style={{ marginLeft: 'auto' }}>
          <Input.Search
            placeholder="搜索名称 / 法规名"
            allowClear
            style={{ width: 220 }}
            onSearch={setKeyword}
            onChange={(e) => !e.target.value && setKeyword('')}
          />
          <Segmented
            value={statusFilter}
            onChange={(v) => setStatusFilter(v as string)}
            options={[
              { value: 'all', label: '全部' },
              { value: 'ready', label: '已就绪' },
              { value: 'mining', label: '生成中' },
              { value: 'failed', label: '失败' },
            ]}
          />
          <Button icon={<ReloadOutlined />} onClick={() => void loadList()} loading={loading}>
            刷新
          </Button>
        </Space>
      </div>

      <Table
        rowKey="id"
        loading={loading}
        dataSource={filtered}
        columns={columns as never}
        pagination={{ pageSize: 10, showTotal: (t) => `共 ${t} 条` }}
        size="middle"
      />

      <RulesetDetailDrawer
        detail={detail}
        open={!!detail}
        loading={detailLoading}
        onClose={() => setDetail(null)}
        onChanged={() => {
          void loadList()
          if (detail) void openDetail(detail.id)
        }}
        onPromote={(r) => setPromoteTarget(r)}
      />

      <PromoteModal
        target={promoteTarget}
        onClose={() => setPromoteTarget(null)}
        onDone={() => {
          setPromoteTarget(null)
          message.success('已转正为正式规则集，可在规则管理中查看')
          void loadList()
        }}
      />
    </div>
  )
}

// --------------------------------------------------------------------------- //
// 详情抽屉：规则集元信息 + 规则列表（启停/编辑/增删）+ 过程日志
// --------------------------------------------------------------------------- //

function RulesetDetailDrawer({
  detail,
  open,
  loading,
  onClose,
  onChanged,
  onPromote,
}: {
  detail: LegalRuleset | null
  open: boolean
  loading: boolean
  onClose: () => void
  onChanged: () => void
  onPromote: (r: LegalRuleset) => void
}) {
  const { message } = AntdApp.useApp()
  const [rules, setRules] = useState<LegalRulesetRule[]>([])
  const [editing, setEditing] = useState<LegalRulesetRule | null>(null)
  const [isNew, setIsNew] = useState(false)
  const [saving, setSaving] = useState(false)
  const [sourceFilter, setSourceFilter] = useState<string>('all')
  const [editForm] = Form.useForm<LegalRulesetRule>()

  useEffect(() => {
    setRules(detail?.rules ? JSON.parse(JSON.stringify(detail.rules)) : [])
    setSourceFilter('all')
  }, [detail])

  if (!detail) return null

  const dirty =
    JSON.stringify(rules) !== JSON.stringify(detail.rules ?? []) && detail.status === 'ready'

  const persistRules = async (next: LegalRulesetRule[]) => {
    setSaving(true)
    try {
      const updated = await api.updateLegalRuleset(detail.id, { rules: next })
      message.success('规则修改已保存')
      onChanged()
      setRules(updated.rules ?? next)
    } catch (e) {
      message.error((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const toggleRule = (id: string, enabled: boolean) => {
    const next = rules.map((r) => (r.id === id ? { ...r, enabled } : r))
    setRules(next)
    void persistRules(next)
  }

  const deleteRule = (id: string) => {
    const next = rules.filter((r) => r.id !== id)
    setRules(next)
    void persistRules(next)
  }

  const openEdit = (rule: LegalRulesetRule | null) => {
    setIsNew(!rule)
    setEditing(rule)
    editForm.setFieldsValue(
      (rule ?? {
        name: '',
        category: 'legal',
        severity: 'major',
        description: '',
        checkpoints: [],
        legal_basis: '',
        applies_to: 'both',
        enabled: true,
      }) as never,
    )
  }

  const submitEdit = async () => {
    const v = await editForm.validateFields()
    const checkpoints = String(v.checkpointsText ?? '')
      .split('\n')
      .map((s) => s.trim())
      .filter(Boolean)
    const saved: LegalRulesetRule = {
      ...(editing ?? {}),
      ...v,
      checkpointsText: undefined,
      checkpoints,
    } as LegalRulesetRule
    let next: LegalRulesetRule[]
    if (isNew) {
      const maxSeq = rules.reduce((m, r) => {
        const n = parseInt(r.id.split('-').pop() ?? '0', 10)
        return Number.isFinite(n) ? Math.max(m, n) : m
      }, 0)
      next = [...rules, { ...saved, id: `${detail.id}-u${maxSeq + 1}`.replace('lrs', 'lr') }]
    } else {
      next = rules.map((r) => (r.id === saved.id ? saved : r))
    }
    setRules(next)
    setEditing(null)
    await persistRules(next)
  }

  const sourceOptions = [
    { value: 'all', label: '全部来源' },
    ...(detail.source_meta ?? []).map((m) => ({
      value: m.file_id,
      label: m.law_name || m.filename || m.file_id,
    })),
  ]

  const shownRules =
    sourceFilter === 'all' ? rules : rules.filter((r) => r.source_file === sourceFilter)

  const metaTab = (
    <div>
      <div className="legal-mgmt-meta">
        <Space wrap size={[16, 8]}>
          <span>
            状态：<Tag color={STATUS_META[detail.status]?.color}>{STATUS_META[detail.status]?.text}</Tag>
          </span>
          <span>规则数：<Typography.Text strong>{rules.length}</Typography.Text></span>
          <span>启用：<Typography.Text strong>{rules.filter((r) => r.enabled !== false).length}</Typography.Text></span>
          <span>创建：{formatTime(detail.created_at)}</span>
          <span>更新：{formatTime(detail.updated_at)}</span>
        </Space>
        {detail.status === 'failed' && detail.error && (
          <Typography.Paragraph type="danger" style={{ marginTop: 8, marginBottom: 0 }}>
            失败原因：{detail.error}
          </Typography.Paragraph>
        )}
        {(detail.warnings ?? []).length > 0 && (
          <Typography.Paragraph type="warning" style={{ marginTop: 8, marginBottom: 0 }}>
            {detail.warnings!.join('；')}
          </Typography.Paragraph>
        )}
      </div>
      <Typography.Title level={5}>关联法规文件</Typography.Title>
      <Table
        rowKey={(r) => r.file_id ?? r.filename ?? Math.random().toString(36)}
        dataSource={(detail.source_meta ?? []).map((m) => ({ ...m }))}
        size="small"
        pagination={false}
        columns={[
          { title: '法规名称', key: 'law', render: (_: unknown, m: LegalSourceMeta) => m.law_name || m.filename || '-' },
          {
            title: '文号',
            dataIndex: 'doc_number',
            key: 'doc_number',
            render: (v: string) => v || '-',
          },
          {
            title: '最近版本日期',
            dataIndex: 'latest_date',
            key: 'latest_date',
            width: 120,
            render: (v: string) => v || '-',
          },
          {
            title: '来源规则数',
            key: 'rule_count',
            width: 110,
            render: (_: unknown, m: LegalSourceMeta) => rules.filter((r) => r.source_file === m.file_id).length,
          },
        ] as never}
        locale={{ emptyText: <Empty description="未记录来源元数据" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
      />
      {detail.status === 'ready' && (
        <Button
          style={{ marginTop: 12 }}
          icon={<RocketOutlined />}
          onClick={() => onPromote(detail)}
        >
          转正为正式规则集
        </Button>
      )}
    </div>
  )

  const rulesTab = (
    <div>
      <Space style={{ marginBottom: 12 }} wrap>
        <Select
          value={sourceFilter}
          onChange={setSourceFilter}
          options={sourceOptions}
          style={{ minWidth: 200 }}
          disabled={(detail.source_meta ?? []).length === 0}
        />
        <Button size="small" icon={<PlusOutlined />} onClick={() => openEdit(null)} disabled={detail.status !== 'ready'}>
          新增规则
        </Button>
        {dirty && (
          <Typography.Text type="warning">有未保存的修改</Typography.Text>
        )}
      </Space>
      <Table
        rowKey="id"
        dataSource={shownRules}
        size="small"
        pagination={{ pageSize: 10, showTotal: (t) => `共 ${t} 条` }}
        columns={[
          {
            title: '启用',
            key: 'enabled',
            width: 70,
            render: (_: unknown, r: LegalRulesetRule) => (
              <Switch
                size="small"
                checked={r.enabled !== false}
                onChange={(v) => toggleRule(r.id, v)}
                disabled={detail.status !== 'ready'}
              />
            ),
          },
          {
            title: '规则',
            key: 'name',
            render: (_: unknown, r: LegalRulesetRule) => (
              <div>
                <Space size={4} wrap>
                  <Typography.Text strong>{r.name}</Typography.Text>
                  <Tag color={SEVERITY_META[r.severity ?? 'major']?.color}>
                    {SEVERITY_META[r.severity ?? 'major']?.text}
                  </Tag>
                  <Tag>{(CATEGORY_OPTIONS.find((c) => c.value === r.category) ?? { label: r.category }).label}</Tag>
                </Space>
                {r.legal_basis && (
                  <div>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      依据：{r.legal_basis}
                    </Typography.Text>
                  </div>
                )}
              </div>
            ),
          },
          {
            title: '说明',
            dataIndex: 'description',
            key: 'description',
            ellipsis: true,
          },
          {
            title: '操作',
            key: 'action',
            width: 140,
            render: (_: unknown, r: LegalRulesetRule) => (
              <Space size={0}>
                <Button
                  size="small"
                  type="link"
                  icon={<EditOutlined />}
                  disabled={detail.status !== 'ready'}
                  onClick={() => openEdit(r)}
                >
                  编辑
                </Button>
                <Popconfirm title="删除该规则？" onConfirm={() => deleteRule(r.id)}>
                  <Button size="small" type="link" danger icon={<DeleteOutlined />} disabled={detail.status !== 'ready'} />
                </Popconfirm>
              </Space>
            ),
          },
        ] as never}
      />
    </div>
  )

  const logsTab = (
    <pre className="prompt-render-view" style={{ maxHeight: 480 }}>
      {(detail.logs ?? []).length === 0
        ? '暂无过程日志（该规则集生成于日志功能上线之前）'
        : (detail.logs ?? []).map((l) => `[${formatTime(l.time)}] ${l.text}`).join('\n')}
    </pre>
  )

  return (
    <Drawer
      title={
        <Space>
          <span>{detail.name}</span>
          <Tag color={STATUS_META[detail.status]?.color}>{STATUS_META[detail.status]?.text}</Tag>
        </Space>
      }
      open={open}
      loading={loading}
      onClose={onClose}
      width={860}
      destroyOnClose
    >
      <Tabs
        defaultActiveKey="meta"
        items={[
          { key: 'meta', label: '概览', children: metaTab },
          { key: 'rules', label: `规则列表（${rules.length}）`, children: rulesTab },
          { key: 'logs', label: '生成过程', children: logsTab },
        ]}
      />

      <Modal
        title={isNew ? '新增规则' : `编辑规则：${editing?.name ?? ''}`}
        open={!!editing}
        onCancel={() => setEditing(null)}
        onOk={() => void submitEdit()}
        okText="保存"
        okButtonProps={{ loading: saving }}
        width={620}
        destroyOnClose
      >
        <Form form={editForm} layout="vertical">
          <Form.Item name="name" label="规则名" rules={[{ required: true, message: '请输入规则名' }]}>
            <Input maxLength={60} placeholder="动宾结构，体现可核查点" />
          </Form.Item>
          <Space size={12} style={{ display: 'flex' }}>
            <Form.Item name="category" label="类别" style={{ width: 180 }}>
              <Select options={CATEGORY_OPTIONS} />
            </Form.Item>
            <Form.Item name="severity" label="严重级别" style={{ width: 120 }}>
              <Select options={SEVERITY_OPTIONS} />
            </Form.Item>
            <Form.Item name="applies_to" label="适用对象" style={{ width: 140 }}>
              <Select options={APPLIES_OPTIONS} />
            </Form.Item>
          </Space>
          <Form.Item name="description" label="规则说明">
            <Input.TextArea rows={3} maxLength={500} />
          </Form.Item>
          <Form.Item
            name="checkpointsText"
            label="审核要点（每行一条）"
            initialValue={(editing?.checkpoints ?? []).join('\n')}
          >
            <Input.TextArea rows={4} placeholder={'逐项核对保证金金额\n核对签署日期'} />
          </Form.Item>
          <Form.Item name="legal_basis" label="法规依据">
            <Input maxLength={300} placeholder="第X条（原文要点）" />
          </Form.Item>
        </Form>
      </Modal>
    </Drawer>
  )
}

// --------------------------------------------------------------------------- //
// 转正弹窗
// --------------------------------------------------------------------------- //

function PromoteModal({
  target,
  onClose,
  onDone,
}: {
  target: LegalRuleset | null
  onClose: () => void
  onDone: () => void
}) {
  const { message } = AntdApp.useApp()
  const [name, setName] = useState('')
  const [doing, setDoing] = useState(false)

  useEffect(() => {
    if (target) setName(`${target.name}（正式）`)
  }, [target])

  const submit = async () => {
    if (!target) return
    setDoing(true)
    try {
      await api.promoteLegalRuleset(target.id, name.trim() || undefined)
      onDone()
    } catch (e) {
      message.error((e as Error).message)
    } finally {
      setDoing(false)
    }
  }

  return (
    <Modal
      title="转正为正式规则集"
      open={!!target}
      onCancel={onClose}
      onOk={() => void submit()}
      confirmLoading={doing}
      okText="转正"
      destroyOnClose
    >
      <Typography.Paragraph type="secondary">
        把临时规则集固化到正式规则库，可在任务向导的规则集中长期选用。
      </Typography.Paragraph>
      <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="正式规则集名称" maxLength={80} />
    </Modal>
  )
}
