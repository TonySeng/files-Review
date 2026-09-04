import { useState } from 'react'
import {
  App as AntdApp,
  Badge,
  Button,
  Card,
  Drawer,
  Empty,
  List,
  Popconfirm,
  Progress,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import {
  DeleteOutlined,
  EyeOutlined,
  ClearOutlined,
  FileTextOutlined,
} from '@ant-design/icons'
import { api } from '../services/api'
import type { ReviewTaskDetail, ReviewTaskSummary, TaskStatus } from '../types'
import ProgressPanel from './ProgressPanel'
import ResultPanel from './ResultPanel'

const STATUS_META: Record<
  TaskStatus,
  { label: string; color: string }
> = {
  pending: { label: '排队中', color: 'blue' },
  running: { label: '进行中', color: 'processing' },
  completed: { label: '已完成', color: 'success' },
  failed: { label: '失败', color: 'error' },
  cancelled: { label: '已取消', color: 'default' },
}

const TERMINAL: TaskStatus[] = ['completed', 'failed', 'cancelled']

interface Props {
  tasks: ReviewTaskSummary[]
  onSelect: (id: string) => void
  onCancel: (id: string) => void
  onRemove: (id: string) => void
  onClear: () => void
}

export default function HistoryPanel({ tasks, onSelect, onCancel, onRemove, onClear }: Props) {
  const { message } = AntdApp.useApp()
  const [detailId, setDetailId] = useState<string | null>(null)
  const [detail, setDetail] = useState<ReviewTaskDetail | null>(null)
  const [loadingDetail, setLoadingDetail] = useState(false)

  const openDetail = async (id: string) => {
    setDetailId(id)
    setLoadingDetail(true)
    setDetail(null)
    try {
      setDetail(await api.getTask(id))
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setLoadingDetail(false)
    }
  }

  const running = detail ? !TERMINAL.includes(detail.status) : false

  return (
    <Card
      size="small"
      title={
        <Space size={6}>
          <span>历史任务</span>
          <Badge count={tasks.length} showZero style={{ backgroundColor: '#1668dc' }} />
        </Space>
      }
      extra={
        tasks.length > 0 && (
          <Popconfirm
            title="清空全部任务历史？"
            description="此操作不可恢复"
            okText="清空"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            onConfirm={onClear}
          >
            <Button size="small" type="text" icon={<ClearOutlined />}>
              清空
            </Button>
          </Popconfirm>
        )
      }
    >
      {tasks.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="暂无审核任务，提交后会出现在这里"
        />
      ) : (
        <List
          size="small"
          dataSource={tasks}
          renderItem={(task) => {
            const meta = STATUS_META[task.status]
            const isRunning = !TERMINAL.includes(task.status)
            return (
              <List.Item
                className="task-item"
                onClick={() => openDetail(task.task_id)}
                actions={[
                  <Tooltip key="view" title="查看详情">
                    <Button
                      type="text"
                      size="small"
                      icon={<EyeOutlined />}
                      onClick={(e) => {
                        e.stopPropagation()
                        openDetail(task.task_id)
                      }}
                    />
                  </Tooltip>,
                  <Button
                    key="current"
                    type="link"
                    size="small"
                    onClick={(e) => {
                      e.stopPropagation()
                      onSelect(task.task_id)
                    }}
                  >
                    右侧查看
                  </Button>,
                  isRunning ? (
                    <Popconfirm
                      key="cancel"
                      title="取消该审核任务？"
                      description="任务将停止执行，已产生的结果不会保留"
                      okText="取消任务"
                      cancelText="不取消"
                      okButtonProps={{ danger: true }}
                      onCancel={(e) => e?.stopPropagation?.()}
                      onConfirm={() => onCancel(task.task_id)}
                    >
                      <Button
                        type="link"
                        size="small"
                        danger
                        onClick={(e) => e.stopPropagation()}
                      >
                        取消
                      </Button>
                    </Popconfirm>
                  ) : (
                    <Popconfirm
                      key="del"
                      title="删除该任务记录？"
                      okText="删除"
                      cancelText="取消"
                      okButtonProps={{ danger: true }}
                      onConfirm={() => onRemove(task.task_id)}
                    >
                      <Button
                        type="text"
                        size="small"
                        danger
                        icon={<DeleteOutlined />}
                        onClick={(e) => e.stopPropagation()}
                      />
                    </Popconfirm>
                  ),
                ]}
              >
                <List.Item.Meta
                  avatar={
                    <Tag color={meta.color} style={{ marginTop: 4 }}>
                      {meta.label}
                    </Tag>
                  }
                  title={
                    <Space size={6} wrap>
                      {task.score != null && (
                        <Tag color={task.score >= 85 ? 'success' : task.score >= 60 ? 'warning' : 'error'}>
                          得分 {task.score}
                        </Tag>
                      )}
                    </Space>
                  }
                  description={
                    <div className="task-item-desc">
                      <div>
                        <FileTextOutlined /> {task.file_names.join('、') || '（无文件）'}
                      </div>
                      <div className="upload-hint">
                        {task.created_at || '—'} · {task.rule_count} 条规则
                        {task.error ? ` · 错误：${task.error}` : ''}
                      </div>
                      <Progress
                        percent={task.progress}
                        size="small"
                        status={isRunning ? 'active' : task.status === 'failed' ? 'exception' : 'success'}
                        showInfo={false}
                      />
                    </div>
                  }
                />
              </List.Item>
            )
          }}
        />
      )}

      <Drawer
        title="任务详情"
        width={Math.min(720, typeof window !== 'undefined' ? window.innerWidth - 40 : 720)}
        open={!!detailId}
        onClose={() => setDetailId(null)}
        destroyOnClose
      >
        {loadingDetail ? (
          <div style={{ textAlign: 'center', padding: '40px 0' }}>
            <Spin tip="加载任务详情…" />
          </div>
        ) : detail ? (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Space size={8} wrap>
              <Tag color={STATUS_META[detail.status].color}>
                {STATUS_META[detail.status].label}
              </Tag>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                创建：{detail.created_at || '—'}
              </Typography.Text>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                结束：{detail.finished_at || '进行中'}
              </Typography.Text>
            </Space>
            <div className="upload-hint">
              <div>文件：{detail.file_names.join('、') || '（无）'}</div>
              <div>规则数：{detail.rule_count}</div>
              {detail.request?.extra_instruction && (
                <div>附加要求：{detail.request.extra_instruction}</div>
              )}
            </div>
            {running && (
              <ProgressPanel
                logs={detail.logs}
                progress={detail.progress}
                running={running}
                kbCount={detail.kb_traces.length}
              />
            )}
            <ResultPanel
              findings={detail.findings}
              issues={detail.consistency_issues}
              kbTraces={detail.kb_traces}
              summary={detail.summary}
              running={running}
            />
            {!running && detail.logs.length > 0 && (
              <ProgressPanel
                logs={detail.logs}
                progress={detail.progress}
                running={false}
                kbCount={detail.kb_traces.length}
              />
            )}
          </Space>
        ) : (
          <Empty description="未找到任务详情" />
        )}
      </Drawer>
    </Card>
  )
}
