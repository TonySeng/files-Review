import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Badge,
  Button,
  Card,
  Col,
  Drawer,
  Empty,
  Input,
  InputNumber,
  Modal,
  Row,
  Segmented,
  Space,
  Spin,
  Statistic,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd'
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  DatabaseOutlined,
  FileTextOutlined,
  HistoryOutlined,
  SafetyCertificateOutlined,
  SafetyOutlined,
  SyncOutlined,
  TagOutlined,
  RocketOutlined,
} from '@ant-design/icons'
import { api } from '../services/api'
import type {
  AuditConsistencyResult,
  AuditRecord,
  ReviewDataDecision,
  ReviewDataRecord,
  ReviewDataStats,
  ReviewDataVerifyResult,
} from '../types'

const { Text, Paragraph, Title } = Typography

type JudgmentFilter = 'all' | 'adopt' | 'reject' | 'mixed'

/** 版本标签颜色 */
const versionTagColor = (v: string) =>
  v.includes('CUSTOM') ? 'purple' : v.includes('GROUP') ? 'geekblue' : 'blue'

export default function HistoricalDataPage({
  onBack,
  isAdmin = false,
}: {
  onBack: () => void
  isAdmin?: boolean
}) {
  const [stats, setStats] = useState<ReviewDataStats | null>(null)
  const [records, setRecords] = useState<ReviewDataRecord[]>([])
  const [total, setTotal] = useState(0)
  const [q, setQ] = useState('')
  const [judgment, setJudgment] = useState<JudgmentFilter>('all')
  const [loading, setLoading] = useState(false)
  const [page, setPage] = useState(1)

  const [detail, setDetail] = useState<ReviewDataRecord | null>(null)
  const [detailOpen, setDetailOpen] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [verify, setVerify] = useState<ReviewDataVerifyResult | null>(null)

  const [editing, setEditing] = useState<ReviewDataDecision | null>(null)
  const [editJudgment, setEditJudgment] = useState<'adopt' | 'reject'>('adopt')
  const [editReason, setEditReason] = useState('')
  const [submitting, setSubmitting] = useState(false)

  // 审计存证 Tab
  const [audits, setAudits] = useState<AuditRecord[]>([])
  const [auditTotal, setAuditTotal] = useState(0)
  const [auditLoading, setAuditLoading] = useState(false)
  const [auditPage, setAuditPage] = useState(1)

  // 一致性监控 Tab
  const [consistency, setConsistency] = useState<AuditConsistencyResult | null>(null)
  const [consLoading, setConsLoading] = useState(false)
  const [consSample, setConsSample] = useState(5)

  const pageSize = 20

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [s, r] = await Promise.all([
        api.reviewDataStats(),
        api.listReviewData({
          q: q || undefined,
          judgment: judgment === 'all' ? undefined : (judgment as 'adopt' | 'reject' | 'mixed'),
          limit: pageSize,
          offset: (page - 1) * pageSize,
        }),
      ])
      setStats(s)
      setRecords(r.records)
      setTotal(r.total)
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setLoading(false)
    }
  }, [q, judgment, page])

  const loadAudits = useCallback(async () => {
    setAuditLoading(true)
    try {
      const r = await api.listAudit({ limit: pageSize, offset: (auditPage - 1) * pageSize })
      setAudits(r.records)
      setAuditTotal(r.total)
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setAuditLoading(false)
    }
  }, [auditPage])

  const loadConsistency = useCallback(async () => {
    setConsLoading(true)
    try {
      const r = await api.auditConsistency(consSample)
      setConsistency(r)
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setConsLoading(false)
    }
  }, [consSample])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (isAdmin && (auditPage > 1 || audits.length === 0)) void loadAudits()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const openDetail = useCallback(async (id: string) => {
    setDetailOpen(true)
    setDetailLoading(true)
    setVerify(null)
    try {
      const rec = await api.getReviewData(id)
      setDetail(rec)
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setDetailLoading(false)
    }
  }, [])

  const runVerify = useCallback(async (id: string) => {
    try {
      const res = await api.verifyReviewData(id)
      setVerify(res)
      if (res.healthy) message.success('一致性校验通过：全部历史判定对应规则仍然有效')
      else message.warning(`发现 ${res.stale} 条历史判定对应规则已失效，请关注`)
    } catch (err) {
      message.error((err as Error).message)
    }
  }, [])

  const openEdit = useCallback((d: ReviewDataDecision) => {
    setEditing(d)
    setEditJudgment(d.historical_judgment === 'reject' ? 'reject' : 'adopt')
    setEditReason(d.reject_reason || '')
  }, [])

  const submitEdit = useCallback(async () => {
    if (!editing || !detail) return
    if (editJudgment === 'reject' && !editReason.trim()) {
      message.error('不采纳必须填写原因')
      return
    }
    setSubmitting(true)
    try {
      await api.updateReviewDataDecision(detail.id, editing.id, {
        judgment: editJudgment,
        reject_reason: editJudgment === 'reject' ? editReason.trim() : null,
      })
      message.success('已更新历史判定，回流结果将随之生效')
      setEditing(null)
      await Promise.all([openDetail(detail.id), load()])
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setSubmitting(false)
    }
  }, [editing, detail, editJudgment, editReason, openDetail, load])

  const jTag = (j: string | null) => {
    if (j === 'adopt') return <Tag color="success" icon={<CheckCircleOutlined />}>已采纳</Tag>
    if (j === 'reject') return <Tag color="error" icon={<CloseCircleOutlined />}>已不采纳</Tag>
    return <Tag>未判定</Tag>
  }

  const detTag = (on: boolean) => (
    on ? (
      <Tag color="cyan" icon={<SafetyOutlined />}>确定性</Tag>
    ) : (
      <Tag>常规</Tag>
    )
  )

  // 统计概览卡
  const StatCards = () => (
    <Row gutter={[12, 12]}>
      <Col xs={12} sm={6} md={6}>
        <Card size="small" bordered={false} style={{ background: '#eef4ff', borderRadius: 12 }}>
          <Statistic title="关联记录" value={stats?.records ?? 0} prefix={<DatabaseOutlined />} />
          <Text type="secondary" style={{ fontSize: 12 }}>文件 + 规则 关联档案</Text>
        </Card>
      </Col>
      <Col xs={12} sm={6} md={6}>
        <Card size="small" bordered={false} style={{ background: '#f6ffed', borderRadius: 12 }}>
          <Statistic title="已采纳" value={stats?.adopted ?? 0} valueStyle={{ color: '#52c41a' }} />
          <Text type="secondary" style={{ fontSize: 12 }}>历史判定 · 采纳</Text>
        </Card>
      </Col>
      <Col xs={12} sm={6} md={6}>
        <Card size="small" bordered={false} style={{ background: '#fff1f0', borderRadius: 12 }}>
          <Statistic title="已不采纳" value={stats?.rejected ?? 0} valueStyle={{ color: '#ff4d4f' }} />
          <Text type="secondary" style={{ fontSize: 12 }}>历史判定 · 不采纳</Text>
        </Card>
      </Col>
      <Col xs={12} sm={6} md={6}>
        <Card size="small" bordered={false} style={{ background: '#f9f0ff', borderRadius: 12 }}>
          <Statistic title="审计存证" value={stats?.audits ?? 0} prefix={<HistoryOutlined />} />
          <Text type="secondary" style={{ fontSize: 12 }}>版本化存证记录</Text>
        </Card>
      </Col>
    </Row>
  )

  const recordColumns = [
    {
      title: '关联文件',
      dataIndex: 'file_names',
      render: (names: string[], r: ReviewDataRecord) => (
        <Space direction="vertical" size={2}>
          {(names || []).map((n, i) => (
            <Tooltip key={i} title={`MD5: ${r.file_md5s?.[i] || ''}`}>
              <Text ellipsis style={{ maxWidth: 220, fontSize: 13 }}>
                <FileTextOutlined style={{ color: '#8f959e', marginRight: 4 }} />
                {n}
              </Text>
            </Tooltip>
          ))}
        </Space>
      ),
    },
    {
      title: '审核规则',
      dataIndex: 'rule_names',
      render: (names: string[]) => (
        <Tooltip title={(names || []).join('、')}>
          <Text type="secondary" ellipsis style={{ maxWidth: 180, fontSize: 13 }}>
            {(names || []).slice(0, 3).join('、')}
            {(names || []).length > 3 ? ` 等${names.length}项` : ''}
          </Text>
        </Tooltip>
      ),
    },
    {
      title: '结论',
      key: 'verdict',
      render: (_: unknown, r: ReviewDataRecord) => (
        <Space size={4}>
          <Tag color="success" style={{ marginRight: 0 }}>采纳 {r.adopt_count}</Tag>
          <Tag color="error" style={{ marginRight: 0 }}>不采纳 {r.reject_count}</Tag>
        </Space>
      ),
    },
    { title: '结论数', dataIndex: 'finding_count', width: 80, render: (v: number) => <Text type="secondary">{v}</Text> },
    {
      title: '最近任务',
      dataIndex: 'task_id',
      width: 120,
      render: (v: string) => (
        <Tooltip title={v}>
          <Text code style={{ fontSize: 12 }}>{v?.slice(0, 8)}</Text>
        </Tooltip>
      ),
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      width: 160,
      render: (v: string) => <Text type="secondary" style={{ fontSize: 12 }}>{v?.replace('T', ' ')}</Text>,
    },
    {
      title: '操作',
      key: 'act',
      width: 140,
      render: (_: unknown, r: ReviewDataRecord) => (
        <Space size={4}>
          <Button size="small" onClick={() => openDetail(r.id)}>查看</Button>
          <Button size="small" icon={<SafetyCertificateOutlined />} onClick={() => runVerify(r.id)}>
            校验
          </Button>
        </Space>
      ),
    },
  ]

  const auditColumns = [
    {
      title: '规则集版本',
      dataIndex: 'rule_set_version',
      render: (v: string) => <Tag color={versionTagColor(v)} icon={<TagOutlined />}>{v}</Tag>,
    },
    {
      title: '引擎版本',
      dataIndex: 'engine_version',
      render: (v: string) => <Tag>{v}</Tag>,
    },
    {
      title: '数据快照',
      dataIndex: 'data_snapshot_version',
      render: (v: string) => <Tag color="gold">{v}</Tag>,
    },
    {
      title: '解析器',
      dataIndex: 'parser_version',
      ellipsis: true,
      render: (v: string) => <Text type="secondary" style={{ fontSize: 12 }}>{v}</Text>,
    },
    {
      title: '确定性',
      dataIndex: 'deterministic',
      width: 90,
      render: (v: boolean) => detTag(!!v),
    },
    {
      title: '环境',
      dataIndex: 'environment',
      width: 160,
      render: (env: AuditRecord['environment']) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {env?.encoding} · {env?.timezone} · {env?.locale}
        </Text>
      ),
    },
    {
      title: '关联任务',
      dataIndex: 'task_id',
      width: 110,
      render: (v: string | null) =>
        v ? (
          <Tooltip title={v}>
            <Text code style={{ fontSize: 12 }}>{v.slice(0, 8)}</Text>
          </Tooltip>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
    {
      title: '存证时间',
      dataIndex: 'created_at',
      width: 160,
      render: (v: string) => <Text type="secondary" style={{ fontSize: 12 }}>{v?.replace('T', ' ')}</Text>,
    },
  ]

  return (
    <div className="rm-page">
      {/* 顶部栏 */}
      <div className="rm-topbar">
        <Space>
          <Button className="rm-back" icon={<DatabaseOutlined />} onClick={onBack}>
            返回
          </Button>
          <div className="rm-title-wrap">
            <div className="rm-title">
              <span className="rm-title-icon"><DatabaseOutlined /></span>
              历史审核数据管理
            </div>
            <div className="rm-subtitle">
              按「文件(MD5) + 审核规则」关联保留全部历史结果 · 版本化一切 · 确定性执行 · 可回放追溯
            </div>
          </div>
        </Space>
        <Tag color="cyan" icon={<SafetyOutlined />} style={{ fontSize: 13, padding: '4px 10px', marginLeft: 'auto' }}>
          确定性引擎运行中
        </Tag>
      </div>

      {/* 概览卡 */}
      {stats && (
        <div className="rm-headcard" style={{ marginBottom: 16 }}>
          <Row gutter={[16, 16]} align="middle">
            <Col xs={24} md={10}>
              <StatCards />
            </Col>
            <Col xs={24} md={14}>
              <Alert
                type="info"
                showIcon
                message="回流与一致性"
                description="相同「文件 + 规则」组合再次发起审核时，下方历史判定将自动回流至模型，确保输出与历史处理一致；每次审核均存证版本清单，支持按历史版本重跑复现。"
              />
            </Col>
          </Row>
        </div>
      )}

      <Tabs
        defaultActiveKey="records"
        items={[
          {
            key: 'records',
            label: (
              <span>
                <DatabaseOutlined /> 关联记录
                {total ? <Badge count={total} style={{ marginLeft: 6 }} /> : null}
              </span>
            ),
            children: (
              <>
                <div className="rm-filterbar" style={{ marginBottom: 12 }}>
                  <Segmented
                    value={judgment}
                    onChange={(v) => {
                      setPage(1)
                      setJudgment(v as JudgmentFilter)
                    }}
                    options={[
                      { label: '全部', value: 'all' },
                      { label: '全部采纳', value: 'adopt' },
                      { label: '含不采纳', value: 'reject' },
                      { label: '混合', value: 'mixed' },
                    ]}
                  />
                  <div className="rm-filterbar-right">
                    <Input.Search
                      className="rm-search"
                      placeholder="搜索文件名 / 规则名 / 任务ID"
                      allowClear
                      onSearch={(v) => {
                        setPage(1)
                        setQ(v)
                      }}
                    />
                    <Button icon={<SyncOutlined />} onClick={() => void load()}>刷新</Button>
                  </div>
                </div>
                <Card bordered={false} style={{ borderRadius: 12, boxShadow: 'var(--rm-shadow-sm)' }}>
                  <Spin spinning={loading}>
                    <Table<ReviewDataRecord>
                      rowKey="id"
                      dataSource={records}
                      pagination={{
                        current: page,
                        pageSize,
                        total,
                        onChange: (p) => setPage(p),
                      }}
                      columns={recordColumns}
                      locale={{ emptyText: <Empty description="暂无历史审核关联记录。完成一次审核后将自动归档。" /> }}
                    />
                  </Spin>
                </Card>
              </>
            ),
          },
          ...(isAdmin
            ? [
                {
                  key: 'audit',
            label: (
              <span>
                <HistoryOutlined /> 审计存证
                {auditTotal ? <Badge count={auditTotal} style={{ marginLeft: 6 }} /> : null}
              </span>
            ),
            children: (
              <Card bordered={false} style={{ borderRadius: 12, boxShadow: 'var(--rm-shadow-sm)' }}>
                <Spin spinning={auditLoading}>
                  <Table<AuditRecord>
                    rowKey="id"
                    size="middle"
                    dataSource={audits}
                    pagination={{
                      current: auditPage,
                      pageSize,
                      total: auditTotal,
                      onChange: (p) => {
                        setAuditPage(p)
                        void loadAudits()
                      },
                    }}
                    columns={auditColumns}
                    locale={{ emptyText: <Empty description="暂无审计存证。完成一次审核后将自动存证版本清单。" /> }}
                  />
                </Spin>
              </Card>
            ),
          },
          {
            key: 'consistency',
            label: (
              <span>
                <SafetyOutlined /> 一致性监控
              </span>
            ),
            children: (
              <Space direction="vertical" style={{ width: '100%' }} size="middle">
                <Card bordered={false} style={{ borderRadius: 12, boxShadow: 'var(--rm-shadow-sm)' }}>
                  <Space wrap style={{ marginBottom: 12 }}>
                    <Text type="secondary">
                      抽取最近若干已审任务，用相同版本基线重跑并对比结构化结果差异，监控多次审核一致性。
                    </Text>
                    <InputNumber
                      min={1}
                      max={50}
                      value={consSample}
                      onChange={(v) => setConsSample(v ?? 5)}
                      addonAfter="条"
                    />
                    <Button
                      type="primary"
                      icon={<SyncOutlined />}
                      loading={consLoading}
                      onClick={() => void loadConsistency()}
                    >
                      运行监控
                    </Button>
                  </Space>
                  {consistency && (
                    <>
                      <Row gutter={16} align="middle">
                        <Col>
                          <Statistic
                            title="不一致率"
                            value={consistency.inconsistency_rate * 100}
                            precision={2}
                            suffix="%"
                            valueStyle={{
                              color: consistency.inconsistency_rate < 0.001 ? '#52c41a' : '#ff4d4f',
                              fontSize: 28,
                            }}
                          />
                        </Col>
                        <Col>
                          <Space direction="vertical" size={2}>
                            <Text type="secondary">抽样 {consistency.sampled} 条</Text>
                            <Text type="secondary">不一致 {consistency.inconsistent} 条</Text>
                            <Text type="secondary">目标 &lt; 0.1%</Text>
                          </Space>
                        </Col>
                      </Row>
                      <Table
                        rowKey="task_id"
                        size="small"
                        style={{ marginTop: 12 }}
                        pagination={false}
                        dataSource={consistency.items}
                        columns={[
                          {
                            title: '任务',
                            dataIndex: 'task_id',
                            render: (v: string | null) =>
                              v ? <Text code>{v.slice(0, 8)}</Text> : <Text type="secondary">—</Text>,
                          },
                          { title: '规则集版本', dataIndex: 'rule_set_version', render: (v: string) => <Tag color={versionTagColor(v)}>{v}</Tag> },
                          { title: '引擎', dataIndex: 'engine_version', render: (v: string) => <Tag>{v}</Tag> },
                          { title: '数据快照', dataIndex: 'data_snapshot_version', render: (v: string) => <Tag color="gold">{v}</Tag> },
                          { title: '确定性', dataIndex: 'deterministic', width: 90, render: (v: boolean) => detTag(!!v) },
                          {
                            title: '可重放',
                            dataIndex: 'replay_ready',
                            width: 90,
                            render: (v: boolean) =>
                              v ? <Tag color="success" icon={<RocketOutlined />}>就绪</Tag> : <Tag color="error">缺失</Tag>,
                          },
                          {
                            title: '差异率',
                            dataIndex: 'diff_rate',
                            width: 90,
                            render: (v: number) => (
                              <Text style={{ color: v > 0.001 ? '#ff4d4f' : '#52c41a' }}>
                                {(v * 100).toFixed(2)}%
                              </Text>
                            ),
                          },
                          { title: '说明', dataIndex: 'note', ellipsis: true },
                        ]}
                      />
                    </>
                  )}
                </Card>
              </Space>
            ),
                },
              ]
            : []),
        ]}
      />

      <Drawer
        title="关联记录详情"
        width={820}
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        styles={{ body: { paddingBottom: 24 } }}
      >
        {detailLoading ? (
          <div className="rm-loading"><Spin /></div>
        ) : detail ? (
          <Space direction="vertical" style={{ width: '100%' }} size="middle">
            <Alert
              type="info"
              showIcon
              message="相同「文件 + 规则」组合再次发起审核时，下方历史判定将自动回流至模型，确保输出与历史处理一致。"
            />

            {/* 概览 */}
            <Card size="small" title="概览" bordered={false} style={{ borderRadius: 10 }}>
              <Row gutter={[16, 8]}>
                <Col span={8}><Statistic title="采纳" value={detail.adopt_count} valueStyle={{ color: '#52c41a' }} /></Col>
                <Col span={8}><Statistic title="不采纳" value={detail.reject_count} valueStyle={{ color: '#ff4d4f' }} /></Col>
                <Col span={8}><Statistic title="结论数" value={detail.finding_count} /></Col>
              </Row>
              <div style={{ marginTop: 10 }}>
                <Text type="secondary">文件组指纹：</Text>
                <Text code style={{ fontSize: 12 }}>{detail.file_group_fp}</Text>
                <Text type="secondary" style={{ marginLeft: 12 }}>规则组指纹：</Text>
                <Text code style={{ fontSize: 12 }}>{detail.rule_group_fp}</Text>
              </div>
            </Card>

            {/* 版本存证快照 */}
            <Card size="small" title="版本存证快照" bordered={false} style={{ borderRadius: 10, background: '#fafcff' }}>
              <Space wrap>
                <Tag color="blue">规则集 RULE_SET</Tag>
                <Tag color="gold">数据快照 KB</Tag>
                <Tag color="cyan">确定性执行</Tag>
                <Tag>引擎版本锁定</Tag>
              </Space>
              <Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0, fontSize: 12 }}>
                每次审核均按「文件解析快照 + 规则版本 + 引擎版本 + 依赖数据快照 + 环境常量」存证，
                支持按历史版本重跑复现相同结果。
              </Paragraph>
            </Card>

            {/* 关联文件 */}
            <div>
              <Title level={5} style={{ marginBottom: 8 }}>关联文件（{detail.file_md5s.length}）</Title>
              <Space direction="vertical" style={{ width: '100%' }} size={8}>
                {(detail.files && detail.files.length
                  ? detail.files
                  : detail.file_md5s.map((m, i) => ({
                      md5: m,
                      filename: detail.file_names[i] || m,
                      role: '',
                    }))
                ).map((f, i) => (
                  <Card key={i} size="small" bordered={false} style={{ background: '#fafafa', borderRadius: 8 }}>
                    <Space>
                      <FileTextOutlined />
                      <Text strong>{f.filename}</Text>
                      {f.role ? <Tag>{f.role}</Tag> : null}
                      <Tooltip title={f.md5}>
                        <Text type="secondary" code style={{ fontSize: 12 }}>
                          MD5 {f.md5.slice(0, 16)}
                        </Text>
                      </Tooltip>
                    </Space>
                  </Card>
                ))}
              </Space>
            </div>

            {/* 审核规则 */}
            <div>
              <Title level={5} style={{ marginBottom: 8 }}>审核规则（{detail.rule_ids.length} 项）</Title>
              <Paragraph type="secondary">{detail.rule_names.join('、')}</Paragraph>
            </div>

            {/* 一致性校验结果 */}
            {verify && (
              <Alert
                type={verify.healthy ? 'success' : 'warning'}
                showIcon
                message={
                  verify.healthy
                    ? '一致性校验通过'
                    : `发现 ${verify.stale} 条历史判定对应规则已失效（规则被移除或不存在）`
                }
              />
            )}

            {/* 历史判定 */}
            <div>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
                <Title level={5} style={{ margin: 0 }}>历史判定（{detail.decisions?.length || 0}）</Title>
                <Button size="small" icon={<SafetyCertificateOutlined />} onClick={() => runVerify(detail.id)}>
                  一致性校验
                </Button>
              </div>
              <Table<ReviewDataDecision>
                rowKey="id"
                size="small"
                pagination={false}
                dataSource={detail.decisions || []}
                columns={[
                  {
                    title: '规则 / 结论项',
                    dataIndex: 'rule_name',
                    render: (v, r) => (
                      <Space direction="vertical" size={0}>
                        <Text strong>{v}</Text>
                        <Text type="secondary" ellipsis style={{ maxWidth: 260 }}>
                          {r.field || r.title}
                        </Text>
                      </Space>
                    ),
                  },
                  {
                    title: '历史判定',
                    key: 'j',
                    width: 100,
                    render: (_, r) => jTag(r.historical_judgment),
                  },
                  {
                    title: '不采纳原因',
                    dataIndex: 'reject_reason',
                    width: 160,
                    render: (v) => (v ? <Text type="danger">{v}</Text> : <Text type="secondary">—</Text>),
                  },
                  {
                    title: '操作',
                    key: 'a',
                    width: 80,
                    render: (_, r) => (
                      <Button size="small" onClick={() => openEdit(r)}>
                        维护
                      </Button>
                    ),
                  },
                ]}
              />
            </div>
          </Space>
        ) : null}
      </Drawer>

      <Modal
        title="维护历史判定（影响回流结论）"
        open={!!editing}
        onCancel={() => setEditing(null)}
        onOk={submitEdit}
        confirmLoading={submitting}
        okText="保存"
      >
        {editing && (
          <Space direction="vertical" style={{ width: '100%' }} size="middle">
            <div>
              <Text strong>{editing.rule_name}</Text>
              <div>
                <Text type="secondary">{editing.field || editing.title}</Text>
              </div>
            </div>
            <Segmented
              block
              value={editJudgment}
              onChange={(v) => setEditJudgment(v as 'adopt' | 'reject')}
              options={[
                { label: '采纳', value: 'adopt' },
                { label: '不采纳', value: 'reject' },
              ]}
            />
            {editJudgment === 'reject' && (
              <Input.TextArea
                rows={3}
                placeholder="请填写不采纳原因（必填，将随回流注入模型）"
                value={editReason}
                onChange={(e) => setEditReason(e.target.value)}
              />
            )}
          </Space>
        )}
      </Modal>
    </div>
  )
}
