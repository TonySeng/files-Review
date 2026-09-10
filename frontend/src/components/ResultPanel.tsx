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
  FindingLocation,
  FindingStatus,
  KBTrace,
  ReviewSummary,
  RuleFileResult,
  RuleResult,
  Severity,
} from '../types'
import { api } from '../services/api'
import SourcePreviewModal from './SourcePreviewModal'

/** evidence 占位词：参与检索只会随机命中无关单字（如正文里的「无」），不给定位入口 */
const PLACEHOLDER_EVIDENCE =
  /^(无|暂无|没有|没有发现|未找到|未提供|未见|无原文|无依据|无相关依据|不适用|n\/a|na|none|-|—|\/)$/i

/** 该结论是否具备「定位原文」入口：
 * 1) 结构化 locations 中存在真正命中的条目（新任务，后端只返回命中项）；
 * 2) 无 locations 字段的历史任务但有真实 evidence 摘录（≥6 字且非占位词，
 *    走统一回退：三级定位→原始文件预览）。
 * 完整性等不涉及原文定位的结论（locations 为空/无命中且无 evidence）不展示按钮；
 * 「通过」结论不参与原文定位（未发现违规没有位置可言，evidence 也可能只是「无」）。 */
function hasLocateEntry(f: {
  status?: string
  locations?: FindingLocation[]
  evidence?: string
}): boolean {
  if (f.status === 'pass') return false
  if (f.locations?.some((l) => l.file_id && l.matched)) return true
  // 新任务：locations 字段存在（含空数组）说明后端已判定不可定位，不再回退
  if (f.locations) return false
  const ev = (f.evidence || '').trim()
  return ev.length >= 6 && !PLACEHOLDER_EVIDENCE.test(ev)
}

const STATUS_META: Record<
  FindingStatus,
  { label: string; color: string; icon: React.ReactNode }
> = {
  pass: { label: '无风险', color: 'success', icon: <CheckCircleOutlined /> },
  fail: { label: '有风险', color: 'error', icon: <CloseCircleOutlined /> },
  warn: { label: '待复核', color: 'warning', icon: <ExclamationCircleOutlined /> },
  unknown: { label: '待复核', color: 'default', icon: <QuestionCircleOutlined /> },
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
  // 原始文件预览（PDF 跳页高亮 / Word / Excel 网页渲染 + 自动定位）
  const [srcLoc, setSrcLoc] = useState<FindingLocation | null>(null)
  /** 结论定位入口：统一走原始文件预览（所有类型一致），历史任务无 locations 时
   *  先经后端三级定位换算出 file_id/页码/片段，再打开预览——不再使用旧的
   *  「提取文本片段查看」模式。 */
  const openLocate = (f: {
    status?: string
    locations?: FindingLocation[]
    evidence?: string
    detail?: string
    title?: string
    location?: string
  }) => {
    // 「通过」结论不执行文件预览与定位展示（与后端口径一致：pass ⇒ 无 locations）
    if (f.status === 'pass') return
    const target =
      f.locations?.find((l) => l.file_id && l.matched) || null
    if (target) {
      setSrcLoc(target)
      return
    }
    const snippet = f.evidence || ''
    // 回退检索门禁：evidence 是「无」类占位词或过短摘录时不发检索请求
    //（单字/短串会在全文随机命中无关位置），直接提示不可定位
    const evTrim = snippet.trim()
    if (f.locations || evTrim.length < 6 || PLACEHOLDER_EVIDENCE.test(evTrim)) {
      message.warning('该结论未提供可核验的原文依据，无法定位原文')
      return
    }
    if (!snippet.trim()) return
    const candidates = [f.evidence, f.detail, f.title].filter(
      (s): s is string => Boolean(s && s.trim()),
    )
    void (async () => {
      try {
        const res = await api.locateSnippet(
          snippet,
          fileIds,
          240,
          f.location,
          candidates,
        )
        const m = res.matches?.[0]
        if (!m) {
          message.warning('未能在原文中定位到该结论')
          return
        }
        // 从定位描述（如「第3页」）解析页码供 PDF 跳页
        const pmt = /第\s*(\d+)\s*页/.exec(m.location || '')
        setSrcLoc({
          file_id: m.file_id,
          filename: m.filename,
          ext: m.filename.split('.').pop()?.toLowerCase() || null,
          page: pmt ? Number(pmt[1]) : null,
          page_label: pmt ? `第${pmt[1]}页` : null,
          page_count: null,
          char_start: m.start,
          char_end: m.end,
          snippet: m.matched,
          matched: true,
          match_type: m.match_type,
        })
      } catch (err) {
        message.error(err instanceof Error ? err.message : '原文定位失败')
      }
    })()
  }
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

  // 为每条结论生成「稳定且唯一」的行键。同一规则可能产出多条结论（例如一条规则触发多个
  // 检查点、或 auto_match 从多个规则集拉入同名规则），findings 中会存在相同 rule_id 的多条记录。
  // 若直接用 rule_id 作 Table 的 rowKey，会产生重复的 React key；antd 在切换过滤条件复用行节点时
  // 会按 key 错误复用，表现为「筛出错误项」「切回全部后顺序与初始不一致」。这里以
  // 「rule_id + 原始序号」组成稳定键：过滤/切回全部时，同一条结论始终对应同一个 key，
  // 显示顺序始终与后端返回（原始）顺序一致。
  const keyedFindings = useMemo(
    () => findings.map((f, i) => ({ ...f, _key: `${f.rule_id}#${i}` })),
    [findings],
  )

  const filtered = useMemo(
    () =>
      filter === 'all'
        ? keyedFindings
        : keyedFindings.filter((f) => f.status === filter),
    [keyedFindings, filter],
  )

  const counts = useMemo(() => {
    const base = { pass: 0, fail: 0, warn: 0, unknown: 0 }
    findings.forEach((f) => {
      base[f.status] += 1
    })
    return base
  }, [findings])

  // 规则审核结果同样可能含重复 rule_id（跨规则集同名规则），用稳定键避免行键冲突。
  const keyedRuleResults = useMemo(
    () => (ruleResults || []).map((r, i) => ({ ...r, _key: `rr-${r.rule_id}#${i}` })),
    [ruleResults],
  )

  const FILE_STATUS_ORDER: Record<FindingStatus, number> = {
    fail: 0,
    warn: 1,
    unknown: 2,
    pass: 3,
  }

  // 规则维度下钻：取该规则的文件级结论明细。
  // 优先用后端聚合返回的 file_results；历史任务无该字段时，从 findings 按
  // involved_files 现场推导（口径与后端 _aggregate_rule_results._file_results 一致：
  // 跨文件结论归属其列出的每个文件，未关联文件归入「（未关联文件）」）。
  const resolveFileResults = (rule: RuleResult): RuleFileResult[] => {
    if (rule.file_results) return rule.file_results
    const items = findings.filter((f) => f.rule_id === rule.rule_id)
    const groups = new Map<string, Finding[]>()
    for (const it of items) {
      const files = (it.involved_files || [])
        .map((s) => String(s).trim())
        .filter(Boolean)
      for (const fname of files.length ? files : ['（未关联文件）']) {
        const arr = groups.get(fname) || []
        arr.push(it)
        groups.set(fname, arr)
      }
    }
    const out: RuleFileResult[] = []
    groups.forEach((its, fname) => {
      const worst = Math.min(
        ...its.map((it) => FILE_STATUS_ORDER[it.status] ?? 3),
      )
      const status =
        (Object.keys(FILE_STATUS_ORDER) as FindingStatus[]).find(
          (k) => FILE_STATUS_ORDER[k] === worst,
        ) ?? 'pass'
      out.push({
        file: fname,
        status,
        issue_count: its.filter((it) => it.status !== 'pass').length,
        findings: its.map((it) => ({
          status: it.status,
          title: it.title || '',
          detail: it.detail || '',
          evidence: it.evidence || '',
          location: it.location || '',
          suggestion: it.suggestion || '',
          confidence: it.confidence,
        })),
      })
    })
    out.sort((a, b) => a.file.localeCompare(b.file))
    return out
  }

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
          {/* 折叠后的审核结论为汇总文本，含换行（【结论】/【核查范围】/【问题概述】分段），
              必须按原文换行渲染，否则挤成一团不可读；逐文件详细结论在展开下钻中查看 */}
          {row.detail && (
            <Typography.Text
              type="secondary"
              style={{
                fontSize: 12.5,
                lineHeight: 1.75,
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
              }}
            >
              {row.detail}
            </Typography.Text>
          )}
          {hasLocateEntry(row) && (
            <div className="evidence-block">
              <Space size={6}>
                {row.evidence && (
                  <Typography.Text type="secondary" style={{ fontSize: 11.5 }}>
                    原文依据
                  </Typography.Text>
                )}
                <Button
                  type="link"
                  size="small"
                  icon={<FileSearchOutlined />}
                  style={{ fontSize: 11.5, padding: 0, height: 'auto' }}
                  onClick={() => openLocate(row)}
                >
                  定位原文
                </Button>
              </Space>
              {row.evidence && <div style={{ marginTop: 3 }}>{row.evidence}</div>}
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
                      rowKey="_key"
                      pagination={false}
                      dataSource={keyedRuleResults}
                      expandable={{
                        // 规则维度下钻：展开查看该规则针对不同文件的逐条审核结论。
                        // 无任何结论的规则不提供展开（展开无内容可看）。
                        rowExpandable: (r) => resolveFileResults(r).length > 0,
                        expandedRowRender: (r) => {
                          const files = resolveFileResults(r)
                          if (!files.length) return null
                          return (
                            <Collapse
                              size="small"
                              items={files.map((fr, i) => ({
                                key: String(i),
                                label: (
                                  <Space size={8} wrap>
                                    <Tag
                                      color={STATUS_META[fr.status].color}
                                      icon={STATUS_META[fr.status].icon}
                                      style={{ marginRight: 0 }}
                                    >
                                      {STATUS_META[fr.status].label}
                                    </Tag>
                                    <span style={{ fontSize: 13 }}>{fr.file}</span>
                                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                                      {fr.issue_count > 0
                                        ? `${fr.issue_count} 条问题`
                                        : '无问题'}
                                    </Typography.Text>
                                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                                      {fr.findings.length} 条结论
                                    </Typography.Text>
                                  </Space>
                                ),
                                children: (
                                  <Space
                                    direction="vertical"
                                    size={10}
                                    style={{ width: '100%' }}
                                  >
                                    {fr.findings.map((fd, j) => (
                                      <div key={j}>
                                        <Space size={6} wrap>
                                          <Tag
                                            color={STATUS_META[fd.status].color}
                                            icon={STATUS_META[fd.status].icon}
                                            style={{ marginRight: 0 }}
                                          >
                                            {STATUS_META[fd.status].label}
                                          </Tag>
                                          <Typography.Text
                                            strong={fd.status === 'fail'}
                                            style={{ fontSize: 13 }}
                                          >
                                            {/* 历史任务可能存在空标题：按状态回落到
                                                「状态：规则名」，保证标题与状态口径一致 */}
                                            {fd.title || `（${STATUS_META[fd.status].label}）`}
                                          </Typography.Text>
                                        </Space>
                                        {fd.detail && (
                                          <Typography.Text
                                            type="secondary"
                                            style={{
                                              fontSize: 12.5,
                                              display: 'block',
                                              lineHeight: 1.75,
                                              marginTop: 2,
                                            }}
                                          >
                                            {fd.detail}
                                          </Typography.Text>
                                        )}
                                        {hasLocateEntry(fd) && (
                                          <div className="evidence-block" style={{ marginTop: 4 }}>
                                            <Space size={6} wrap>
                                              {fd.evidence && (
                                                <Typography.Text
                                                  type="secondary"
                                                  style={{ fontSize: 11.5 }}
                                                >
                                                  原文依据
                                                </Typography.Text>
                                              )}
                                              <Button
                                                type="link"
                                                size="small"
                                                icon={<FileSearchOutlined />}
                                                style={{ fontSize: 11.5, padding: 0, height: 'auto' }}
                                                onClick={() => openLocate(fd)}
                                              >
                                                定位原文
                                              </Button>
                                            </Space>
                                            {fd.evidence && (
                                              <div style={{ marginTop: 3 }}>{fd.evidence}</div>
                                            )}
                                          </div>
                                        )}
                                        {fd.suggestion && (
                                          <Typography.Text
                                            style={{
                                              fontSize: 12.5,
                                              color: '#d4380d',
                                              display: 'block',
                                              marginTop: 2,
                                            }}
                                          >
                                            整改建议：{fd.suggestion}
                                          </Typography.Text>
                                        )}
                                      </div>
                                    ))}
                                  </Space>
                                ),
                              }))}
                            />
                          )
                        },
                      }}
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
                rowKey="_key"
                columns={columns}
                dataSource={filtered}
                pagination={false}
                loading={running && !findings.length}
                expandable={{
                  // 结论已按「规则维度」折叠（每规则一条），展开查看该规则下
                  // 各送审文件的逐条明细；无 file_results 的历史任务不提供展开。
                  rowExpandable: (r) => (r.file_results || []).length > 0,
                  expandedRowRender: (r) => {
                    const files = r.file_results || []
                    if (!files.length) return null
                    return (
                      <Collapse
                        size="small"
                        items={files.map((fr, i) => ({
                          key: String(i),
                          label: (
                            <Space size={8} wrap>
                              <Tag
                                color={STATUS_META[fr.status].color}
                                icon={STATUS_META[fr.status].icon}
                                style={{ marginRight: 0 }}
                              >
                                {STATUS_META[fr.status].label}
                              </Tag>
                              <span style={{ fontSize: 13 }}>{fr.file}</span>
                              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                                {fr.issue_count > 0 ? `${fr.issue_count} 条问题` : '无问题'}
                              </Typography.Text>
                            </Space>
                          ),
                          children: (
                            <Space direction="vertical" size={8} style={{ width: '100%' }}>
                              {fr.findings.map((fd, j) => (
                                <div key={j}>
                                  <Space size={6} wrap>
                                    <Tag
                                      color={STATUS_META[fd.status].color}
                                      icon={STATUS_META[fd.status].icon}
                                      style={{ marginRight: 0 }}
                                    >
                                      {STATUS_META[fd.status].label}
                                    </Tag>
                                    <Typography.Text
                                      strong={fd.status === 'fail'}
                                      style={{ fontSize: 13 }}
                                    >
                                      {fd.title || `（${STATUS_META[fd.status].label}）`}
                                    </Typography.Text>
                                  </Space>
                                  {fd.detail && (
                                    <Typography.Text
                                      type="secondary"
                                      style={{
                                        fontSize: 12.5,
                                        display: 'block',
                                        lineHeight: 1.75,
                                        whiteSpace: 'pre-wrap',
                                        marginTop: 2,
                                      }}
                                    >
                                      {fd.detail}
                                    </Typography.Text>
                                  )}
                                  {fd.evidence && (
                                    <div className="evidence-block" style={{ marginTop: 4 }}>
                                      {fd.evidence}
                                    </div>
                                  )}
                                </div>
                              ))}
                            </Space>
                          ),
                        }))}
                      />
                    )
                  },
                }}
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

      <SourcePreviewModal
        open={!!srcLoc}
        location={srcLoc}
        onClose={() => setSrcLoc(null)}
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
