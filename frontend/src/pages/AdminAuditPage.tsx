import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  App as AntdApp,
  Button,
  Card,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
} from 'antd'
import { DownloadOutlined, ReloadOutlined } from '@ant-design/icons'
import { api } from '../services/api'
import type {
  AuditCallRecord,
  FeedbackRecord,
  ReviewTaskSummary,
  User,
} from '../types'

interface Props {
  onBack: () => void
}

export default function AdminAuditPage({ onBack }: Props) {
  const { message } = AntdApp.useApp()
  const [users, setUsers] = useState<User[]>([])
  const [filterUserId, setFilterUserId] = useState<string | undefined>(undefined)

  const [calls, setCalls] = useState<AuditCallRecord[]>([])
  const [tasks, setTasks] = useState<ReviewTaskSummary[]>([])
  const [feedbacks, setFeedbacks] = useState<FeedbackRecord[]>([])
  const [loading, setLoading] = useState(false)

  const userMap = useMemo(
    () => Object.fromEntries(users.map((u) => [u.id, u.display_name || u.username])),
    [users],
  )

  const loadUsers = useCallback(async () => {
    try {
      const users = await api.listUsers()
      setUsers(users)
    } catch {
      /* 忽略 */
    }
  }, [])

  useEffect(() => {
    void loadUsers()
  }, [loadUsers])

  const loadAll = useCallback(async () => {
    setLoading(true)
    try {
      const [c, t, f] = await Promise.all([
        api.listAuditCalls({ user_id: filterUserId, since_hours: 72 }).catch(() => ({
          items: [] as AuditCallRecord[],
        })),
        api.listAdminTasks(filterUserId).catch(() => ({ tasks: [] as ReviewTaskSummary[] })),
        api
          .listAdminFeedback({ user_id: filterUserId, limit: 200 })
          .catch(() => ({ records: [] as FeedbackRecord[] })),
      ])
      setCalls(c.items || [])
      setTasks(t.tasks || [])
      setFeedbacks(f.records || [])
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setLoading(false)
    }
  }, [filterUserId, message])

  useEffect(() => {
    void loadAll()
  }, [loadAll])

  const userFilter = (
    <Select
      allowClear
      placeholder="全部用户"
      style={{ width: 200 }}
      value={filterUserId}
      onChange={(v) => setFilterUserId(v || undefined)}
      options={users.map((u) => ({
        value: u.id,
        label: `${u.display_name || u.username}（${u.role === 'admin' ? '管理员' : '用户'}）`,
      }))}
    />
  )

  const callColumns = [
    {
      title: '时间',
      dataIndex: 'ts',
      key: 'ts',
      render: (ts: number) => new Date(ts * 1000).toLocaleString(),
    },
    {
      title: '用户',
      dataIndex: 'user_id',
      key: 'user_id',
      render: (id: string | null, r: AuditCallRecord) =>
        id ? (
          <span>
            {userMap[id] || id}{' '}
            <Tag color={r.role === 'admin' ? 'gold' : 'blue'}>{r.role}</Tag>
          </span>
        ) : (
          <Tag>未鉴权</Tag>
        ),
    },
    { title: '方法', dataIndex: 'method', key: 'method', width: 70 },
    { title: '路径', dataIndex: 'path', key: 'path' },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      render: (s: number) => <Tag color={s >= 500 ? 'error' : s >= 400 ? 'warning' : 'success'}>{s}</Tag>,
    },
    {
      title: '耗时',
      dataIndex: 'duration_ms',
      key: 'duration_ms',
      render: (ms: number) => `${Math.round(ms)} ms`,
    },
    {
      title: '错误',
      dataIndex: 'has_error',
      key: 'has_error',
      render: (e: boolean) => (e ? <Tag color="error">是</Tag> : <Tag>否</Tag>),
    },
    { title: '客户端', dataIndex: 'client', key: 'client' },
  ]

  const taskColumns = [
    {
      title: '提交时间',
      key: 'created_at',
      render: (_: unknown, t: ReviewTaskSummary) => t.created_at || '-',
    },
    { title: '模式', dataIndex: 'mode', key: 'mode', width: 80 },
    {
      title: '用户',
      dataIndex: 'user_id',
      key: 'user_id',
      render: (id: string | null) =>
        id ? <span>{userMap[id] || id}</span> : <Tag color="default">系统</Tag>,
    },
    {
      title: '文件',
      dataIndex: 'file_names',
      key: 'file_names',
      render: (n: string[]) => (n || []).join('、').slice(0, 60) || '-',
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      render: (s: string) => (
        <Tag
          color={
            s === 'completed' ? 'success' : s === 'failed' ? 'error' : s === 'running' ? 'processing' : 'default'
          }
        >
          {s}
        </Tag>
      ),
    },
    { title: '规则数', dataIndex: 'rule_count', key: 'rule_count', width: 70 },
    {
      title: '得分',
      dataIndex: 'score',
      key: 'score',
      render: (s: number | null) => (s == null ? '-' : s),
    },
  ]

  const fbColumns = [
    {
      title: '时间',
      dataIndex: 'created_at',
      key: 'created_at',
    },
    {
      title: '用户',
      dataIndex: 'user_id',
      key: 'user_id',
      render: (id: string | null) =>
        id ? <span>{userMap[id] || id}</span> : <Tag color="default">未知</Tag>,
    },
    { title: '规则', dataIndex: 'rule_name', key: 'rule_name' },
    {
      title: '判定',
      dataIndex: 'judgment',
      key: 'judgment',
      render: (j: string) =>
        j === 'adopt' ? <Tag color="success">采纳</Tag> : <Tag color="error">不采纳</Tag>,
    },
    { title: '标题', dataIndex: 'title', key: 'title', render: (t: string) => (t || '').slice(0, 40) },
    {
      title: '错别字',
      dataIndex: 'is_typo',
      key: 'is_typo',
      render: (b: boolean) => (b ? <Tag>是</Tag> : null),
    },
  ]

  const handleExportFeedback = async () => {
    try {
      const resp = await api.exportAdminFeedback({ user_id: filterUserId })
      if (!resp.ok) throw new Error(`导出失败 (${resp.status})`)
      const blob = await resp.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = 'feedback_training_data.jsonl'
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } catch (err) {
      message.error((err as Error).message)
    }
  }

  return (
    <div style={{ padding: 16 }}>
      <Space className="page-sticky-row" style={{ marginBottom: 12 }}>
        <Button onClick={onBack}>返回</Button>
        <Typography.Text strong>按用户筛选：</Typography.Text>
        {userFilter}
        <Button icon={<ReloadOutlined />} onClick={() => void loadAll()}>
          刷新
        </Button>
      </Space>

      <Tabs
        defaultActiveKey="calls"
        items={[
          {
            key: 'calls',
            label: '调用审计日志',
            children: (
              <Card size="small" title={`调用记录（近 72 小时）共 ${calls.length} 条`}>
                <Table
                  rowKey={(r) => r.req_id || `${r.ts}-${r.path}`}
                  loading={loading}
                  dataSource={calls}
                  columns={callColumns}
                  pagination={{ pageSize: 15 }}
                  size="small"
                />
              </Card>
            ),
          },
          {
            key: 'tasks',
            label: '审核任务',
            children: (
              <Card size="small" title={`审核任务共 ${tasks.length} 条`}>
                <Table
                  rowKey="task_id"
                  loading={loading}
                  dataSource={tasks}
                  columns={taskColumns}
                  pagination={{ pageSize: 15 }}
                  size="small"
                />
              </Card>
            ),
          },
          {
            key: 'feedback',
            label: '反馈与训练数据',
            children: (
              <Card
                size="small"
                title={`反馈记录共 ${feedbacks.length} 条`}
                extra={
                  <Button icon={<DownloadOutlined />} onClick={handleExportFeedback}>
                    导出 JSONL
                  </Button>
                }
              >
                <Table
                  rowKey="id"
                  loading={loading}
                  dataSource={feedbacks}
                  columns={fbColumns}
                  pagination={{ pageSize: 15 }}
                  size="small"
                />
              </Card>
            ),
          },
        ]}
      />
    </div>
  )
}
