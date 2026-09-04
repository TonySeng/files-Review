import { useEffect, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Divider,
  Input,
  Modal,
  Spin,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import {
  AimOutlined,
  LeftOutlined,
  RightOutlined,
} from '@ant-design/icons'
import { api } from '../services/api'
import type { FilePreview, LocateMatch } from '../types'

const MATCH_META: Record<
  LocateMatch['match_type'],
  { label: string; color: string; tip: string }
> = {
  exact: { label: '精确匹配', color: 'success', tip: '与文档原文完全一致' },
  normalized: {
    label: '格式差异',
    color: 'processing',
    tip: '忽略空格与全角/半角标点后一致',
  },
  fuzzy: {
    label: '近似匹配',
    color: 'warning',
    tip: '模型对原文做了改写，仅定位到相近段落，边界可能不精确',
  },
}

const ROLE_LABEL: Record<string, string> = {
  tender: '招标文档',
  bid: '投标文档',
  attachment: '附件',
}

interface Props {
  open: boolean
  snippet: string
  /** 模型标注的位置（如「第3页」），用于限定检索范围 */
  location?: string
  /** 多候选片段，按顺序回退（通常 evidence → detail → title） */
  candidates?: string[]
  /** 限定检索范围；为空则在全部已上传文件中查找 */
  fileIds?: string[]
  onClose: () => void
}

/** 单个定位命中的原文分页预览：页码跳转 + 页内高亮（与定位共用同一份提取文本）。 */
function PagePreview({ match }: { match: LocateMatch }) {
  const [preview, setPreview] = useState<FilePreview | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [pageInput, setPageInput] = useState('')
  const firstMarkRef = useRef<HTMLDivElement | null>(null)

  const load = (params: {
    page?: number
    start?: number
    end?: number
    snippet?: string
  }) => {
    setLoading(true)
    setError(null)
    api
      .getFilePreview(match.file_id, params)
      .then(setPreview)
      .catch((err) => setError((err as Error).message))
      .finally(() => setLoading(false))
  }

  // 首次加载：按定位命中的绝对偏移跳页并高亮
  useEffect(() => {
    if (match.file_id) {
      load({ start: match.start, end: match.end, snippet: match.matched })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [match.file_id, match.start])

  // 预览更新后滚动到首个高亮处
  useEffect(() => {
    if (preview && preview.highlights.length > 0) {
      firstMarkRef.current?.scrollIntoView({ block: 'center' })
    }
  }, [preview])

  const gotoPage = (n: number) => {
    if (
      !preview ||
      n < 1 ||
      (preview.is_paged && n > preview.page_count) ||
      n === preview.page
    ) {
      return
    }
    // 跳页时带上命中片段做页内定位：片段恰在该页时继续高亮，否则仅展示页面
    load({ page: n, snippet: match.matched })
  }

  const renderPageText = (p: FilePreview) => {
    if (!p.highlights.length) {
      return <Typography.Text>{p.page_text}</Typography.Text>
    }
    const nodes: React.ReactNode[] = []
    let cursor = 0
    p.highlights.forEach((h, i) => {
      const s = Math.max(0, Math.min(h.start, p.page_text.length))
      const e = Math.max(s, Math.min(h.end, p.page_text.length))
      if (s > cursor) {
        nodes.push(<Typography.Text key={`t${i}`}>{p.page_text.slice(cursor, s)}</Typography.Text>)
      }
      nodes.push(
        <mark
          key={`m${i}`}
          ref={i === 0 ? firstMarkRef : undefined}
          style={{ background: '#ffe58f', padding: '1px 2px', fontWeight: 500 }}
        >
          {p.page_text.slice(s, e)}
        </mark>,
      )
      cursor = e
    })
    if (cursor < p.page_text.length) {
      nodes.push(<Typography.Text key="tail">{p.page_text.slice(cursor)}</Typography.Text>)
    }
    return nodes
  }

  const backToMatch = () => load({ start: match.start, end: match.end, snippet: match.matched })

  return (
    <div>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          marginBottom: 8,
          flexWrap: 'wrap',
        }}
      >
        <Typography.Text strong style={{ fontSize: 12.5 }}>
          原文预览
        </Typography.Text>
        {preview && (
          <>
            <Tag color="blue">{preview.is_paged ? preview.page_label : '全文'}</Tag>
            {preview.is_paged && (
              <>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  共 {preview.page_count} 页
                </Typography.Text>
                <Button
                  size="small"
                  icon={<LeftOutlined />}
                  disabled={loading || preview.page <= 1}
                  onClick={() => gotoPage(preview.page - 1)}
                />
                <Input
                  size="small"
                  style={{ width: 64 }}
                  value={pageInput}
                  placeholder={String(preview.page)}
                  onChange={(e) => setPageInput(e.target.value.replace(/\D/g, ''))}
                  onPressEnter={() => {
                    const n = parseInt(pageInput, 10)
                    if (n) {
                      gotoPage(n)
                      setPageInput('')
                    }
                  }}
                />
                <Button
                  size="small"
                  icon={<RightOutlined />}
                  disabled={loading || preview.page >= preview.page_count}
                  onClick={() => gotoPage(preview.page + 1)}
                />
                <Tooltip title="回到定位命中的页面与位置">
                  <Button size="small" icon={<AimOutlined />} onClick={backToMatch}>
                    定位页
                  </Button>
                </Tooltip>
              </>
            )}
          </>
        )}
        {preview?.match_type && preview.match_type !== 'located' && (
          <Tag color={preview.match_type === 'fuzzy' ? 'warning' : 'success'}>
            页内高亮：{preview.match_type === 'fuzzy' ? '近似' : '一致'}
          </Tag>
        )}
      </div>

      {loading && (
        <div style={{ textAlign: 'center', padding: '24px 0' }}>
          <Spin size="small" tip="正在加载原文…" />
        </div>
      )}

      {error && <Alert type="error" showIcon message="预览加载失败" description={error} />}

      {!loading && !error && preview && (
        <>
          {(() => {
            const matchPage = pageOfMatch(match)
            return preview.is_paged &&
              matchPage != null &&
              preview.page !== matchPage &&
              !preview.highlights.length ? (
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 8, fontSize: 12 }}
                message={`当前为第 ${preview.page} 页，命中内容位于第 ${matchPage} 页，点「定位页」可跳回`}
              />
            ) : null
          })()}
          <div
            style={{
              maxHeight: '42vh',
              overflowY: 'auto',
              background: '#fff',
              border: '1px solid #e5e7eb',
              borderRadius: 4,
              padding: 16,
              fontSize: 13,
              lineHeight: 1.9,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
            }}
          >
            {renderPageText(preview)}
          </div>
          <Typography.Text type="secondary" style={{ fontSize: 11.5, marginTop: 4, display: 'block' }}>
            预览内容与原文定位使用同一份解析文本，页码与高亮位置严格对应；如需查看原始版式，可
            <a onClick={() => window.open(`/api/files/${match.file_id}`, '_blank')}>下载原文件</a>。
          </Typography.Text>
        </>
      )}
    </div>
  )
}

/** 从定位结果推断命中所在页码（用于跨页提示文案）；无页码信息时返回 null。 */
function pageOfMatch(m: LocateMatch): number | null {
  if (!m.location) return null
  const mm = /第\s*(\d+)\s*页/.exec(m.location)
  return mm ? parseInt(mm[1], 10) : null
}

export default function SnippetViewer({
  open,
  snippet,
  location,
  candidates,
  fileIds,
  onClose,
}: Props) {
  const [loading, setLoading] = useState(false)
  const [matches, setMatches] = useState<LocateMatch[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open || !snippet.trim()) return
    setLoading(true)
    setError(null)
    setMatches([])
    api
      .locateSnippet(snippet, fileIds, 240, location, candidates)
      .then((res) => setMatches(res.matches))
      .catch((err) => setError((err as Error).message))
      .finally(() => setLoading(false))
  }, [open, snippet, location, candidates, fileIds])

  const renderMatch = (m: LocateMatch) => (
    <div>
      <div style={{ marginBottom: 8 }}>
        <Tooltip title={MATCH_META[m.match_type].tip}>
          <Tag color={MATCH_META[m.match_type].color}>
            {MATCH_META[m.match_type].label}
          </Tag>
        </Tooltip>
        {m.location && <Tag>{m.location}</Tag>}
        {m.model_location && (
          <Tooltip
            title={
              m.page_constrained
                ? '已按模型标注位置限定到对应页面/表格内检索，命中更精准'
                : '模型标注位置未在对应区域命中，已回退到全文检索'
            }
          >
            <Tag color={m.page_constrained ? 'blue' : 'default'}>
              {`模型标注：${m.model_location}`}
              {m.page_constrained ? '（已限定）' : '（回退全文）'}
            </Tag>
          </Tooltip>
        )}
        <Tag>{ROLE_LABEL[m.role] || m.role}</Tag>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          字符位置 {m.start}–{m.end}
          {m.match_type === 'fuzzy' && ` · 相似度 ${Math.round(m.confidence * 100)}%`}
        </Typography.Text>
      </div>

      {m.match_type === 'fuzzy' && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 8, fontSize: 12 }}
          message="近似匹配"
          description="审核结论中的表述与原文不完全一致，高亮范围为估算值，请结合上下文人工确认。"
        />
      )}

      {/* 命中片段上下文（快速浏览） */}
      <div
        style={{
          background: '#fafafa',
          border: '1px solid #f0f0f0',
          borderRadius: 4,
          padding: 12,
          fontSize: 13,
          lineHeight: 1.9,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
        }}
      >
        {m.truncated_before && (
          <Typography.Text type="secondary">{'…（上文省略）\n'}</Typography.Text>
        )}
        <Typography.Text type="secondary">{m.context_before}</Typography.Text>
        <mark style={{ background: '#ffe58f', padding: '1px 2px', fontWeight: 500 }}>
          {m.matched}
        </mark>
        <Typography.Text type="secondary">{m.context_after}</Typography.Text>
        {m.truncated_after && (
          <Typography.Text type="secondary">{'\n'}…（下文省略）</Typography.Text>
        )}
      </div>

      {/* 原文分页预览：自动跳页 + 页内高亮 */}
      <Divider style={{ margin: '12px 0 8px' }} />
      <PagePreview match={m} />
    </div>
  )

  return (
    <Modal
      open={open}
      title="原文定位"
      width={860}
      footer={null}
      onCancel={onClose}
      destroyOnClose
    >
      <div className="evidence-block" style={{ marginBottom: 12 }}>
        <Typography.Text type="secondary" style={{ fontSize: 11.5 }}>
          待定位内容
        </Typography.Text>
        <div style={{ marginTop: 3 }}>{snippet}</div>
      </div>

      {loading && (
        <div style={{ textAlign: 'center', padding: '32px 0' }}>
          <Spin tip="正在文档中检索…" />
        </div>
      )}

      {error && <Alert type="error" showIcon message="定位失败" description={error} />}

      {!loading && !error && matches.length === 0 && (
        <Alert
          type="info"
          showIcon
          message="未在文档中找到对应原文"
          description="该内容可能是模型的归纳表述而非原文摘录，或原文位于已截断的部分。建议打开文件全文人工核对。"
        />
      )}

      {!loading && matches.length > 0 && (
        <Tabs
          size="small"
          items={matches.map((m, idx) => ({
            key: String(idx),
            label: (
              <span style={{ fontSize: 13 }}>
                {m.filename}
                {m.location ? ` · ${m.location}` : ''}
              </span>
            ),
            children: renderMatch(m),
          }))}
        />
      )}
    </Modal>
  )
}
