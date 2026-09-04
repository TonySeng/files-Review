import { useEffect, useMemo, useState } from 'react'
import {
  Badge,
  Button,
  Card,
  Col,
  Collapse,
  Dropdown,
  Empty,
  Input,
  Modal,
  Progress,
  Radio,
  Row,
  Space,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd'
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  DownloadOutlined,
  ExclamationCircleOutlined,
  FileSearchOutlined,
  LikeOutlined,
  DislikeOutlined,
  QuestionCircleOutlined,
} from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import type {
  ConsistencyIssue,
  FeedbackJudgment,
  Finding,
  FindingStatus,
  KBTrace,
  ReviewSummary,
  RuleResult,
  Severity,
} from '../types'
import { api } from '../services/api'
import SnippetViewer from './SnippetViewer'

const STATUS_META: Record<
  FindingStatus,
  { label: string; color: string; icon: React.ReactNode }
> = {
  pass: { label: '通过', color: 'success', icon: <CheckCircleOutlined /> },
  fail: { label: '不合规', color: 'error', icon: <CloseCircleOutlined /> },
  warn: { label: '存疑', color: 'warning', icon: <ExclamationCircleOutlined /> },
  unknown: { label: '待确认', color: 'default', icon: <QuestionCircleOutlined /> },
}

const SEVERITY_META: Record<Severity, { label: string; color: string }> = {
  critical: { label: '否决项', color: 'red' },
  major: { label: '重要', color: 'orange' },
  minor: { label: '一般', color: 'blue' },
  info: { label: '提示', color: 'default' },
}

const CATEGORY_LABEL: Record<string, string> = {
  qualification: '资格性',
  commercial: '商务',
  technical: '技术',
  format: '格式',
  consistency: '一致性',
  legal: '法规',
}

interface Props {
  findings: Finding[]
  issues: ConsistencyIssue[]
  /** 每规则一条的聚合结果（长文档拆分并行审核后合并去重；20 规则 → 20 条） */
  ruleResults?: RuleResult[]
  kbTraces: KBTrace[]
  summary: ReviewSummary | null
  running: boolean
  taskId?: string | null
  /** 当前任务的文件范围，用于限定"定位原文"检索，避免串到全局历史同名文件 */
  fileIds?: string[]
}

export default function ResultPanel({
  findings,
  issues,
  ruleResults,
  kbTraces,
  summary,
  running,
  taskId,
  fileIds,
}: Props) {
  const [filter, setFilter] = useState<FindingStatus | 'all'>('all')
  const [exporting, setExporting] = useState(false)
  const [locateState, setLocateState] = useState<{
    snippet: string
    location?: string
    candidates?: string[]
  } | null>(null)
  // 采纳/不采纳反馈态：key = rule_id
  const [feedbackByRule, setFeedbackByRule] = useState<
    Record<string, { judgment: FeedbackJudgment; reject_reason?: string | null }>
  >({})
  const [rejecting, setRejecting] = useState<Finding | null>(null)
  const [rejectReason, setRejectReason] = useState('')
  const [submitting, setSubmitting] = useState(false)

  // 切换任务时重置过滤状态，避免上一个任务的 Tab 选择残留到新任务。
  // 注意：同一任务内 SSE 增量更新不会改变 taskId，过滤选择可安全保留。
  useEffect(() => {
    setFilter('all')
    setFeedbackByRule({})
  }, [taskId])

  const giveFeedback = async (f: Finding, judgment: FeedbackJudgment, reason?: string) => {
    try {
      setSubmitting(true)
      await api.submitFeedback({
        task_id: taskId ?? null,
        rule_id: f.rule_id,
        finding: f,
        judgment,
        reject_reason: reason ?? null,
      })
      setFeedbackByRule((prev) => ({
        ...prev,
        [f.rule_id]: { judgment, reject_reason: reason ?? null },
      }))
      message.success(judgment === 'adopt' ? '已标记为采纳' : '已标记为不采纳，并记入训练数据')
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setSubmitting(false)
      setRejecting(null)
      setRejectReason('')
    }
  }

  const filtered = useMemo(
    () => (filter === 'all' ? findings : findings.filter((f) => f.status === filter)),
    [findings, filter],
  )

  const counts = useMemo(() => {
    const base = { pass: 0, fail: 0, warn: 0, unknown: 0 }
    findings.forEach((f) => {
      base[f.status] += 1
    })
    return base
  }, [findings])

  const handleExport = async (format: 'word' | 'pdf') => {
    setExporting(true)
    try {
      const response = await api.exportReport({
        findings,
        consistency_issues: issues,
        kb_traces: kbTraces,
        summary,
        rule_results: ruleResults || [],
        format,
      })

      if (!response.ok) {
        const body = await response.json()
        throw new Error(body.detail || '导出失败')
      }

      const blob = await response.blob()
      const url = window.URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `审核报告_${new Date().toLocaleString('zh-CN').replace(/[/:]/g, '-')}.${format === 'word' ? 'docx' : 'pdf'}`
      document.body.appendChild(a)
      a.click()
      window.URL.revokeObjectURL(url)
      document.body.removeChild(a)

      message.success(`已导出为 ${format === 'word' ? 'Word' : 'PDF'} 文档`)
    } catch (err) {
      message.error(`导出失败：${(err as Error).message}`)
    } finally {
      setExporting(false)
    }
  }

  if (!findings.length && !running) {
    return (
      <Card size="small">
        <Empty
          image={<FileSearchOutlined style={{ fontSize: 48, color: '#c9cdd4' }} />}
          description="上传文件并选择规则后，点击「开始审核」查看结果"
        />
      </Card>
    )
  }

  const columns: ColumnsType<Finding> = [
    {
      title: '状态',
      dataIndex: 'status',
      width: 92,
      // 注意：此处【不能】配置 filters/onFilter 列筛选。
      // 列筛选是非受控的，antd Table 会把筛选值存在内部且不随 dataSource 变化自动清除——
      // 曾导致「点过『不合规』后再切其他 Tab，不合规数据仍残留/置顶」。
      // 结论过滤统一由上方 Radio.Group（受控 filter 状态）负责。
      render: (status: FindingStatus) => (
        <Tag color={STATUS_META[status].color} icon={STATUS_META[status].icon}>
          {STATUS_META[status].label}
        </Tag>
      ),
    },
    {
      title: '规则',
      dataIndex: 'rule_name',
      width: 190,
      render: (name: string, row) => (
        <Space direction="vertical" size={2}>
          <span style={{ fontSize: 13 }}>{name || row.rule_id}</span>
          <Space size={4}>
            <Tag color={SEVERITY_META[row.severity]?.color} style={{ marginRight: 0 }}>
              {SEVERITY_META[row.severity]?.label}
            </Tag>
            <Tag style={{ marginRight: 0 }}>
              {CATEGORY_LABEL[row.category] || row.category}
            </Tag>
          </Space>
        </Space>
      ),
    },
    {
      title: '审核结论',
      dataIndex: 'title',
      render: (title: string, row) => (
        <Space direction="vertical" size={4} style={{ width: '100%' }}>
          <Typography.Text strong={row.status === 'fail'} style={{ fontSize: 13 }}>
            {title || '（无结论）'}
          </Typography.Text>
          {row.detail && (
            <Typography.Text type="secondary" style={{ fontSize: 12.5, lineHeight: 1.75 }}>
              {row.detail}
            </Typography.Text>
          )}
          {row.evidence && (
            <div className="evidence-block">
              <Space size={6}>
                <Typography.Text type="secondary" style={{ fontSize: 11.5 }}>
                  原文依据{row.location ? ` · ${row.location}` : ''}
                </Typography.Text>
                <Button
                  type="link"
                  size="small"
                  icon={<FileSearchOutlined />}
                  style={{ fontSize: 11.5, padding: 0, height: 'auto' }}
                  onClick={() =>
                    setLocateState({
                      snippet: row.evidence,
                      location: row.location,
                      candidates: [row.evidence, row.detail, row.title].filter(
                        (s): s is string => Boolean(s && s.trim()),
                      ),
                    })
                  }
                >
                  定位原文
                </Button>
              </Space>
              <div style={{ marginTop: 3 }}>{row.evidence}</div>
            </div>
          )}
          {row.legal_basis && (
            <div className="evidence-block legal-basis">
              <Typography.Text style={{ fontSize: 11.5, color: '#1668dc' }}>
                法规依据
              </Typography.Text>
              <div style={{ marginTop: 3 }}>{row.legal_basis}</div>
            </div>
          )}
          {row.suggestion && (
            <Typography.Text style={{ fontSize: 12.5, color: '#d4380d' }}>
              整改建议：{row.suggestion}
            </Typography.Text>
          )}
        </Space>
      ),
    },
    {
      title: '反馈',
      key: 'feedback',
      width: 116,
      fixed: 'right',
      render: (_: unknown, row: Finding) => {
        const fb = feedbackByRule[row.rule_id]
        if (fb?.judgment === 'adopt') {
          return (
            <Tag color="success" icon={<LikeOutlined />}>
              已采纳
            </Tag>
          )
        }
        if (fb?.judgment === 'reject') {
          return (
            <Tooltip title={fb.reject_reason ? `不采纳原因：${fb.reject_reason}` : '已不采纳'}>
              <Tag color="error" icon={<DislikeOutlined />}>
                已不采纳
              </Tag>
            </Tooltip>
          )
        }
        return (
          <Space direction="vertical" size={4}>
            <Button
              type="text"
              size="small"
              icon={<LikeOutlined />}
              style={{ color: '#52c41a', padding: 0, height: 'auto' }}
              onClick={() => giveFeedback(row, 'adopt')}
            >
              采纳
            </Button>
            <Button
              type="text"
              size="small"
              icon={<DislikeOutlined />}
              style={{ color: '#ff4d4f', padding: 0, height: 'auto' }}
              onClick={() => {
                setRejecting(row)
                setRejectReason('')
              }}
            >
              不采纳
            </Button>
          </Space>
        )
      },
    },
  ]

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      {summary && (
        <Card
          size="small"
          extra={
            <Dropdown
              menu={{
                items: [
                  {
                    key: 'word',
                    label: '导出为 Word',
                    onClick: () => handleExport('word'),
                  },
                  {
                    key: 'pdf',
                    label: '导出为 PDF',
                    onClick: () => handleExport('pdf'),
                  },
                ],
              }}
            >
              <Button
                type="primary"
                icon={<DownloadOutlined />}
                loading={exporting}
                disabled={running || findings.length === 0}
              >
                导出报告
              </Button>
            </Dropdown>
          }
        >
          <Row gutter={16} align="middle">
            <Col span={5}>
              <div className="stat-card">
                <Progress
                  type="dashboard"
                  size={92}
                  percent={summary.score}
                  // 合规得分不是完成度，需显式渲染分值：antd 在 100% 时默认显示对勾
                  format={() => (
                    <span style={{ fontSize: 22, fontWeight: 600 }}>{summary.score}</span>
                  )}
                  strokeColor={
                    summary.score >= 85 ? '#52c41a' : summary.score >= 60 ? '#faad14' : '#ff4d4f'
                  }
                />
                <div style={{ marginTop: 4, fontSize: 13, fontWeight: 500 }}>
                  {summary.conclusion}
                </div>
              </div>
            </Col>
            <Col span={19}>
              <Row gutter={12}>
                <Col span={4}>
                  <Statistic
                    title="不合规"
                    value={counts.fail}
                    valueStyle={{ color: '#ff4d4f', fontSize: 22 }}
                  />
                </Col>
                <Col span={4}>
                  <Statistic
                    title="否决风险"
                    value={summary.severity_counts?.critical ?? 0}
                    valueStyle={{ color: '#cf1322', fontSize: 22 }}
                  />
                </Col>
                <Col span={4}>
                  <Statistic
                    title="存疑"
                    value={counts.warn}
                    valueStyle={{ color: '#faad14', fontSize: 22 }}
                  />
                </Col>
                <Col span={4}>
                  <Statistic
                    title="待确认"
                    value={counts.unknown}
                    valueStyle={{ color: '#8c8c8c', fontSize: 22 }}
                  />
                </Col>
                <Col span={4}>
                  <Statistic
                    title="通过"
                    value={counts.pass}
                    valueStyle={{ color: '#52c41a', fontSize: 22 }}
                  />
                </Col>
                <Col span={4}>
                  <Statistic
                    title="一致性问题"
                    value={issues.length}
                    valueStyle={{ fontSize: 22 }}
                  />
                </Col>
              </Row>
            </Col>
          </Row>
        </Card>
      )}

      {/* 规则审核结果与审核结论：支持展开/收起，默认全部展开 */}
      <Collapse
        size="small"
        defaultActiveKey={['rule-results', 'conclusions']}
        items={[
          ...(ruleResults && ruleResults.length > 0
            ? [
                {
                  key: 'rule-results',
                  label: `规则审核结果（${ruleResults.length}）`,
                  children: (
                    <Table
                      size="small"
                      rowKey={(r) => r.rule_id}
                      pagination={false}
                      dataSource={ruleResults}
                      columns={[
                        {
                          title: '状态',
                          dataIndex: 'status',
                          width: 92,
                          render: (status: FindingStatus) => (
                            <Tag color={STATUS_META[status].color} icon={STATUS_META[status].icon}>
                              {STATUS_META[status].label}
                            </Tag>
                          ),
                        },
                        {
                          title: '规则',
                          dataIndex: 'rule_name',
                          width: 200,
                          render: (name: string, row) => (
                            <Space direction="vertical" size={2}>
                              <span style={{ fontSize: 13 }}>{name || row.rule_id}</span>
                              <Space size={4}>
                                {row.severity && (
                                  <Tag color={SEVERITY_META[row.severity as Severity]?.color} style={{ marginRight: 0 }}>
                                    {SEVERITY_META[row.severity as Severity]?.label}
                                  </Tag>
                                )}
                                {row.category && <Tag style={{ marginRight: 0 }}>{CATEGORY_LABEL[row.category] || row.category}</Tag>}
                              </Space>
                            </Space>
                          ),
                        },
                        {
                          title: '问题数',
                          dataIndex: 'issue_count',
                          width: 80,
                          align: 'center',
                          render: (n: number) => (
                            <Typography.Text strong={n > 0} type={n > 0 ? 'danger' : 'secondary'}>
                              {n}
                            </Typography.Text>
                          ),
                        },
                        {
                          title: '问题样例',
                          dataIndex: 'samples',
                          render: (samples: string[]) =>
                            samples && samples.length ? (
                              <Space direction="vertical" size={2}>
                                {samples.slice(0, 3).map((s, i) => (
                                  <Typography.Text key={i} type="secondary" style={{ fontSize: 12.5 }}>
                                    · {s}
                                  </Typography.Text>
                                ))}
                              </Space>
                            ) : (
                              <Typography.Text type="secondary" style={{ fontSize: 12.5 }}>
                                无问题
                              </Typography.Text>
                            ),
                        },
                      ]}
                    />
                  ),
                },
              ]
            : []),
          {
            key: 'conclusions',
            label: `审核结论（${filtered.length}）`,
            extra: (
              <span onClick={(e) => e.stopPropagation()}>
                <Radio.Group
                  size="small"
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                  options={[
                    { value: 'all', label: `全部 ${findings.length}` },
                    { value: 'fail', label: `不合规 ${counts.fail}` },
                    { value: 'warn', label: `存疑 ${counts.warn}` },
                    { value: 'unknown', label: `待确认 ${counts.unknown}` },
                    { value: 'pass', label: `通过 ${counts.pass}` },
                  ]}
                  optionType="button"
                />
              </span>
            ),
            children: (
              <Table
                size="small"
                rowKey={(r) => r.rule_id}
                columns={columns}
                dataSource={filtered}
                pagination={false}
                loading={running && !findings.length}
              />
            ),
          },
        ]}
      />

      {issues.length > 0 && (
        <Card size="small" title={`跨文件一致性问题（${issues.length}）`}>
          <Collapse
            size="small"
            items={issues.map((issue, idx) => ({
              key: String(idx),
              label: (
                <Space size={6}>
                  <Tag color={SEVERITY_META[issue.severity]?.color}>
                    {SEVERITY_META[issue.severity]?.label}
                  </Tag>
                  <span style={{ fontSize: 13 }}>{issue.field}</span>
                </Space>
              ),
              children: (
                <Space direction="vertical" size={8} style={{ width: '100%' }}>
                  <Typography.Text style={{ fontSize: 13 }}>
                    {issue.description}
                  </Typography.Text>
                  <Table
                    size="small"
                    pagination={false}
                    rowKey={(_, i) => String(i)}
                    dataSource={issue.values}
                    columns={[
                      { title: '文件', dataIndex: 'file', width: 200 },
                      { title: '位置', dataIndex: 'location', width: 160 },
                      { title: '取值', dataIndex: 'value' },
                    ]}
                  />
                  {issue.suggestion && (
                    <Typography.Text style={{ fontSize: 12.5, color: '#d4380d' }}>
                      修正建议：{issue.suggestion}
                    </Typography.Text>
                  )}
                </Space>
              ),
            }))}
          />
        </Card>
      )}

      {kbTraces.length > 0 && (
        <Card
          size="small"
          title={
            <Space size={6}>
              <span>知识库检索记录</span>
              <Badge count={kbTraces.length} style={{ backgroundColor: '#1668dc' }} />
            </Space>
          }
        >
          <Collapse
            size="small"
            ghost
            items={kbTraces.map((trace, idx) => ({
              key: String(idx),
              label: <span style={{ fontSize: 13 }}>{trace.query}</span>,
              children: (
                <Space direction="vertical" size={6} style={{ width: '100%' }}>
                  {trace.reason && (
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      检索原因：{trace.reason}
                    </Typography.Text>
                  )}
                  <div className="evidence-block legal-basis">{trace.answer}</div>
                  {trace.sources.length > 0 && (
                    <div>
                      <Typography.Text type="secondary" style={{ fontSize: 11.5 }}>
                        引用来源（{trace.sources.length}）
                      </Typography.Text>
                      <Collapse
                        size="small"
                        ghost
                        items={trace.sources.map((s, i) => ({
                          key: String(i),
                          label: (
                            <Space size={4} wrap>
                              <Tag style={{ marginInlineEnd: 0 }}>
                                {s.source_file || '未知来源'}
                                {s.page != null ? ` p.${s.page}` : ''}
                              </Tag>
                              {s.score != null && (
                                <Typography.Text
                                  type="secondary"
                                  style={{ fontSize: 11.5 }}
                                >
                                  相关度 {Number(s.score).toFixed(2)}
                                </Typography.Text>
                              )}
                            </Space>
                          ),
                          children: s.text ? (
                            <div
                              style={{
                                background: '#fafafa',
                                border: '1px solid #f0f0f0',
                                borderRadius: 4,
                                padding: '8px 10px',
                                fontSize: 12.5,
                                lineHeight: 1.85,
                                whiteSpace: 'pre-wrap',
                                wordBreak: 'break-word',
                              }}
                            >
                              {s.text}
                            </div>
                          ) : (
                            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                              该来源未返回文本片段
                            </Typography.Text>
                          ),
                        }))}
                      />
                    </div>
                  )}
                </Space>
              ),
            }))}
          />
        </Card>
      )}

      <SnippetViewer
        open={!!locateState}
        snippet={locateState?.snippet || ''}
        location={locateState?.location}
        candidates={locateState?.candidates}
        fileIds={fileIds}
        onClose={() => setLocateState(null)}
      />

      <Modal
        title="标记为不采纳"
        open={!!rejecting}
        okText="确认不采纳"
        okButtonProps={{ disabled: !rejectReason.trim(), loading: submitting }}
        cancelText="取消"
        onOk={() => rejecting && giveFeedback(rejecting, 'reject', rejectReason.trim())}
        onCancel={() => {
          setRejecting(null)
          setRejectReason('')
        }}
        destroyOnClose
      >
        {rejecting && (
          <Space direction="vertical" size={10} style={{ width: '100%' }}>
            <Typography.Text type="secondary" style={{ fontSize: 13 }}>
              「{rejecting.rule_name}」：{rejecting.title || '（无结论）'}
              {rejecting.typo && (
                <span>
                  {' '}
                  · 错别字「{rejecting.typo.wrong}」应为「{rejecting.typo.correct}」
                </span>
              )}
            </Typography.Text>
            <Typography.Text style={{ fontSize: 13 }}>
              请填写不采纳原因（必填，将作为模型调优训练数据记录）：
            </Typography.Text>
            <Input.TextArea
              rows={4}
              autoFocus
              value={rejectReason}
              onChange={(e) => setRejectReason(e.target.value)}
              placeholder="例如：该处并非错别字，属专业术语/约定写法；或识别上下文有误"
            />
          </Space>
        )}
      </Modal>
    </Space>
  )
}
