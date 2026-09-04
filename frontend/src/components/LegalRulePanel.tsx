import { useEffect, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Modal,
  Radio,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  Upload,
} from 'antd'
import {
  CheckCircleOutlined,
  DeleteOutlined,
  FileAddOutlined,
  PlusOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { api } from '../services/api'
import ProgressPanel from './ProgressPanel'
import type {
  LegalRuleset,
  LegalRulesetRule,
  LegalSourceMeta,
  ReviewMode,
  RuleGroup,
  UploadedFile,
} from '../types'

const STALENESS_YEARS = 8

function yearsSince(dateStr?: string): number | null {
  if (!dateStr) return null
  try {
    const d = new Date(dateStr + 'T00:00:00')
    if (Number.isNaN(d.getTime())) return null
    return Math.floor((Date.now() - d.getTime()) / (365.25 * 24 * 60 * 60 * 1000))
  } catch {
    return null
  }
}

function SourceMetaLines({ meta }: { meta: LegalSourceMeta[] }) {
  return (
    <div>
      <Typography.Text type="secondary">版本信息：</Typography.Text>
      {meta.map((m, i) => {
        const parts: string[] = []
        if (m.law_name) parts.push(`《${m.law_name}》`)
        else parts.push(m.filename)
        if (m.doc_number) parts.push(m.doc_number)
        if (m.latest_date) parts.push(`版本 ${m.latest_date}`)
        return <Typography.Text key={i} style={{ marginRight: 16 }}>{parts.join(' · ')}</Typography.Text>
      })}
    </div>
  )
}

function StalenessAlerts({ meta }: { meta: LegalSourceMeta[] }) {
  const items = meta
    .map((m) => ({ m, years: yearsSince(m.latest_date) }))
    .filter(({ years }) => years !== null && years > STALENESS_YEARS)
  if (!items.length) return null
  return (
    <>
      {items.map(({ m, years }, i) => {
        const title = m.law_name ? `《${m.law_name}》` : m.filename
        return (
          <Alert
            key={i}
            type="warning"
            showIcon
            message="法规时效性提示"
            description={`${title} 最近版本为 ${m.latest_date}，距今已 ${years} 年，可能已被修订或废止，请以现行有效版本为准。`}
          />
        )
      })}
    </>
  )
}

/** 生成/解析过程中的实时统计标签（文本块进度、已耗时、原始规则数）。 */
function MiningStatsTags({ rec }: { rec: LegalRuleset }) {
  const s = (rec.stats ?? {}) as Record<string, number>
  const chunks = Number(s.chunks || 0)
  const done = Number(s.chunks_done || 0)
  const elapsed = Number(s.elapsed_sec || 0)
  const raw = Number(s.raw_rules || 0)
  const fmtElapsed =
    elapsed >= 60
      ? `${Math.floor(elapsed / 60)} 分 ${Math.round(elapsed % 60)} 秒`
      : `${Math.round(elapsed)} 秒`
  return (
    <>
      {chunks > 0 && <Tag color="blue">文本块 {done}/{chunks}</Tag>}
      {raw > 0 && <Tag color="geekblue">已抽取 {raw} 条</Tag>}
      {elapsed > 0 && <Tag>已耗时 {fmtElapsed}</Tag>}
    </>
  )
}

/** 是否仍在解析中（未进入 ready/failed 终态）。 */
function isRunning(rec?: LegalRuleset | null): boolean {
  return rec?.status === 'pending' || rec?.status === 'mining'
}

interface Props {
  /** 当前审核模式（bid/tender/general），用于新建规则集时回填默认审核对象。 */
  mode: ReviewMode
  /** 已选中的法规规则集 id（向导状态，由外层持久化）。 */
  selectedRulesetId: string
  onSelectedRulesetIdChange: (id: string) => void
  /** 是否「仅用法规规则」：true=纯法规；false=叠加所选规则组。 */
  legalRulesOnly: boolean
  onLegalRulesOnlyChange: (v: boolean) => void
  /** 叠加模式下复用的规则组列表。 */
  ruleGroups: RuleGroup[]
  selectedGroupIds: string[]
  onSelectedGroupIdsChange: (ids: string[]) => void
}

/**
 * 法规临时规则配置：用户上传法律法规文件 → 模型解析全文自动生成一组审核规则
 * → 预览/裁剪 → 用这组规则（可叠加内置规则组）发起审核。
 *
 * 与「按规则组 / 按规则集」互斥，作为第三类「规则来源」接入 TaskWizard。
 */
export default function LegalRulePanel({
  mode,
  selectedRulesetId,
  onSelectedRulesetIdChange,
  legalRulesOnly,
  onLegalRulesOnlyChange,
  ruleGroups,
  selectedGroupIds,
  onSelectedGroupIdsChange,
}: Props) {
  const [list, setList] = useState<LegalRuleset[]>([])
  const [detail, setDetail] = useState<LegalRuleset | null>(null)
  const [loadingList, setLoadingList] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  /** 正在跟踪进度（轮询）的规则集 id；为空表示无在途任务。 */
  const [trackingId, setTrackingId] = useState('')
  /** 跟踪中的实时记录（含 logs），结束后保留终态供回看过程。 */
  const [live, setLive] = useState<LegalRuleset | null>(null)
  /** 本组件内已发起过生成的规则集 id（区分「未开始 / 进行中 / 已结束」）。 */
  const [startedId, setStartedId] = useState('')
  const selectedIdRef = useRef(selectedRulesetId)
  selectedIdRef.current = selectedRulesetId

  const loadList = async () => {
    setLoadingList(true)
    try {
      const { rulesets } = await api.listLegalRulesets()
      setList(rulesets)
    } catch {
      /* 忽略列表加载失败 */
    } finally {
      setLoadingList(false)
    }
  }

  useEffect(() => {
    void loadList()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    let alive = true
    if (!selectedRulesetId) {
      setDetail(null)
      return
    }
    api
      .getLegalRuleset(selectedRulesetId)
      .then((d) => {
        if (alive) setDetail(d)
        // 在途任务（例如刷新页面后回到该规则集）自动接上进度轮询，
        // 避免「生成中」永远停在一个百分比上。
        if (alive && isRunning(d)) setTrackingId(d.id)
      })
      .catch(() => {
        if (alive) setDetail(null)
      })
    return () => {
      alive = false
    }
  }, [selectedRulesetId])

  // 进度轮询放在父组件而非弹窗内：弹窗关闭后（点「后台运行」）解析继续，
  // 用户在下方规则集卡片里仍能看到实时过程日志。
  useEffect(() => {
    if (!trackingId) return
    let alive = true
    let timer: ReturnType<typeof setTimeout> | null = null
    let settled = false

    const tick = async () => {
      if (!alive) return
      try {
        const d = await api.getLegalRuleset(trackingId)
        if (!alive) return
        setLive(d)
        if (d.status === 'ready' || d.status === 'failed') {
          settled = true
          // 终态同步到详情卡；若用户期间切了别的规则集则不动详情
          setDetail((prev) => (selectedIdRef.current === d.id ? d : prev))
          void loadList()
          return
        }
      } catch {
        /* 单次轮询失败忽略，下一轮重试 */
      }
      if (alive && !settled) timer = setTimeout(tick, 1000)
    }
    void tick()

    return () => {
      alive = false
      if (timer) clearTimeout(timer)
      if (!settled) void loadList()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trackingId])

  const ready = detail?.status === 'ready'
  // 弹窗内发起、且仍在本组件生命周期内跟踪的记录（用于展示刚完成的过程日志）
  const liveHere = live && live.id === detail?.id ? live : null
  const showProgress = !!detail && (isRunning(detail) || !!liveHere?.logs?.length)
  const progressRec = liveHere ?? detail

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Typography.Paragraph type="secondary" style={{ fontSize: 13, marginBottom: 0 }}>
        上传法律法规文件，由模型解析全文并自动抽取一组「临时审核规则」，可预览裁剪后再用于审核。
        规则不污染正式规则库，可随时「转正」为正式规则集长期复用。
      </Typography.Paragraph>

      <Space style={{ width: '100%' }} wrap>
        <Select
          style={{ minWidth: 320 }}
          placeholder="选择已生成的法规规则集"
          loading={loadingList}
          value={selectedRulesetId || undefined}
          onChange={(v) => onSelectedRulesetIdChange(v)}
          options={list.map((r) => ({
            value: r.id,
            label: `${r.name}（${
              r.status === 'ready' ? `${r.rule_count ?? r.rules?.length ?? 0} 条` : r.status
            }）`,
          }))}
        />
        <Button icon={<ReloadOutlined />} onClick={() => void loadList()}>
          刷新
        </Button>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
          新建法规规则集
        </Button>
      </Space>

      {selectedRulesetId && detail && (
        <Card size="small" title={`规则预览：${detail.name}`} className="legal-rules-card">
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Space size={8} wrap>
              <Tag color={ready ? 'green' : 'orange'}>
                {detail.status === 'ready'
                  ? `已生成 ${detail.rules?.length ?? 0} 条`
                  : detail.status === 'mining'
                    ? '生成中…'
                    : detail.status === 'failed'
                      ? '生成失败'
                      : detail.status}
              </Tag>
              {detail.version != null && <Tag>v{detail.version}</Tag>}
              {detail.source_files?.length ? (
                <Typography.Text type="secondary">
                  来源：{detail.source_files.map((f) => f.filename).join('、')}
                </Typography.Text>
              ) : null}
              {detail.updated_at && (
                <Typography.Text type="secondary">
                  更新于 {detail.updated_at}
                </Typography.Text>
              )}
            </Space>

            {detail.status === 'failed' && (
              <Alert
                type="error"
                showIcon
                message="解析失败"
                description={detail.error || detail.progress_message || '请重试或调整文件后重新生成'}
              />
            )}

            {showProgress && (
              <ProgressPanel
                title="法规解析过程"
                logs={progressRec?.logs ?? []}
                progress={Math.round(progressRec?.progress ?? 0)}
                running={isRunning(progressRec)}
                extra={progressRec ? <MiningStatsTags rec={progressRec} /> : null}
                emptyTip="正在提交解析任务…"
              />
            )}

            {detail.source_meta?.length ? (
              <Space direction="vertical" size={8} style={{ width: '100%' }}>
                <SourceMetaLines meta={detail.source_meta} />
                <StalenessAlerts meta={detail.source_meta} />
              </Space>
            ) : null}

            {detail.warnings?.length ? (
              <Alert
                type="warning"
                showIcon
                message="生成告警"
                description={
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {detail.warnings.map((w, i) => (
                      <li key={i}>{w}</li>
                    ))}
                  </ul>
                }
              />
            ) : null}

            {ready && (
              <LegalRulesTable
                rules={detail.rules ?? []}
                onChanged={async (rules) => {
                  await api.updateLegalRuleset(detail.id, { rules })
                }}
              />
            )}
          </Space>
        </Card>
      )}

      <Card size="small" title="审核模式" className="legal-mode-card">
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          <Radio.Group
            value={legalRulesOnly ? 'only' : 'overlay'}
            onChange={(e) => onLegalRulesOnlyChange(e.target.value === 'only')}
          >
            <Radio value="only">仅用法规规则（不套用任何内置规则）</Radio>
            <Radio value="overlay">叠加所选规则组</Radio>
          </Radio.Group>
          {!legalRulesOnly && (
            <Select
              mode="multiple"
              style={{ width: '100%' }}
              placeholder="选择需要叠加的审核规则组"
              value={selectedGroupIds}
              onChange={(v) => onSelectedGroupIdsChange(v)}
              options={ruleGroups.map((g) => ({ value: g.id, label: g.name }))}
            />
          )}
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {legalRulesOnly
              ? '本次审核只跑由法规文件自动生成的规则。'
              : '本次审核 = 所选规则组规则 + 法规规则（按规则 id 去重合并）。'}
          </Typography.Text>
        </Space>
      </Card>

      <CreateModal
        open={createOpen}
        mode={mode}
        live={live && live.id === startedId ? live : null}
        startedId={startedId}
        onStarted={(rec) => {
          setLive(null)
          setStartedId(rec.id)
          onSelectedRulesetIdChange(rec.id)
          void loadList()
          setTrackingId(rec.id)
        }}
        onClose={() => setCreateOpen(false)}
        onReset={() => {
          setStartedId('')
          void loadList()
        }}
      />
    </Space>
  )
}

// --------------------------------------------------------------------------- //
// 规则预览表格：支持逐条启用/停用、删除；变更后整体 PATCH 回后端，确保裁剪确定生效。
// --------------------------------------------------------------------------- //
function LegalRulesTable({
  rules,
  onChanged,
}: {
  rules: LegalRulesetRule[]
  onChanged: (rules: LegalRulesetRule[]) => Promise<void> | void
}) {
  const [data, setData] = useState<LegalRulesetRule[]>(rules)
  useEffect(() => setData(rules), [rules])
  const [saving, setSaving] = useState(false)

  const commit = (next: LegalRulesetRule[]) => {
    setData(next)
    setSaving(true)
    Promise.resolve(onChanged(next))
      .catch(() => undefined)
      .finally(() => setSaving(false))
  }

  const columns = [
    {
      title: '启用',
      dataIndex: 'enabled',
      width: 70,
      render: (_: unknown, r: LegalRulesetRule, idx: number) => (
        <Switch
          checked={r.enabled !== false}
          onChange={(v) =>
            commit(data.map((x, i) => (i === idx ? { ...x, enabled: v } : x)))
          }
        />
      ),
    },
    { title: '规则', dataIndex: 'name', ellipsis: true },
    {
      title: '类别',
      dataIndex: 'category',
      width: 110,
      render: (v: string) => <Tag>{v || '-'}</Tag>,
    },
    {
      title: '严重级',
      dataIndex: 'severity',
      width: 90,
      render: (v: string) => <Tag color="red">{v || '-'}</Tag>,
    },
    {
      title: '依据',
      dataIndex: 'legal_basis',
      ellipsis: true,
      render: (v: string) => <Typography.Text type="secondary">{v || '-'}</Typography.Text>,
    },
    {
      title: '确定性',
      dataIndex: 'structured',
      width: 150,
      render: (_: unknown, r: LegalRulesetRule, idx: number) => {
        if (r.structured) {
          return (
            <Space size={4}>
              <Tooltip title="已转为确定性规则：审核时由规则引擎直接判定，LLM 不可推翻。">
                <Tag color="green" icon={<ThunderboltOutlined />}>
                  确定性
                </Tag>
              </Tooltip>
              <Button
                type="link"
                size="small"
                style={{ padding: 0 }}
                onClick={() =>
                  commit(
                    data.map((x, i) =>
                      i === idx ? { ...x, structured: undefined } : x
                    )
                  )
                }
              >
                撤销
              </Button>
            </Space>
          )
        }
        if (r.structured_hint) {
          return (
            <Space size={4}>
              <Tooltip title="模型为该规则抽取了可计算的结构化条件（如金额阈值/禁止关键词）。点击「转确定性」后由规则引擎直接判定，根治 LLM 对量化阈值的判定漂移。">
                <Tag color="blue" icon={<CheckCircleOutlined />}>
                  可结构化
                </Tag>
              </Tooltip>
              <Button
                type="link"
                size="small"
                style={{ padding: 0 }}
                onClick={() =>
                  commit(
                    data.map((x, i) =>
                      i === idx ? { ...x, structured: x.structured_hint } : x
                    )
                  )
                }
              >
                转确定性
              </Button>
            </Space>
          )
        }
        return <Typography.Text type="secondary">-</Typography.Text>
      },
    },
    {
      title: '',
      width: 60,
      render: (_: unknown, _r: LegalRulesetRule, idx: number) => (
        <Button
          type="text"
          danger
          icon={<DeleteOutlined />}
          onClick={() => commit(data.filter((_, i) => i !== idx))}
        />
      ),
    },
  ]

  return (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      <Table
        size="small"
        rowKey={(r, i) => r.id || String(i)}
        dataSource={data}
        columns={columns as never}
        pagination={false}
        scroll={{ y: 320 }}
      />
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {saving ? '保存中…' : '改动将自动保存为规则集的新版本。'}
      </Typography.Text>
    </Space>
  )
}

// --------------------------------------------------------------------------- //
// 新建法规规则集：上传 → 预检 → 生成（父组件轮询，与审核界面同一个过程面板）。
// --------------------------------------------------------------------------- //
function CreateModal({
  open,
  mode,
  live,
  startedId,
  onStarted,
  onClose,
  onReset,
}: {
  open: boolean
  mode: ReviewMode
  /** 父组件轮询得到的实时记录（仅当属于本次生成时传入）。 */
  live: LegalRuleset | null
  /** 本次已发起生成的规则集 id，空表示尚未开始。 */
  startedId: string
  onStarted: (rec: LegalRuleset) => void
  onClose: () => void
  onReset: () => void
}) {
  const [files, setFiles] = useState<UploadedFile[]>([])
  const [uploading, setUploading] = useState(false)
  const [preview, setPreview] = useState<{
    total_chars: number
    chunks: number
    split_mode: string
    chunk_chars: number
    max_rules: number
    truncated: boolean
    source_meta?: LegalSourceMeta[]
  } | null>(null)
  const [name, setName] = useState('')
  const [error, setError] = useState('')

  const phase: 'idle' | 'running' | 'ready' | 'failed' = !startedId
    ? 'idle'
    : !live || live.status === 'pending' || live.status === 'mining'
      ? 'running'
      : live.status

  const reset = () => {
    setFiles([])
    setUploading(false)
    setPreview(null)
    setName('')
    setError('')
  }

  useEffect(() => {
    if (!open) {
      reset()
      // 只清「本次生成」标记（下次打开是全新表单）；
      // live 保留——刚完成的过程日志继续展示在下方规则集卡片里可回看，
      // 若是后台运行中的任务，轮询会持续刷新它。
      onReset()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const doUpload = async (fileList: File[]) => {
    if (!fileList.length) return
    setUploading(true)
    setError('')
    try {
      const { files: uploaded } = await api.uploadFiles(fileList, ['legal'])
      setFiles(uploaded)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setUploading(false)
    }
  }

  const doPreview = async () => {
    if (!files.length) return
    setError('')
    try {
      const p = await api.previewLegalRules({
        file_ids: files.map((f) => f.file_id),
        mode,
      })
      setPreview({
        total_chars: p.total_chars,
        chunks: p.chunks,
        split_mode: p.split_mode,
        chunk_chars: p.chunk_chars,
        max_rules: p.max_rules,
        truncated: p.truncated,
        source_meta: p.source_meta,
      })
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const doGenerate = async () => {
    if (!files.length) return
    setError('')
    try {
      const rec = await api.generateLegalRules({
        file_ids: files.map((f) => f.file_id),
        name: name.trim() || files[0]?.filename || '法规临时规则',
        mode,
      })
      onStarted(rec) // 交给父组件轮询；弹窗可随时关闭，解析在后台继续
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <Modal
      title="新建法规临时规则集"
      open={open}
      onCancel={onClose}
      maskClosable={phase !== 'running'}
      footer={
        phase === 'running'
          ? [
              <Button key="bg" onClick={onClose}>
                后台运行
              </Button>,
            ]
          : phase === 'ready'
            ? [
                <Button key="view" type="primary" onClick={onClose}>
                  查看规则
                </Button>,
              ]
            : phase === 'failed'
              ? [
                  <Button key="cancel" onClick={onClose}>
                    关闭
                  </Button>,
                  <Button
                    key="retry"
                    type="primary"
                    onClick={() => void doGenerate()}
                    disabled={!files.length}
                  >
                    重新生成
                  </Button>,
                ]
              : [
                  <Button key="cancel" onClick={onClose}>
                    取消
                  </Button>,
                  <Button
                    key="preview"
                    onClick={() => void doPreview()}
                    disabled={!files.length}
                  >
                    预检
                  </Button>,
                  <Button
                    key="gen"
                    type="primary"
                    onClick={() => void doGenerate()}
                    disabled={!files.length}
                  >
                    开始生成
                  </Button>,
                ]
      }
      width={640}
      destroyOnClose
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Upload.Dragger
          multiple
          accept=".pdf,.doc,.docx,.txt,.md"
          beforeUpload={(f) => {
            void doUpload([f as unknown as File])
            return false
          }}
          showUploadList={false}
          disabled={uploading || phase !== 'idle'}
        >
          <p className="ant-upload-drag-icon">
            <FileAddOutlined />
          </p>
          <p className="ant-upload-text">点击或拖拽法律法规文件（pdf/docx/txt）</p>
        </Upload.Dragger>

        {files.map((f) => (
          <Typography.Text key={f.file_id}>
            {f.filename}（{f.char_count} 字）
          </Typography.Text>
        ))}

        {phase === 'idle' && (
          <Alert
            type="info"
            showIcon
            message="生成耗时提示"
            description="法规规则由大模型逐块抽取，按当前模型响应速度每块约需 1–2 分钟，长法规整体生成可能需要数分钟。生成期间可在下方实时查看解析进度，也可点「后台运行」关闭本窗口。"
          />
        )}

        {phase === 'idle' && preview && (
          <Alert
            type={preview.truncated ? 'warning' : 'info'}
            showIcon
            message={`预计切分为 ${preview.chunks} 块（${preview.split_mode}），最多生成 ${preview.max_rules} 条规则`}
            description={
              preview.truncated
                ? `源文件 ${preview.total_chars} 字超过处理上限，将截断处理。`
                : `源文件共 ${preview.total_chars} 字。`
            }
          />
        )}

        {phase === 'idle' && preview?.source_meta?.length ? (
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Alert
              type="info"
              showIcon
              message="识别到法规版本信息"
              description={<SourceMetaLines meta={preview.source_meta} />}
            />
            <StalenessAlerts meta={preview.source_meta} />
          </Space>
        ) : null}

        {phase !== 'idle' && (
          <ProgressPanel
            title="法规解析过程"
            logs={live?.logs ?? []}
            progress={Math.round(live?.progress ?? 0)}
            running={phase === 'running'}
            extra={live ? <MiningStatsTags rec={live} /> : null}
            emptyTip="正在提交解析任务…"
          />
        )}

        {phase === 'ready' && !!live?.warnings?.length && (
          <Alert
            type="warning"
            showIcon
            message="生成告警"
            description={
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {live.warnings?.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            }
          />
        )}

        {phase === 'failed' && (
          <Alert
            type="error"
            showIcon
            message="解析失败"
            description={live?.error || live?.progress_message || '请重试或调整文件后重新生成'}
          />
        )}

        {error && <Alert type="error" showIcon message={error} />}
      </Space>
    </Modal>
  )
}
