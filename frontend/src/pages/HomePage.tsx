import { useMemo } from 'react'
import {
  Badge,
  Button,
  Card,
  Empty,
  Popconfirm,
  Progress,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  DeleteOutlined,
  FileTextOutlined,
  PlusOutlined,
  RightOutlined,
  StopOutlined,
} from '@ant-design/icons'
import type { ReviewTaskSummary, TaskStatus } from '../types'


const STATUS_META: Record<TaskStatus, { label: string; color: string }> = {
  pending: { label: '排队中', color: 'blue' },
  running: { label: '进行中', color: 'processing' },
  completed: { label: '已完成', color: 'success' },
  failed: { label: '失败', color: 'error' },
  cancelled: { label: '已取消', color: 'default' },
}

const TERMINAL: TaskStatus[] = ['completed', 'failed', 'cancelled']

/** 将起止 epoch（秒）格式化为中文时长；进行中任务用当前时间实时计算，已结束任务用 finished_at_ts 固定值。 */
function formatDuration(
  startTs: number | null | undefined,
  endTs: number | null | undefined,
): string | null {
  if (startTs == null) return null
  const s = Number(startTs) * 1000
  const e = endTs != null ? Number(endTs) * 1000 : Date.now()
  const sec = Math.max(0, Math.floor((e - s) / 1000))
  if (sec < 60) return `${sec}秒`
  const m = Math.floor(sec / 60)
  const rs = sec % 60
  if (m < 60) return rs ? `${m}分${rs}秒` : `${m}分钟`
  const h = Math.floor(m / 60)
  const rm = m % 60
  return rm ? `${h}小时${rm}分` : `${h}小时`
}

/** 将 epoch（秒）按浏览器本地时区格式化为 YYYY-MM-DD HH:MM:SS。 */
function formatLocalTime(ts: number | null | undefined): string {
  if (ts == null) return '—'
  const d = new Date(Number(ts) * 1000)
  if (Number.isNaN(d.getTime())) return '—'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(
    d.getHours(),
  )}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

interface Props {
  tasks: ReviewTaskSummary[]
  onOpen: (id: string) => void
  onNew: () => void
  onCancel: (id: string) => void
  onRemove: (id: string) => void
  onClear: () => void
}

/**
 * 系统主界面（默认视图）：历史审核任务列表。
 * 点击任务进入详情页；右上角「新建审核」进入向导式创建流程。
 */
export default function HomePage({
  tasks,
  onOpen,
  onNew,
  onCancel,
  onRemove,
  onClear,
}: Props) {
  const stats = useMemo(() => {
    const running = tasks.filter((t) => !TERMINAL.includes(t.status)).length
    const done = tasks.filter((t) => t.status === 'completed')
    const avg =
      done.length > 0
        ? Math.round(done.reduce((s, t) => s + (t.score ?? 0), 0) / done.length)
        : null
    return { total: tasks.length, running, done: done.length, avg }
  }, [tasks])

  const columns: ColumnsType<ReviewTaskSummary> = [
    {
      title: '状态',
      dataIndex: 'status',
      width: 96,
      filters: Object.entries(STATUS_META).map(([k, v]) => ({ text: v.label, value: k })),
      onFilter: (value, row) => row.status === value,
      render: (status: TaskStatus) => (
        <Tag color={STATUS_META[status].color}>{STATUS_META[status].label}</Tag>
      ),
    },
    {
      title: '审核文件',
      dataIndex: 'file_names',
      render: (names: string[]) => (
        <Space size={4} wrap>
          <FileTextOutlined style={{ color: '#8f959e' }} />
          <span style={{ fontSize: 13 }}>{names.join('、') || '（无文件）'}</span>
        </Space>
      ),
    },
    {
      title: '规则数',
      dataIndex: 'rule_count',
      width: 80,
      align: 'center',
      render: (n: number) => <Typography.Text>{n}</Typography.Text>,
    },
    {
      title: '合规得分',
      dataIndex: 'score',
      width: 100,
      align: 'center',
      sorter: (a, b) => (a.score ?? -1) - (b.score ?? -1),
      render: (score: number | null) =>
        score == null ? (
          <Typography.Text type="secondary">—</Typography.Text>
        ) : (
          <Tag color={score >= 85 ? 'success' : score >= 60 ? 'warning' : 'error'}>
            {score}
          </Tag>
        ),
    },
    {
      title: '进度',
      dataIndex: 'progress',
      width: 140,
      render: (progress: number, row) => {
        const isRunning = !TERMINAL.includes(row.status)
        return (
          <Progress
            percent={progress}
            size="small"
            status={isRunning ? 'active' : row.status === 'failed' ? 'exception' : 'success'}
            showInfo={false}
          />
        )
      },
    },
    {
      title: '创建时间',
      dataIndex: 'created_at_ts',
      width: 170,
      render: (_: unknown, row: ReviewTaskSummary) => (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {formatLocalTime(row.created_at_ts)}
        </Typography.Text>
      ),
    },
    {
      title: '执行时长',
      key: 'duration',
      width: 110,
      align: 'center',
      render: (_: unknown, row: ReviewTaskSummary) => {
        const running = !TERMINAL.includes(row.status)
        let d: string | null
        if (running) {
          // 进行中：基于本地时区的 epoch 实时估算（列表每 2.5s 轮询自动刷新）
          d = formatDuration(row.created_at_ts, null)
        } else if (row.finished_at_ts != null) {
          // 已结束：用结束时间固定值（时基与创建时间一致，差值稳定）
          d = formatDuration(row.created_at_ts, row.finished_at_ts)
        } else {
          // 终态但缺失结束时间（如任务被后台重启中断未落盘）：无法计算，不显示错误数值
          d = null
        }
        return d ? (
          <Typography.Text style={{ fontSize: 13 }}>{d}</Typography.Text>
        ) : (
          <Typography.Text type="secondary">—</Typography.Text>
        )
      },
    },
    {
      title: '操作',
      key: 'actions',
      width: 150,
      fixed: 'right',
      render: (_, row) => {
        const isRunning = !TERMINAL.includes(row.status)
        return (
          // 阻止操作列内部任何点击（含 Popconfirm 确认按钮）冒泡到表格行，
          // 避免删除/取消时意外触发 onOpen 跳转详情。
          <Space size={4} onClick={(e) => e.stopPropagation()}>
            <Button
              type="link"
              size="small"
              onClick={(e) => {
                e.stopPropagation()
                // eslint-disable-next-line no-console
                console.log('[HomePage] onOpen', row.task_id)
                onOpen(row.task_id)
              }}
            >
              查看 <RightOutlined style={{ fontSize: 10 }} />
            </Button>
            {isRunning ? (
              <Popconfirm
                title="取消该审核任务？"
                description="任务将停止执行，已产生的结果不会保留"
                okText="取消任务"
                cancelText="不取消"
                okButtonProps={{ danger: true }}
                onConfirm={() => onCancel(row.task_id)}
              >
                <Button
                  type="link"
                  size="small"
                  danger
                  icon={<StopOutlined />}
                  onClick={(e) => e.stopPropagation()}
                >
                  取消
                </Button>
              </Popconfirm>
            ) : (
              <Popconfirm
                title="删除该任务记录？"
                okText="删除"
                cancelText="取消"
                okButtonProps={{ danger: true }}
                onConfirm={() => {
                  // eslint-disable-next-line no-console
                  console.log('[HomePage] onRemove confirm', row.task_id)
                  onRemove(row.task_id)
                }}
              >
                <Button
                  type="link"
                  size="small"
                  danger
                  icon={<DeleteOutlined />}
                  onClick={(e) => e.stopPropagation()}
                >
                  删除
                </Button>
              </Popconfirm>
            )}
          </Space>
        )
      },
    },
  ]

  return (
    <div className="home-page">
      <Card
        size="small"
        className="home-summary-card"
        styles={{ body: { padding: '16px 20px' } }}
      >
        <div className="home-summary">
          <div>
            <Typography.Title level={4} style={{ margin: 0 }}>
              审核任务
            </Typography.Title>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              文件合规审核全流程管理
            </Typography.Text>
          </div>
          <Space size={28} className="home-stat-group">
            <Statistic title="任务总数" value={stats.total} />
            <Statistic title="进行中" value={stats.running} valueStyle={{ color: '#1668dc' }} />
            <Statistic title="已完成" value={stats.done} />
            <Statistic
              title="平均得分"
              value={stats.avg ?? '—'}
              valueStyle={{ color: stats.avg != null && stats.avg >= 85 ? '#52c41a' : '#fa8c16' }}
            />
          </Space>
          <Button type="primary" icon={<PlusOutlined />} size="large" onClick={onNew}>
            新建审核任务
          </Button>
        </div>
      </Card>

      <Card size="small" className="home-list-card" title={
        <Space size={6}>
          <span>历史任务</span>
          <Badge count={tasks.length} showZero style={{ backgroundColor: '#1668dc' }} />
        </Space>
      } extra={
        tasks.length > 0 && (
          <Popconfirm
            title="清空全部任务历史？"
            description="此操作不可恢复"
            okText="清空"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            onConfirm={onClear}
          >
            <Button size="small" type="text">
              清空历史
            </Button>
          </Popconfirm>
        )
      }>
        {tasks.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={
              <span>
                暂无审核任务，点击右侧「新建审核任务」开始
                <Typography.Text strong> 向导式 </Typography.Text>
                创建
              </span>
            }
          >
            <Button type="primary" icon={<PlusOutlined />} onClick={onNew}>
              新建审核
            </Button>
          </Empty>
        ) : (
          <Table
            rowKey="task_id"
            size="small"
            columns={columns}
            dataSource={tasks}
            pagination={{ pageSize: 10, hideOnSinglePage: true }}
            scroll={{ x: 1040 }}
            onRow={(record) => ({
              onClick: () => onOpen(record.task_id),
              style: { cursor: 'pointer' },
            })}
          />
        )}
      </Card>
    </div>
  )
}
