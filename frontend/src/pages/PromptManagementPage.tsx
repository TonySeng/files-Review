import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  App as AntdApp,
  Button,
  Checkbox,
  Drawer,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
} from 'antd'
import {
  ArrowLeftOutlined,
  DiffOutlined,
  ExperimentOutlined,
  ReloadOutlined,
  RollbackOutlined,
  SaveOutlined,
  UndoOutlined,
} from '@ant-design/icons'
import { api } from '../services/api'
import type { PromptDetail, PromptDiffResult, PromptMeta, PromptVersion } from '../types'

interface Props {
  onBack: () => void
}

const CATEGORY_COLOR: Record<string, string> = {
  合规审核: 'blue',
  一致性核查: 'purple',
  法规挖掘: 'cyan',
}

/** 大文本占位符用多行输入，其余用单行 */
const LARGE_VARS = new Set([
  'docs_text',
  'doc_text',
  'seg_text',
  'chunk',
  'rules_block',
  'tender_text',
])

function formatTime(iso?: string): string {
  if (!iso) return '-'
  try {
    const d = new Date(iso)
    return d.toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

export default function PromptManagementPage({ onBack }: Props) {
  const { message } = AntdApp.useApp()
  const [list, setList] = useState<PromptMeta[]>([])
  const [loading, setLoading] = useState(false)
  const [active, setActive] = useState<PromptDetail | null>(null)
  const [activeLoading, setActiveLoading] = useState(false)
  const [tab, setTab] = useState<'edit' | 'history' | 'test'>('edit')

  const loadList = useCallback(async () => {
    setLoading(true)
    try {
      const res = await api.listPrompts()
      setList(res.prompts)
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
    async (key: string, targetTab: 'edit' | 'history' | 'test' = 'edit') => {
      setActiveLoading(true)
      try {
        const d = await api.getPrompt(key)
        setActive(d)
        setTab(targetTab)
      } catch (e) {
        message.error((e as Error).message)
      } finally {
        setActiveLoading(false)
      }
    },
    [message],
  )

  const refreshActive = useCallback(
    async (key: string) => {
      const d = await api.getPrompt(key)
      setActive(d)
      void loadList()
      return d
    },
    [loadList],
  )

  const columns = [
    {
      title: '模板',
      key: 'name',
      width: 260,
      render: (_: unknown, r: PromptMeta) => (
        <div>
          <Typography.Text strong>{r.name}</Typography.Text>
          <Typography.Paragraph type="secondary" code style={{ marginBottom: 0, fontSize: 12 }}>
            {r.key}
          </Typography.Paragraph>
        </div>
      ),
    },
    {
      title: '类别',
      dataIndex: 'category',
      key: 'category',
      width: 110,
      render: (c: string) => <Tag color={CATEGORY_COLOR[c] ?? 'default'}>{c}</Tag>,
    },
    {
      title: '说明',
      dataIndex: 'description',
      key: 'description',
      ellipsis: true,
    },
    {
      title: '版本',
      key: 'version',
      width: 130,
      render: (_: unknown, r: PromptMeta) => (
        <Space size={4}>
          <Tag>v{r.current_version}</Tag>
          <Typography.Text type="secondary">共 {r.version_count ?? 1} 版</Typography.Text>
        </Space>
      ),
    },
    {
      title: '状态',
      key: 'customized',
      width: 90,
      render: (_: unknown, r: PromptMeta) =>
        r.customized ? <Tag color="orange">已自定义</Tag> : <Tag>内置默认</Tag>,
    },
    {
      title: '最近更新',
      dataIndex: 'updated_at',
      key: 'updated_at',
      width: 160,
      render: (t: string) => (
        <Typography.Text type="secondary">{formatTime(t)}</Typography.Text>
      ),
    },
    {
      title: '操作',
      key: 'action',
      width: 180,
      render: (_: unknown, r: PromptMeta) => (
        <Space size={4}>
          <Button size="small" type="link" onClick={() => void openDetail(r.key, 'edit')}>
            编辑
          </Button>
          <Button size="small" type="link" onClick={() => void openDetail(r.key, 'history')}>
            历史
          </Button>
          <Button size="small" type="link" onClick={() => void openDetail(r.key, 'test')}>
            测试
          </Button>
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
            <ExperimentOutlined className="rm-title-icon" />
            Prompt 管理
          </div>
          <div className="rm-subtitle">
            集中管理系统全部 LLM 提示词：编辑即生效，支持版本回滚与发布前测试
          </div>
        </div>
        <Button
          icon={<ReloadOutlined />}
          onClick={() => void loadList()}
          loading={loading}
          style={{ marginLeft: 'auto' }}
        >
          刷新
        </Button>
      </div>

      <Table
        rowKey="key"
        loading={loading}
        dataSource={list}
        columns={columns as never}
        pagination={false}
        size="middle"
      />

      <PromptDrawer
        detail={active}
        open={!!active}
        loading={activeLoading}
        tab={tab}
        onTabChange={setTab}
        onClose={() => setActive(null)}
        onRefresh={refreshActive}
      />
    </div>
  )
}

// --------------------------------------------------------------------------- //
// 详情抽屉：编辑 / 版本历史 / 测试验证
// --------------------------------------------------------------------------- //

function PromptDrawer({
  detail,
  open,
  loading,
  tab,
  onTabChange,
  onClose,
  onRefresh,
}: {
  detail: PromptDetail | null
  open: boolean
  loading: boolean
  tab: 'edit' | 'history' | 'test'
  onTabChange: (t: 'edit' | 'history' | 'test') => void
  onClose: () => void
  onRefresh: (key: string) => Promise<PromptDetail>
}) {
  const { message } = AntdApp.useApp()
  const [content, setContent] = useState('')
  const [comment, setComment] = useState('')
  const [saving, setSaving] = useState(false)
  const [dirty, setDirty] = useState(false)

  useEffect(() => {
    if (detail) {
      setContent(detail.versions[detail.versions.length - 1]?.content ?? '')
      setComment('')
      setDirty(false)
    }
  }, [detail])

  if (!detail) return null

  const save = async () => {
    if (!content.trim()) {
      message.warning('模板内容不能为空')
      return
    }
    setSaving(true)
    try {
      await api.updatePrompt(detail.key, content, comment)
      await onRefresh(detail.key)
      setDirty(false)
      setComment('')
      message.success('已保存为新版本并即时生效')
    } catch (e) {
      message.error((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const reset = async () => {
    setSaving(true)
    try {
      const d = await api.resetPrompt(detail.key)
      setContent(d.versions[d.versions.length - 1].content)
      setDirty(false)
      await onRefresh(detail.key)
      message.success('已重置为内置默认')
    } catch (e) {
      message.error((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const editTab = (
    <div>
      <Space wrap style={{ marginBottom: 8 }}>
        <Typography.Text type="secondary">占位符（保存后由引擎按变量注入）：</Typography.Text>
        {(detail.placeholders ?? []).length === 0 ? (
          <Tag>无（静态提示词）</Tag>
        ) : (
          (detail.placeholders ?? []).map((p) => (
            <Tag key={p} color="geekblue" style={{ fontFamily: 'monospace' }}>{`{{${p}}}`}</Tag>
          ))
        )}
      </Space>
      <Input.TextArea
        value={content}
        onChange={(e) => {
          setContent(e.target.value)
          setDirty(true)
        }}
        rows={20}
        style={{ fontFamily: 'monospace', fontSize: 12 }}
      />
      <Space style={{ marginTop: 12 }} wrap>
        <Input
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          placeholder="变更备注（可选，记录到版本历史）"
          style={{ width: 320 }}
        />
        <Button
          type="primary"
          icon={<SaveOutlined />}
          loading={saving}
          disabled={!dirty}
          onClick={() => void save()}
        >
          保存新版本
        </Button>
        <Popconfirm
          title="重置为内置默认"
          description="将追加一个内容为内置默认的新版本，当前修改保留在历史中。"
          onConfirm={() => void reset()}
        >
          <Button icon={<UndoOutlined />} disabled={!detail.customized}>
            重置为内置默认
          </Button>
        </Popconfirm>
      </Space>
    </div>
  )

  return (
    <Drawer
      title={
        <Space>
          <span>{detail.name}</span>
          <Tag color={CATEGORY_COLOR[detail.category] ?? 'default'}>{detail.category}</Tag>
          <Typography.Text type="secondary" code>
            {detail.key}
          </Typography.Text>
        </Space>
      }
      open={open}
      loading={loading}
      onClose={onClose}
      width={820}
      destroyOnClose
    >
      <Typography.Paragraph type="secondary">{detail.description}</Typography.Paragraph>
      <Tabs
        activeKey={tab}
        onChange={(k) => onTabChange(k as 'edit' | 'history' | 'test')}
        items={[
          { key: 'edit', label: '编辑', children: editTab },
          {
            key: 'history',
            label: '版本历史',
            children: (
              <VersionHistory
                detail={detail}
                onRefresh={onRefresh}
                onSwitchEdit={() => onTabChange('edit')}
              />
            ),
          },
          {
            key: 'test',
            label: (
              <span>
                <ExperimentOutlined /> 测试验证
              </span>
            ),
            children: <TestPanel detail={detail} />,
          },
        ]}
      />
    </Drawer>
  )
}

// --------------------------------------------------------------------------- //
// 版本历史：查看 / 回滚 / 两版对比
// --------------------------------------------------------------------------- //

function VersionHistory({
  detail,
  onRefresh,
  onSwitchEdit,
}: {
  detail: PromptDetail
  onRefresh: (key: string) => Promise<PromptDetail>
  onSwitchEdit: () => void
}) {
  const { message } = AntdApp.useApp()
  const [diffOpen, setDiffOpen] = useState(false)
  const [diffLoading, setDiffLoading] = useState(false)
  const [diff, setDiff] = useState<PromptDiffResult | null>(null)
  // 对比选择：先点的为 v1，后点的为 v2
  const [picked, setPicked] = useState<number[]>([])

  const versions = [...detail.versions].sort((a, b) => b.version - a.version)

  const pick = (v: number) => {
    setPicked((prev) => {
      if (prev.includes(v)) return prev.filter((x) => x !== v)
      if (prev.length >= 2) return [prev[1], v]
      return [...prev, v]
    })
  }

  const runDiff = async () => {
    if (picked.length !== 2) return
    setDiffLoading(true)
    try {
      const [a, b] = picked
      const d = await api.diffPrompt(detail.key, Math.min(a, b), Math.max(a, b))
      setDiff(d)
      setDiffOpen(true)
    } catch (e) {
      message.error((e as Error).message)
    } finally {
      setDiffLoading(false)
    }
  }

  const rollback = async (v: number) => {
    try {
      await api.rollbackPrompt(detail.key, v)
      await onRefresh(detail.key)
      message.success(`已回滚到 v${v}（生成新版本 v${(await api.getPrompt(detail.key)).current_version}）`)
      onSwitchEdit()
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  return (
    <div>
      <Space style={{ marginBottom: 12 }} wrap>
        <Typography.Text type="secondary">
          点选两个版本进行对比{picked.length ? `（已选 v${picked.join('、v')}）` : ''}
        </Typography.Text>
        <Button
          icon={<DiffOutlined />}
          disabled={picked.length !== 2}
          loading={diffLoading}
          onClick={() => void runDiff()}
        >
          对比所选版本
        </Button>
      </Space>
      <Table
        rowKey="version"
        dataSource={versions}
        size="small"
        pagination={false}
        rowClassName={(r) => (r.version === detail.current_version ? 'current-version-row' : '')}
        columns={[
          {
            title: '',
            key: 'pick',
            width: 40,
            render: (_: unknown, r: PromptVersion) => (
              <Checkbox checked={picked.includes(r.version)} onChange={() => pick(r.version)} />
            ),
          },
          { title: '版本', key: 'version', width: 70, render: (_: unknown, r: PromptVersion) => <Tag>v{r.version}</Tag> },
          { title: '变更备注', dataIndex: 'comment', key: 'comment', ellipsis: true },
          { title: '操作人', dataIndex: 'updated_by', key: 'updated_by', width: 90 },
          {
            title: '时间',
            dataIndex: 'updated_at',
            key: 'updated_at',
            width: 160,
            render: (t: string) => formatTime(t),
          },
          {
            title: '操作',
            key: 'action',
            width: 150,
            render: (_: unknown, r: PromptVersion) => (
              <Space size={0}>
                {r.version !== detail.current_version && (
                  <Popconfirm
                    title={`回滚到 v${r.version}？`}
                    description="将复制该版本内容生成新版本并生效。"
                    onConfirm={() => void rollback(r.version)}
                  >
                    <Button size="small" type="link" icon={<RollbackOutlined />}>
                      回滚到此版
                    </Button>
                  </Popconfirm>
                )}
                {r.version === detail.current_version && <Tag color="green">当前版本</Tag>}
              </Space>
            ),
          },
        ] as never}
      />
      <Modal
        title={`版本对比：v${diff?.v1} → v${diff?.v2}`}
        open={diffOpen}
        onCancel={() => setDiffOpen(false)}
        footer={null}
        width={860}
      >
        {diff && (
          <>
            <Space style={{ marginBottom: 8 }}>
              <Tag color="green">+{diff.added} 行</Tag>
              <Tag color="red">-{diff.removed} 行</Tag>
            </Space>
            <pre className="diff-view">
              {diff.diff.map((line, i) => {
                const cls = line.startsWith('+') && !line.startsWith('+++')
                  ? 'diff-add'
                  : line.startsWith('-') && !line.startsWith('---')
                    ? 'diff-del'
                    : line.startsWith('@@')
                      ? 'diff-hunk'
                      : ''
                return (
                  <div key={i} className={cls}>
                    {line || ' '}
                  </div>
                )
              })}
            </pre>
          </>
        )}
      </Modal>
    </div>
  )
}

// --------------------------------------------------------------------------- //
// 测试验证：变量渲染 + 可选真实调用大模型
// --------------------------------------------------------------------------- //

function TestPanel({ detail }: { detail: PromptDetail }) {
  const { message } = AntdApp.useApp()
  const placeholders = useMemo(
    () => detail.placeholders ?? [],
    [detail.placeholders],
  )
  const [vars, setVars] = useState<Record<string, string>>({})
  const [sendToLlm, setSendToLlm] = useState(false)
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<{
    rendered: string
    unresolved: string[]
    llm_reply?: string
    llm_error?: string
  } | null>(null)

  const run = async () => {
    setRunning(true)
    setResult(null)
    try {
      const res = await api.testPrompt(detail.key, vars, sendToLlm)
      setResult({
        rendered: res.rendered,
        unresolved: res.unresolved_placeholders ?? [],
        llm_reply: res.llm_reply,
        llm_error: res.llm_error,
      })
    } catch (e) {
      message.error((e as Error).message)
    } finally {
      setRunning(false)
    }
  }

  return (
    <div>
      {placeholders.length === 0 ? (
        <Typography.Text type="secondary">
          该模板为静态提示词（无占位符），可直接渲染预览。
        </Typography.Text>
      ) : (
        <>
          <Typography.Paragraph type="secondary">
            填入各占位符的测试内容（模拟引擎注入的变量），验证渲染结果与模型效果：
          </Typography.Paragraph>
          {placeholders.map((p) => (
            <div key={p} style={{ marginBottom: 10 }}>
              <Typography.Text code style={{ fontFamily: 'monospace' }}>{`{{${p}}}`}</Typography.Text>
              {LARGE_VARS.has(p) ? (
                <Input.TextArea
                  rows={4}
                  value={vars[p] ?? ''}
                  onChange={(e) => setVars((v) => ({ ...v, [p]: e.target.value }))}
                  placeholder={`输入 ${p} 的测试内容`}
                  style={{ marginTop: 4, fontFamily: 'monospace', fontSize: 12 }}
                />
              ) : (
                <Input
                  value={vars[p] ?? ''}
                  onChange={(e) => setVars((v) => ({ ...v, [p]: e.target.value }))}
                  placeholder={`输入 ${p} 的测试内容`}
                  style={{ marginTop: 4 }}
                />
              )}
            </div>
          ))}
        </>
      )}
      <Space style={{ margin: '12px 0' }} wrap>
        <Button type="primary" loading={running} onClick={() => void run()}>
          {sendToLlm ? '渲染并调用大模型' : '渲染预览'}
        </Button>
        <Checkbox
          checked={sendToLlm}
          onChange={(e) => setSendToLlm(e.target.checked)}
        >
          同时真实调用大模型验证效果（较慢）
        </Checkbox>
      </Space>

      {result && (
        <div>
          {result.unresolved.length > 0 && (
            <Typography.Paragraph type="warning">
              未注入的占位符（保留原样）：{result.unresolved.map((p) => `{{${p}}}`).join('、')}
            </Typography.Paragraph>
          )}
          <Typography.Text type="secondary">渲染结果（{result.rendered.length} 字）：</Typography.Text>
          <pre className="prompt-render-view">{result.rendered}</pre>
          {result.llm_error && (
            <Typography.Paragraph type="danger">大模型调用失败：{result.llm_error}</Typography.Paragraph>
          )}
          {result.llm_reply !== undefined && (
            <>
              <Typography.Text type="secondary">大模型回复：</Typography.Text>
              <pre className="prompt-render-view">{result.llm_reply || '（空回复）'}</pre>
            </>
          )}
        </div>
      )}
      {!result && !placeholders.length && (
        <Empty description="点击「渲染预览」查看当前模板内容" style={{ marginTop: 24 }} />
      )}
    </div>
  )
}
