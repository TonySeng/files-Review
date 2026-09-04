import { useEffect, useRef, type ReactNode } from 'react'
import { Card, Progress, Spin, Tag } from 'antd'
import type { LogEntry } from '../types'

interface Props {
  logs: LogEntry[]
  progress: number
  running: boolean
  /** 知识库检索次数（仅审核过程有，法规解析传 0 即不展示该标签）。 */
  kbCount?: number
  /** 卡片标题，默认「审核过程」；法规解析复用时传「法规解析过程」。 */
  title?: string
  /** 标题右侧的附加信息（如耗时、块数等统计标签）。 */
  extra?: ReactNode
  /** 无日志时的加载提示语。 */
  emptyTip?: string
}

const LEVEL_CLASS: Record<LogEntry['level'], string> = {
  info: '',
  kb: 'log-line-kb',
  warn: 'log-line-warn',
  error: 'log-line-err',
  ok: 'log-line-ok',
}

export default function ProgressPanel({
  logs,
  progress,
  running,
  kbCount = 0,
  title = '审核过程',
  extra,
  emptyTip = '正在连接审核服务并解析文件…',
}: Props) {
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (running) endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [logs.length, running])

  if (!logs.length) {
    if (running) {
      return (
        <Card size="small" title={title}>
          <div style={{ textAlign: 'center', padding: '28px 0' }}>
            <Spin tip={emptyTip} />
          </div>
        </Card>
      )
    }
    return null
  }

  return (
    <Card
      size="small"
      title={title}
      extra={
        <span style={{ fontSize: 12 }}>
          {extra}
          {kbCount > 0 && <Tag color="blue">知识库检索 {kbCount} 次</Tag>}
          <Tag color={running ? 'processing' : 'success'}>
            {running ? '进行中' : '已完成'}
          </Tag>
        </span>
      }
    >
      <Progress
        percent={progress}
        status={running ? 'active' : 'success'}
        size="small"
        style={{ marginBottom: 10 }}
      />
      <div className="log-panel">
        {logs.map((log, idx) => (
          <div key={idx} className={LEVEL_CLASS[log.level]}>
            <span style={{ opacity: 0.5 }}>{log.time} </span>
            {log.text}
          </div>
        ))}
        <div ref={endRef} />
      </div>
    </Card>
  )
}
