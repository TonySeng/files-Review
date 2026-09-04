import { useEffect, useMemo, useState } from 'react'
import {
  App as AntdApp,
  Alert,
  Button,
  Collapse,
  Descriptions,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  Input,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  ArrowLeftOutlined,
  ArrowUpOutlined,
  DeleteOutlined,
  DownloadOutlined,
  RedoOutlined,
  StopOutlined,
} from '@ant-design/icons'
import ProgressPanel from '../components/ProgressPanel'
import ResultPanel from '../components/ResultPanel'
import RuleSelection, { type RuleConfigMode } from '../components/RuleSelection'
import { deriveReviewMode } from '../utils/deriveMode'
import { api } from '../services/api'
import type { StartPayload } from '../hooks/useReviewTasks'
import type {
  FileType,
  KnowledgeBase,
  ReviewTaskDetail,
  RuleGroup,
  RuleSet,
  TaskStatus,
  UploadedFile,
} from '../types'

const STATUS_META: Record<TaskStatus, { label: string; color: string }> = {
  pending: { label: '排队中', color: 'blue' },
  running: { label: '进行中', color: 'processing' },
  completed: { label: '已完成', color: 'success' },
  failed: { label: '失败', color: 'error' },
  cancelled: { label: '已取消', color: 'default' },
}

const TERMINAL: TaskStatus[] = ['completed', 'failed', 'cancelled']

interface Props {
  task: ReviewTaskDetail | null
  ruleGroups: RuleGroup[]
  rulesets: RuleSet[]
  fileTypes: FileType[]
  knowledgeBases: KnowledgeBase[]
  onBack: () => void
  onRerun: (payload: StartPayload) => void
  onCancel: (id: string) => void
  onRemove: (id: string) => void
  onRefreshKnowledgeBases?: () => void
}

/**
 * 任务详情页：展示审核文件列表、规则清单与全部配置，并支持修改配置后「重新运行」。
 */
export default function TaskDetailPage({
  task,
  ruleGroups,
  rulesets,
  fileTypes,
  knowledgeBases,
  onBack,
  onRerun,
  onCancel,
  onRemove,
  onRefreshKnowledgeBases,
}: Props) {
  const { message } = AntdApp.useApp()
  const [rerunOpen, setRerunOpen] = useState(false)
  // 滚动超过阈值时显示「返回顶部」悬浮按钮
  const [showBackTop, setShowBackTop] = useState(false)
  useEffect(() => {
    const onScroll = () => setShowBackTop(window.scrollY > 320)
    window.addEventListener('scroll', onScroll, { passive: true })
    onScroll()
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  // 重跑编辑器初始值（来自任务原始请求）
  const [editRuleConfigMode, setEditRuleConfigMode] = useState<RuleConfigMode>('group')
  const [editGroupIds, setEditGroupIds] = useState<string[]>([])
  const [editRulesetId, setEditRulesetId] = useState('')
  const [editRuleIds, setEditRuleIds] = useState<string[]>([])
  const [editKbEnabled, setEditKbEnabled] = useState(false)
  const [editKbId, setEditKbId] = useState('')
  const [editWeb, setEditWeb] = useState(false)
  const [editExtra, setEditExtra] = useState('')

  // 审核模式不手动选择，由重跑弹窗内的规则集 / 规则组选择自动推导
  const editReviewMode = useMemo(
    () =>
      deriveReviewMode({
        configMode: editRuleConfigMode,
        activeRulesetId: editRulesetId,
        selectedGroupIds: editGroupIds,
        rulesets,
        ruleGroups,
      }),
    [editRuleConfigMode, editRulesetId, editGroupIds, rulesets, ruleGroups],
  )

  const findRuleset = (id: string) => rulesets.find((r) => r.id === id)
  const enabledRuleIdsOf = (id: string) =>
    findRuleset(id)?.rules.filter((r) => r.enabled).map((r) => r.id) || []

  const ruleNameMap = useMemo(() => {
    const m = new Map<string, { name: string; category: string }>()
    rulesets.forEach((rs) =>
      rs.rules.forEach((r) => m.set(r.id, { name: r.name, category: r.category })),
    )
    return m
  }, [rulesets])

  // 重跑弹窗内，按文件类型预览自动匹配所需的文件对象
  const detailFiles: UploadedFile[] = useMemo(() => {
    const names = task?.file_names || []
    const types = task?.request?.file_types || []
    return names.map((n, i) => ({
      file_id: String(i),
      filename: n,
      size: 0,
      ext: '',
      role: 'attachment',
      file_type: types[i] ?? null,
      char_count: 0,
      page_count: 0,
      used_ocr: false,
      parse_error: null,
      validation: null,
    }))
  }, [task])

  const fileTypeName = useMemo(
    () => new Map(fileTypes.map((t) => [t.id, t.name])),
    [fileTypes],
  )

  // 以下声明必须在「!task 提前返回」之前完成，否则 hooks 数量随 task 变化而变动，
  // 触发 React 「Rendered more hooks than during the previous render」导致整页崩溃。
  const running = !TERMINAL.includes(task?.status ?? 'pending')
  const req = (task?.request ?? {}) as ReviewTaskDetail['request']

  // 规则清单：优先用 request.rule_ids；规则组展开兜底；仅按规则集创建（未显式传 rule_ids）时从规则集定义展开
  const ruleRows = useMemo(() => {
    let ids: string[] = req.rule_ids && req.rule_ids.length ? req.rule_ids : []
    if (!ids.length && req.ruleset_id) {
      const rs = findRuleset(req.ruleset_id)
      ids = (rs?.rules || []).filter((r) => r.enabled !== false).map((r) => r.id)
    }
    const fromGroups = (req.rule_group_ids || []).flatMap(
      (gid) => ruleGroups.find((g) => g.id === gid)?.rule_ids || [],
    )
    const merged = Array.from(new Set([...ids, ...fromGroups]))
    return merged.map((id) => ({
      key: id,
      name: ruleNameMap.get(id)?.name || id,
      category: ruleNameMap.get(id)?.category || '',
    }))
  }, [req, ruleGroups, ruleNameMap, rulesets])

  if (!task) {
    return (
      <div style={{ textAlign: 'center', padding: '80px 0' }}>
        <Spin tip="加载任务详情…" />
      </div>
    )
  }

  // 审核文件列表：真实 file_id 来自 req.file_ids（与 task.file_names 按序对应）；
  // 旧任务可能未持久化 file_ids，则退化为仅有文件名（下载按钮不可用）。
  const fileRows = (
    req.file_ids && req.file_ids.length
      ? req.file_ids.map((fid, i) => ({ file_id: fid, name: task.file_names?.[i] || fid }))
      : (task.file_names || []).map((name) => ({ file_id: '', name }))
  ).map((r, i) => ({ key: String(i), ...r, type: req.file_types?.[i] || null }))

  const openRerun = () => {
    onRefreshKnowledgeBases?.()
    const mode = req.mode || 'bid'
    const groupIds = req.rule_group_ids || []
    const rMode: RuleConfigMode = groupIds.length ? 'group' : 'ruleset'
    const rsId = req.ruleset_id || `mode-${mode}`
    setEditRuleConfigMode(rMode)
    setEditGroupIds(groupIds)
    setEditRulesetId(rsId)
    setEditRuleIds(req.rule_ids && req.rule_ids.length ? req.rule_ids : enabledRuleIdsOf(rsId))
    setEditKbEnabled(!!req.kb_enabled)
    setEditKbId(req.kb_id || '')
    setEditWeb(!!req.web_search_enabled)
    setEditExtra(req.extra_instruction || '')
    setRerunOpen(true)
  }

  const confirmRerun = () => {
    if (editRuleConfigMode === 'group' ? !editGroupIds.length : !editRuleIds.length) {
      message.warning(
        editRuleConfigMode === 'group' ? '请至少选择一个审核规则组' : '请至少勾选一条审核规则',
      )
      return
    }
    const base = {
      file_ids: req.file_ids || [],
      mode: editReviewMode,
      kb_enabled: editKbEnabled,
      kb_id: editKbId || undefined,
      web_search_enabled: editWeb,
      extra_instruction: editExtra,
    }
    onRerun(
      editRuleConfigMode === 'group'
        ? {
            ...base,
            ruleset_id: editRulesetId,
            rule_ids: [],
            rule_group_ids: editGroupIds,
            auto_match: req.auto_match,
          }
        : {
            ...base,
            ruleset_id: editRulesetId,
            rule_ids: editRuleIds,
            rule_group_ids: [],
            auto_match: false,
          },
    )
    setRerunOpen(false)
  }

  const fileColumns: ColumnsType<{ key: string; file_id: string; name: string; type: string | null }> = [
    {
      title: '序号',
      dataIndex: 'key',
      width: 64,
      render: (k: string) => <Typography.Text type="secondary">{Number(k) + 1}</Typography.Text>,
    },
    { title: '文件名', dataIndex: 'name', render: (n: string) => <span style={{ fontSize: 13 }}>{n}</span> },
    {
      title: '文件类型',
      dataIndex: 'type',
      width: 160,
      render: (t: string | null) =>
        t ? <Tag color="geekblue">{fileTypeName.get(t) || t}</Tag> : <Typography.Text type="secondary">—</Typography.Text>,
    },
    {
      title: '操作',
      key: 'action',
      width: 88,
      render: (_: unknown, row: { key: string; file_id: string; name: string; type: string | null }) =>
        row.file_id ? (
          <Button
            type="link"
            size="small"
            icon={<DownloadOutlined />}
            onClick={() => handleDownload(row.file_id, row.name)}
          >
            下载
          </Button>
        ) : (
          <Tooltip title="该历史任务未记录文件引用，无法下载">
            <Typography.Text type="secondary">—</Typography.Text>
          </Tooltip>
        ),
    },
  ]

  const handleDownload = (fileId: string, filename: string) => {
    const key = `dl-${fileId}`
    message.loading({ content: '正在准备下载…', key })
    api
      .downloadFile(fileId, filename)
      .then(() => message.success({ content: '已开始下载', key }))
      .catch((e) => message.error({ content: String(e?.message || e), key }))
  }

  const ruleColumns: ColumnsType<{ key: string; name: string; category: string }> = [
    { title: '规则名称', dataIndex: 'name', render: (n: string) => <span style={{ fontSize: 13 }}>{n}</span> },
    {
      title: '类别',
      dataIndex: 'category',
      width: 140,
      render: (c: string) =>
        c ? (
          <Tag>{c}</Tag>
        ) : (
          <Typography.Text type="secondary">—</Typography.Text>
        ),
    },
  ]

  return (
    <div className="task-detail-page">
      <div className="task-detail-header">
        <Button icon={<ArrowLeftOutlined />} onClick={onBack}>
          返回列表
        </Button>
        <Space size={8} wrap>
          <Tag color={STATUS_META[task.status].color} style={{ fontSize: 13 }}>
            {STATUS_META[task.status].label}
          </Tag>
          <Typography.Title level={5} style={{ margin: 0 }}>
            {task.file_names?.[0] || '审核任务详情'}
          </Typography.Title>
          {task.score != null && (
            <Tag color={task.score >= 85 ? 'success' : task.score >= 60 ? 'warning' : 'error'}>
              合规得分 {task.score}
            </Tag>
          )}
        </Space>
        <Space size={8}>
          <Button
            type="primary"
            icon={<RedoOutlined />}
            onClick={openRerun}
            disabled={running}
          >
            重新运行
          </Button>
          {running ? (
            <Tooltip title="取消该审核任务">
              <Button danger icon={<StopOutlined />} onClick={() => onCancel(task.task_id)}>
                取消
              </Button>
            </Tooltip>
          ) : (
            <PopconfirmWrap
              onConfirm={() => onRemove(task.task_id)}
              title="删除该任务记录？"
            />
          )}
        </Space>
      </div>

      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        {/* 审核过程：置于核心区顶部；运行中与已完成的歷史任务均展示 */}
        <ProgressPanel
          logs={task.logs}
          progress={task.progress}
          running={running}
          kbCount={task.kb_traces?.length || 0}
        />

        {/* 审核结果：核心交付物，紧随过程之后 */}
        {task.reflow_applied && (
          <Alert
            type="success"
            showIcon
            style={{ marginBottom: 12 }}
            message="本次审核已自动带入历史审核结论"
            description="所上传文件的 MD5 与本次审核规则对应关系此前已审核过，历史采纳/不采纳结论（含不采纳原因）已作为约束注入模型，确保输出与历史处理保持一致。"
          />
        )}
        <ResultPanel
          findings={task.findings}
          issues={task.consistency_issues}
          ruleResults={task.rule_results}
          kbTraces={task.kb_traces}
          summary={task.summary}
          running={running}
          taskId={task.task_id}
          fileIds={req.file_ids || []}
        />

        {/* 次要信息：默认全部折叠；审核规则清单（含规则组/自动匹配标记）收起于此 */}
        <Collapse
          size="small"
          defaultActiveKey={[]}
          items={[
            {
              key: 'files',
              label: `审核文件（${fileRows.length} 个）`,
              children: (
                <Table
                  rowKey="key"
                  size="small"
                  columns={fileColumns}
                  dataSource={fileRows}
                  pagination={false}
                />
              ),
            },
            {
              key: 'config',
              label: '审核配置',
              children: (
                <Descriptions size="small" bordered column={{ xs: 1, sm: 2 }}>
                  <Descriptions.Item label="自动匹配">
                    {req.auto_match ? '开启' : '关闭'}
                  </Descriptions.Item>
                  <Descriptions.Item label="知识库">
                    {req.kb_enabled
                      ? req.kb_id
                        ? knowledgeBases.find((k) => k.id === req.kb_id)?.name || '已指定'
                        : '使用系统默认'
                      : '未启用'}
                  </Descriptions.Item>
                  <Descriptions.Item label="联网搜索">
                    {req.web_search_enabled ? '已启用' : '未启用'}
                  </Descriptions.Item>
                  <Descriptions.Item label="补充要求" span={2}>
                    {req.extra_instruction || (
                      <Typography.Text type="secondary">（无）</Typography.Text>
                    )}
                  </Descriptions.Item>
                </Descriptions>
              ),
            },
            {
              key: 'rules',
              label: `审核规则清单（${ruleRows.length} 条）`,
              children: (
                <>
                  {(req.rule_group_ids || []).length > 0 || req.auto_match ? (
                    <Space size={4} wrap style={{ marginBottom: 8 }}>
                      {(req.rule_group_ids || []).map((gid) => (
                        <Tag key={gid} color="blue">
                          {ruleGroups.find((g) => g.id === gid)?.name || gid}
                        </Tag>
                      ))}
                      {req.auto_match && <Tag color="green">自动匹配</Tag>}
                    </Space>
                  ) : null}
                  <Table
                    rowKey="key"
                    size="small"
                    columns={ruleColumns}
                    dataSource={ruleRows}
                    pagination={false}
                  />
                </>
              ),
            },
          ]}
        />
      </Space>

      {showBackTop && (
        <Button
          className="detail-backtop"
          type="primary"
          shape="circle"
          size="large"
          icon={<ArrowUpOutlined />}
          aria-label="返回顶部"
          onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}
        />
      )}

      <Modal
        title="修改配置并重新运行"
        open={rerunOpen}
        onCancel={() => setRerunOpen(false)}
        onOk={confirmRerun}
        okText="重新运行"
        cancelText="取消"
        width={560}
        destroyOnClose
      >
        <Space direction="vertical" size={14} style={{ width: '100%' }}>
          <RuleSelection
            configMode={editRuleConfigMode}
            onConfigModeChange={(m) => {
              setEditRuleConfigMode(m)
              if (m === 'group') {
                setEditRuleIds([])
              } else {
                setEditGroupIds([])
                const id = `mode-${editReviewMode}`
                setEditRulesetId(id)
                setEditRuleIds(enabledRuleIdsOf(id))
              }
            }}
            ruleGroups={ruleGroups}
            selectedGroupIds={editGroupIds}
            onSelectedGroupsChange={setEditGroupIds}
            autoMatch={false}
            onAutoMatchChange={() => undefined}
            fileTypes={fileTypes}
            files={detailFiles}
            rulesets={rulesets}
            activeRulesetId={editRulesetId}
            selectedRuleIds={editRuleIds}
            onRulesetChange={(id) => {
              setEditRulesetId(id)
              setEditRuleIds(enabledRuleIdsOf(id))
            }}
            onSelectedRulesChange={setEditRuleIds}
            onOpenManagement={() => setRerunOpen(false)}
            mode={editReviewMode}
            selectedLegalRulesetId={''}
            onSelectedLegalRulesetIdChange={() => undefined}
            legalRulesOnly={true}
            onLegalRulesOnlyChange={() => undefined}
          />
          <Space size={8} align="center">
            <Switch checked={editKbEnabled} onChange={setEditKbEnabled} />
            <Typography.Text>启用知识库检索</Typography.Text>
            <Select
              style={{ width: 220 }}
              allowClear
              placeholder="关联知识库"
              value={editKbId || undefined}
              disabled={!editKbEnabled}
              onChange={setEditKbId}
              options={knowledgeBases.map((kb) => ({
                value: kb.id,
                label: `${kb.name}（${kb.document_count} 篇）`,
              }))}
            />
          </Space>
          <Space size={8} align="center">
            <Switch checked={editWeb} onChange={setEditWeb} />
            <Typography.Text>启用联网搜索</Typography.Text>
          </Space>
          <Input.TextArea
            rows={3}
            value={editExtra}
            onChange={(e) => setEditExtra(e.target.value)}
            placeholder="补充审核要求（选填）"
          />
        </Space>
      </Modal>
    </div>
  )
}

// 轻量封装：避免 antd Popconfirm 在 TSX 中需显式导入的冗余
function PopconfirmWrap({
  title,
  onConfirm,
}: {
  title: string
  onConfirm: () => void
}) {
  return (
    <Popconfirm title={title} okText="删除" cancelText="取消" okButtonProps={{ danger: true }} onConfirm={onConfirm}>
      <Button danger icon={<DeleteOutlined />}>
        删除
      </Button>
    </Popconfirm>
  )
}
