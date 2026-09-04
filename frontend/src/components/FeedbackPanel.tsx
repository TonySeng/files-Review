import { useCallback, useEffect, useState } from 'react'
import {
  Button,
  Card,
  Col,
  Descriptions,
  Drawer,
  Empty,
  Input,
  Modal,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd'
import {
  ArrowLeftOutlined,
  DatabaseOutlined,
  DownloadOutlined,
  FilterOutlined,
  StopOutlined,
  SyncOutlined,
} from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import type {
  FeedbackJudgment,
  FeedbackRecord,
  FeedbackStats,
  ValidationFilter,
  ValidationItem,
} from '../types'
import { api } from '../services/api'

const STATUS_COLOR: Record<string, string> = {
  pending: 'default',
  accepted: 'success',
  rejected: 'error',
  filtered: 'warning',
}
const STATUS_LABEL: Record<string, string> = {
  pending: '待判定',
  accepted: '已采纳',
  rejected: '已不采纳',
  filtered: '已过滤(去噪)',
}

// 过滤规则状态（active 与否）
const FILTER_STATUS: Record<number, { color: string; label: string }> = {
  1: { color: 'success', label: '生效' },
  0: { color: 'default', label: '已停用' },
}

interface FeedbackPanelProps {
  onBack: () => void
  onOpenTask: (taskId: string) => void
}

export default function FeedbackPanel({ onBack, onOpenTask }: FeedbackPanelProps) {
  const [stats, setStats] = useState<FeedbackStats | null>(null)

  // 训练数据集
  const [records, setRecords] = useState<FeedbackRecord[]>([])
  const [recTotal, setRecTotal] = useState(0)
  const [recJudgment, setRecJudgment] = useState<FeedbackJudgment | undefined>()
  const [recTypo, setRecTypo] = useState(false)
  const [recQuery, setRecQuery] = useState('')
  const [recLoading, setRecLoading] = useState(false)

  // 校验集
  const [items, setItems] = useState<ValidationItem[]>([])
  const [itemLoading, setItemLoading] = useState(false)

  // 过滤规则
  const [filters, setFilters] = useState<ValidationFilter[]>([])
  const [filterLoading, setFilterLoading] = useState(false)

  // 校验项审核结果抽屉（点击校验集行打开，不再跳转到原始任务）
  const [drawerItem, setDrawerItem] = useState<ValidationItem | null>(null)
  // 驳回理由弹窗（必填）
  const [rejectOpen, setRejectOpen] = useState(false)
  const [rejectReason, setRejectReason] = useState('')
  // 停用过滤规则理由弹窗（必填）
  const [deactOpen, setDeactOpen] = useState(false)
  const [deactReason, setDeactReason] = useState('')
  const [deactTarget, setDeactTarget] = useState<ValidationFilter | null>(null)

  // 内部标签页（错别字校验集 / 过滤规则 在进入时即自动加载，无需手动点刷新）
  const [fbTab, setFbTab] = useState<string>('training')

  const loadStats = useCallback(async () => {
    try {
      setStats(await api.feedbackStats())
    } catch {
      /* 忽略 */
    }
  }, [])

  const loadRecords = useCallback(async () => {
    setRecLoading(true)
    try {
      const r = await api.listFeedback({
        judgment: recJudgment,
        is_typo: recTypo || undefined,
        q: recQuery || undefined,
        limit: 100,
      })
      setRecords(r.records)
      setRecTotal(r.total)
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setRecLoading(false)
    }
  }, [recJudgment, recTypo, recQuery])

  const loadItems = useCallback(async () => {
    setItemLoading(true)
    try {
      const r = await api.listValidationItems({ limit: 200 })
      setItems(r.items)
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setItemLoading(false)
    }
  }, [])

  const loadFilters = useCallback(async () => {
    setFilterLoading(true)
    try {
      const r = await api.listFilters()
      setFilters(r.filters)
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setFilterLoading(false)
    }
  }, [])

  useEffect(() => {
    loadStats()
  }, [loadStats])

  useEffect(() => {
    loadRecords()
  }, [loadRecords])

  // 错别字校验集与过滤规则进入即加载（修复：原先仅手动点「刷新」才会拉取，导致打开即空白）
  useEffect(() => {
    loadItems()
  }, [loadItems])

  useEffect(() => {
    loadFilters()
  }, [loadFilters])

  const handleExport = async () => {
    try {
      const resp = await api.exportFeedback({
        judgment: recJudgment,
        is_typo: recTypo || undefined,
        q: recQuery || undefined,
      })
      if (!resp.ok) throw new Error('导出失败')
      const blob = await resp.blob()
      const url = window.URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `feedback_training_data_${new Date().toLocaleDateString('zh-CN')}.jsonl`
      document.body.appendChild(a)
      a.click()
      window.URL.revokeObjectURL(url)
      document.body.removeChild(a)
      message.success('已导出训练数据（JSONL）')
    } catch (err) {
      message.error((err as Error).message)
    }
  }

  const handleDeactivate = async (id: string, reason: string) => {
    try {
      await api.deactivateFilter(id, reason)
      message.success('已停用该过滤规则，记录保留并标记「已停用」；后续审核将重新校验该错别字')
      setDeactOpen(false)
      setDeactReason('')
      setDeactTarget(null)
      loadFilters()
      loadStats()
    } catch (err) {
      message.error((err as Error).message)
    }
  }

  // 在审核结果抽屉内更新某条校验项的判定（采纳/驳回/待判定）
  const applyStatus = async (
    row: ValidationItem,
    status: 'accepted' | 'rejected' | 'pending',
    rejectReason?: string | null,
  ) => {
    try {
      await api.updateValidationItemStatus(row.id, status, rejectReason ?? null)
      message.success(
        status === 'accepted'
          ? '已标记为「已采纳」'
          : status === 'rejected'
            ? '已标记为「驳回」并进入过滤规则列表'
            : '已恢复为「待判定」',
      )
      setDrawerItem(null)
      setRejectOpen(false)
      setRejectReason('')
      loadItems()
      loadFilters()
      loadStats()
    } catch (err) {
      message.error((err as Error).message)
    }
  }

  const openReject = (row: ValidationItem) => {
    setDrawerItem(row)
    setRejectReason('')
    setRejectOpen(true)
  }

  const confirmReject = () => {
    const reason = rejectReason.trim()
    if (!reason) {
      message.error('驳回时必须填写补充说明')
      return
    }
    if (drawerItem) void applyStatus(drawerItem, 'rejected', reason)
  }

  const openDeactivate = (row: ValidationFilter) => {
    setDeactTarget(row)
    setDeactReason('')
    setDeactOpen(true)
  }

  // 从数据记录跳转到对应的审核任务详情；无关联任务时给出提示
  const openTask = useCallback(
    (taskId: string | null) => {
      if (!taskId) {
        message.warning('该记录未关联审核任务，无法跳转')
        return
      }
      onOpenTask(taskId)
    },
    [onOpenTask],
  )

  const recColumns: ColumnsType<FeedbackRecord> = [
    {
      title: '判定',
      dataIndex: 'judgment',
      width: 90,
      render: (j: FeedbackJudgment) =>
        j === 'adopt' ? (
          <Tag color="success">采纳</Tag>
        ) : (
          <Tag color="error">不采纳</Tag>
        ),
    },
    {
      title: '规则',
      dataIndex: 'rule_name',
      width: 170,
      render: (name: string, row) => (
        <Space direction="vertical" size={2}>
          <span style={{ fontSize: 13 }}>{name || row.rule_id}</span>
          {row.is_typo && (
            <Tag color="purple" style={{ marginRight: 0 }}>
              错别字
            </Tag>
          )}
        </Space>
      ),
    },
    {
      title: '结论 / 原文识别',
      dataIndex: 'title',
      render: (title: string, row) => (
        <Space direction="vertical" size={3} style={{ width: '100%' }}>
          <Typography.Text style={{ fontSize: 13 }}>{title || '（无结论）'}</Typography.Text>
          {row.typo_wrong && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              错别字「{row.typo_wrong}」→ 应为「{row.typo_correct}」
            </Typography.Text>
          )}
          {row.judgment === 'reject' && row.reject_reason && (
            <Typography.Text type="danger" style={{ fontSize: 12 }}>
              不采纳原因：{row.reject_reason}
            </Typography.Text>
          )}
        </Space>
      ),
    },
    {
      title: '时间',
      dataIndex: 'created_at',
      width: 160,
      render: (t: string) => (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {t?.replace('T', ' ')}
        </Typography.Text>
      ),
    },
    {
      title: '操作',
      key: 'action',
      width: 100,
      fixed: 'right',
      render: (_: unknown, row: FeedbackRecord) => (
        <Button
          type="link"
          size="small"
          disabled={!row.task_id}
          onClick={(e) => {
            e.stopPropagation()
            openTask(row.task_id)
          }}
        >
          查看任务
        </Button>
      ),
    },
  ]

  const itemColumns: ColumnsType<ValidationItem> = [
    {
      title: '错字',
      dataIndex: 'typo_wrong',
      width: 120,
      render: (w: string | null) =>
        w ? (
          <Tag color="purple" style={{ whiteSpace: 'normal', wordBreak: 'break-all' }}>
            {w}
          </Tag>
        ) : (
          <Typography.Text type="secondary">—</Typography.Text>
        ),
    },
    {
      title: '正字',
      dataIndex: 'typo_correct',
      width: 120,
      render: (c: string | null) =>
        c ? (
          <span style={{ whiteSpace: 'normal', wordBreak: 'break-all' }}>{c}</span>
        ) : (
          '—'
        ),
    },
    {
      title: '规则',
      dataIndex: 'rule_id',
      width: 150,
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 120,
      render: (s: string) => <Tag color={STATUS_COLOR[s]}>{STATUS_LABEL[s] || s}</Tag>,
    },
    {
      title: '原文结论',
      key: 'title',
      render: (_: unknown, row) => (
        <Typography.Text style={{ fontSize: 13 }}>
          {row.original_finding?.title || '（无）'}
        </Typography.Text>
      ),
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      width: 160,
      render: (t: string) => (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {t?.replace('T', ' ')}
        </Typography.Text>
      ),
    },
    {
      title: '操作',
      key: 'action',
      width: 90,
      fixed: 'right',
      render: (_: unknown, row: ValidationItem) => (
        <Button type="link" size="small" onClick={(e) => { e.stopPropagation(); setDrawerItem(row) }}>
          详情
        </Button>
      ),
    },
  ]

  const filterColumns: ColumnsType<ValidationFilter> = [
    {
      title: '错字',
      dataIndex: 'typo_wrong',
      width: 120,
      render: (w: string) => (
        <Tag color="purple" style={{ whiteSpace: 'normal', wordBreak: 'break-all' }}>
          {w}
        </Tag>
      ),
    },
    {
      title: '正字',
      dataIndex: 'typo_correct',
      width: 120,
      render: (c) =>
        c ? <span style={{ whiteSpace: 'normal', wordBreak: 'break-all' }}>{c}</span> : '—',
    },
    {
      title: '状态',
      dataIndex: 'active',
      width: 100,
      render: (a: boolean) => {
        const s = FILTER_STATUS[a ? 1 : 0]
        return <Tag color={s.color}>{s.label}</Tag>
      },
    },
    {
      title: '不采纳原因',
      dataIndex: 'reject_reason',
      render: (r: string | null) => r || '—',
    },
    {
      title: '停用说明',
      dataIndex: 'deactivate_reason',
      render: (r: string | null) => r || '—',
    },
    {
      title: '自动跳过次数',
      dataIndex: 'hit_count',
      width: 110,
      render: (n: number) => <Tag color="blue">{n}</Tag>,
    },
    {
      title: '操作',
      key: 'action',
      width: 110,
      render: (_: unknown, row) =>
        row.active ? (
          <Button
            danger
            size="small"
            icon={<StopOutlined />}
            onClick={() => openDeactivate(row)}
          >
            停用
          </Button>
        ) : (
          <Tag>已停用</Tag>
        ),
    },
  ]

  return (
    <div className="rm-page">
      {/* 顶部栏：返回工作台 + 标题 */}
      <div className="rm-topbar">
        <button className="rm-back" type="button" onClick={onBack}>
          <ArrowLeftOutlined />
          返回工作台
        </button>
        <div className="rm-title-wrap">
          <div className="rm-title">
            <DatabaseOutlined className="rm-title-icon" />
            反馈与训练数据
          </div>
          <div className="rm-subtitle">
            收集用户对审核结论的采纳 / 不采纳反馈，沉淀训练数据与错别字去噪过滤规则，并支持回溯至原始审核任务
          </div>
        </div>
        <Tag
          color="purple"
          icon={<FilterOutlined />}
          style={{ fontSize: 13, padding: '4px 10px', marginLeft: 'auto' }}
        >
          活跃过滤规则 {stats?.active_filters ?? 0}
        </Tag>
      </div>

      {/* 概览卡 */}
      <div className="rm-headcard" style={{ marginBottom: 16 }}>
        <Row gutter={[16, 16]} align="middle">
          <Col xs={12} sm={6} md={6}>
            <Statistic title="反馈总数" value={stats?.total ?? 0} prefix={<DatabaseOutlined />} />
          </Col>
          <Col xs={12} sm={6} md={6}>
            <Statistic
              title="采纳"
              value={stats?.adopt ?? 0}
              valueStyle={{ color: '#52c41a' }}
            />
          </Col>
          <Col xs={12} sm={6} md={6}>
            <Statistic
              title="不采纳"
              value={stats?.reject ?? 0}
              valueStyle={{ color: '#ff4d4f' }}
            />
          </Col>
          <Col xs={12} sm={6} md={6}>
            <Statistic
              title="活跃过滤规则"
              value={stats?.active_filters ?? 0}
              valueStyle={{ color: '#722ed1' }}
            />
          </Col>
        </Row>
      </div>

      <Tabs
        activeKey={fbTab}
        onChange={(k) => {
          setFbTab(k)
          if (k === 'validation') void loadItems()
          else if (k === 'filters') void loadFilters()
        }}
        items={[
          {
            key: 'training',
            label: (
              <span>
                <DatabaseOutlined /> 训练数据集（{recTotal}）
              </span>
            ),
            children: (
              <>
                <div className="rm-filterbar" style={{ marginBottom: 12 }}>
                  <Space wrap>
                    <Select
                      style={{ width: 140 }}
                      placeholder="全部判定"
                      allowClear
                      value={recJudgment}
                      onChange={(v) => setRecJudgment(v)}
                      options={[
                        { value: 'adopt', label: '采纳' },
                        { value: 'reject', label: '不采纳' },
                      ]}
                    />
                    <Space size={6}>
                      <span style={{ fontSize: 13 }}>仅错别字</span>
                      <Switch checked={recTypo} onChange={setRecTypo} />
                    </Space>
                    <Input.Search
                      placeholder="搜索结论/原文/错字"
                      allowClear
                      style={{ width: 240 }}
                      onSearch={(v) => setRecQuery(v)}
                    />
                  </Space>
                  <div className="rm-filterbar-right">
                    <Button icon={<DownloadOutlined />} onClick={handleExport}>
                      导出 JSONL
                    </Button>
                  </div>
                </div>
                <Card bordered={false} style={{ borderRadius: 12, boxShadow: 'var(--rm-shadow-sm)' }}>
                  <Spin spinning={recLoading}>
                    <Table
                      size="small"
                      rowKey="id"
                      columns={recColumns}
                      dataSource={records}
                      pagination={false}
                      scroll={{ x: 920 }}
                      onRow={(record: FeedbackRecord) => ({
                        style: { cursor: record.task_id ? 'pointer' : 'default' },
                        onClick: () => openTask(record.task_id),
                      })}
                    />
                  </Spin>
                </Card>
              </>
            ),
          },
          {
            key: 'validation',
            label: (
              <span>
                <FilterOutlined /> 错别字校验集（{items.length}）
              </span>
            ),
            children: (
              <>
                <div className="rm-filterbar" style={{ marginBottom: 12 }}>
                  <div className="rm-filterbar-right" style={{ marginLeft: 'auto' }}>
                    <Button size="small" icon={<SyncOutlined />} onClick={loadItems}>
                      刷新
                    </Button>
                  </div>
                </div>
                <Card bordered={false} style={{ borderRadius: 12, boxShadow: 'var(--rm-shadow-sm)' }}>
                  <Spin spinning={itemLoading}>
                    <Table
                      size="small"
                      rowKey="id"
                      columns={itemColumns}
                      dataSource={items}
                      pagination={false}
                      scroll={{ x: 920 }}
                      onRow={(record: ValidationItem) => ({
                        style: { cursor: 'pointer' },
                        onClick: () => setDrawerItem(record),
                      })}
                    />
                  </Spin>
                </Card>
              </>
            ),
          },
          {
            key: 'filters',
            label: (
              <span>
                <StopOutlined /> 过滤规则（{filters.length}）
              </span>
            ),
            children: filters.length === 0 ? (
              <Empty
                style={{ padding: '48px 0' }}
                description="暂无过滤规则。在审核结果中把某条错别字标记为「不采纳」后，会自动生成过滤规则实现去噪。"
              />
            ) : (
              <>
                <div className="rm-filterbar" style={{ marginBottom: 12 }}>
                  <div className="rm-filterbar-right" style={{ marginLeft: 'auto' }}>
                    <Button size="small" icon={<SyncOutlined />} onClick={loadFilters}>
                      刷新
                    </Button>
                  </div>
                </div>
                <Card bordered={false} style={{ borderRadius: 12, boxShadow: 'var(--rm-shadow-sm)' }}>
                  <Spin spinning={filterLoading}>
                    <Table
                      size="small"
                      rowKey="id"
                      columns={filterColumns}
                      dataSource={filters}
                      pagination={false}
                      scroll={{ x: 640 }}
                    />
                  </Spin>
                </Card>
              </>
            ),
          },
        ]}
      />

      {/* 错别字校验项审核结果抽屉：点击校验集行打开，不再跳转原始任务 */}
      <Drawer
        title={
          drawerItem
            ? `错别字「${drawerItem.typo_wrong}」→「${drawerItem.typo_correct}」审核详情`
            : ''
        }
        width={560}
        open={!!drawerItem}
        onClose={() => setDrawerItem(null)}
        footer={
          drawerItem && (
            <Space>
              <Button type="primary" onClick={() => void applyStatus(drawerItem, 'accepted')}>
                标注采纳
              </Button>
              <Button danger onClick={() => openReject(drawerItem)}>
                驳回
              </Button>
              <Button onClick={() => void applyStatus(drawerItem, 'pending')}>
                待判定
              </Button>
            </Space>
          )
        }
      >
        {drawerItem &&
          (() => {
            const f = (drawerItem.original_finding as unknown as Record<string, unknown>) || {}
            const files = Array.isArray(f.involved_files)
              ? (f.involved_files as string[])
              : []
            return (
              <>
                <Descriptions column={1} size="small" bordered>
                  <Descriptions.Item label="状态">
                    <Tag color={STATUS_COLOR[drawerItem.status]}>
                      {STATUS_LABEL[drawerItem.status] || drawerItem.status}
                    </Tag>
                  </Descriptions.Item>
                  <Descriptions.Item label="规则">{drawerItem.rule_id}</Descriptions.Item>
                  <Descriptions.Item label="关联任务">
                    {drawerItem.task_id ? (
                      <Button
                        type="link"
                        size="small"
                        style={{ padding: 0 }}
                        onClick={() => openTask(drawerItem.task_id)}
                      >
                        查看原始任务
                      </Button>
                    ) : (
                      '（无）'
                    )}
                  </Descriptions.Item>
                  <Descriptions.Item label="更新时间">
                    {drawerItem.updated_at?.replace('T', ' ')}
                  </Descriptions.Item>
                </Descriptions>

                <Typography.Title level={5} style={{ marginTop: 16 }}>
                  原文审核结论
                </Typography.Title>
                <Descriptions column={1} size="small" bordered>
                  <Descriptions.Item label="标题">
                    {(f.title as string) || '（无）'}
                  </Descriptions.Item>
                  <Descriptions.Item label="详情">
                    {(f.detail as string) || '（无）'}
                  </Descriptions.Item>
                  <Descriptions.Item label="证据">
                    {(f.evidence as string) || '（无）'}
                  </Descriptions.Item>
                  <Descriptions.Item label="建议">
                    {(f.suggestion as string) || '（无）'}
                  </Descriptions.Item>
                  <Descriptions.Item label="位置">
                    {(f.location as string) || '（无）'}
                  </Descriptions.Item>
                  {files.length > 0 && (
                    <Descriptions.Item label="涉及文件">
                      {files.join('、')}
                    </Descriptions.Item>
                  )}
                </Descriptions>
              </>
            )
          })()}
      </Drawer>

      {/* 驳回理由（必填） */}
      <Modal
        title="驳回该错别字标注"
        open={rejectOpen}
        onOk={confirmReject}
        onCancel={() => {
          setRejectOpen(false)
          setRejectReason('')
        }}
        okText="确认驳回"
        okButtonProps={{ danger: true }}
      >
        <p>
          驳回将把该错别字写入过滤规则列表（生效状态），后续审核自动跳过该错字。请填写补充说明：
        </p>
        <Input.TextArea
          rows={4}
          value={rejectReason}
          onChange={(e) => setRejectReason(e.target.value)}
          placeholder="请填写驳回的补充说明（必填）"
        />
      </Modal>

      {/* 停用过滤规则理由（必填） */}
      <Modal
        title="停用过滤规则"
        open={deactOpen}
        onOk={() => {
          const reason = deactReason.trim()
          if (!reason) {
            message.error('停用时必须填写停用补充说明')
            return
          }
          if (deactTarget) void handleDeactivate(deactTarget.id, reason)
        }}
        onCancel={() => {
          setDeactOpen(false)
          setDeactReason('')
          setDeactTarget(null)
        }}
        okText="确认停用"
        okButtonProps={{ danger: true }}
      >
        <p>
          停用后该规则记录仍保留并标记为「已停用」，不再参与审核去噪匹配。请填写停用补充说明：
        </p>
        <Input.TextArea
          rows={4}
          value={deactReason}
          onChange={(e) => setDeactReason(e.target.value)}
          placeholder="请填写停用补充说明（必填）"
        />
      </Modal>
    </div>
  )
}
