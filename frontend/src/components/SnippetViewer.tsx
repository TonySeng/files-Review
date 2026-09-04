import { useEffect, useState } from 'react'
import { Alert, Modal, Spin, Tabs, Tag, Tooltip, Typography } from 'antd'
import { api } from '../services/api'
import type { LocateMatch } from '../types'

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

      <div
        style={{
          maxHeight: '46vh',
          overflowY: 'auto',
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
