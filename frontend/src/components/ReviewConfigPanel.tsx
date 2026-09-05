import {
  Alert,
  Button,
  Card,
  Input,
  Select,
  Space,
  Switch,
  Tooltip,
  Typography,
} from 'antd'
import { PlayCircleOutlined } from '@ant-design/icons'
import type {
  KnowledgeBase,
} from '../types'

interface Props {
  kbEnabled: boolean
  onKbEnabledChange: (v: boolean) => void
  knowledgeBases: KnowledgeBase[]
  kbId: string
  onKbIdChange: (v: string) => void
  webSearchEnabled: boolean
  onWebSearchEnabledChange: (v: boolean) => void
  webSearchConfigured: boolean
  /** 任务级缓存模式：default=跟随系统配置；on=本任务强制启用；off=本任务强制禁用 */
  cacheMode: 'default' | 'on' | 'off'
  onCacheModeChange: (v: 'default' | 'on' | 'off') => void
  extraInstruction: string
  onExtraInstructionChange: (v: string) => void
  canStart: boolean
  onStart: () => void
  running: boolean
  selectedFileNames: string
  /** 向导/详情页中作为纯配置表单时隐藏「开始审核」按钮，由外层控制提交 */
  hideStartButton?: boolean
}

/**
 * 右侧审核配置：原「审核选项」卡片迁移至此，置于结果区上方、开始审核按钮之上，
 * 与左侧「配置与列表」解耦，符合大厂「操作区与结果区分离」的布局规范。
 */
export default function ReviewConfigPanel({
  kbEnabled,
  onKbEnabledChange,
  knowledgeBases,
  kbId,
  onKbIdChange,
  webSearchEnabled,
  onWebSearchEnabledChange,
  webSearchConfigured,
  cacheMode,
  onCacheModeChange,
  extraInstruction,
  onExtraInstructionChange,
  canStart,
  onStart,
  running,
  selectedFileNames,
  hideStartButton,
}: Props) {
  // 知识库是否可用 = 是否已连接到提供了知识库列表的服务（与系统是否预设默认 kb_id 无关）
  const kbAvailable = knowledgeBases.length > 0
  return (
    <Card
      size="small"
      title="审核配置"
      className="review-config-card"
    >
      <Space direction="vertical" size={14} style={{ width: '100%' }}>
        <div className="rc-row rc-row--switch">
          <Switch
            checked={kbEnabled}
            disabled={!kbAvailable}
            onChange={onKbEnabledChange}
          />
          <Typography.Text className="rc-text">启用知识库检索</Typography.Text>
          <Tooltip title="开启后，模型会自主判断哪些规则需要法规依据，并检索知识库获取条文原文">
            <Typography.Text type="secondary" className="rc-tip">
              （模型自主决策）
            </Typography.Text>
          </Tooltip>
        </div>

        <div className="rc-row rc-row--block">
          <Typography.Text className="rc-label rc-label--block">
            关联检索知识库（本任务）
          </Typography.Text>
          <Select
            style={{ width: '100%' }}
            size="small"
            allowClear
            placeholder={
              knowledgeBases.length ? '选择本次审核关联的知识库' : '暂无可用知识库'
            }
            value={kbId || undefined}
            disabled={!kbEnabled || !kbAvailable}
            onChange={(v) => onKbIdChange(v ?? '')}
            options={knowledgeBases.map((kb) => ({
              value: kb.id,
              label: `${kb.name}（${kb.document_count} 篇文档）`,
            }))}
          />
          <Typography.Text type="secondary" className="rc-tip rc-tip--block">
            留空则使用系统配置中默认的知识库
          </Typography.Text>
        </div>

        {!kbAvailable && (
          <Alert
            type="warning"
            showIcon
            className="rc-alert"
            message="未连接知识库服务"
            description="请在「服务配置」中确认知识库服务已连接，否则无法检索知识库。"
          />
        )}

        <div className="rc-row rc-row--switch">
          <Switch
            checked={webSearchEnabled}
            disabled={!webSearchConfigured}
            onChange={onWebSearchEnabledChange}
          />
          <Typography.Text className="rc-text">启用联网搜索</Typography.Text>
          <Tooltip title="开启后，模型可以搜索最新政策、行业动态等实时信息（需在配置中设置API密钥）">
            <Typography.Text type="secondary" className="rc-tip">
              （获取实时信息）
            </Typography.Text>
          </Tooltip>
        </div>
        {!webSearchConfigured && webSearchEnabled && (
          <Alert
            type="warning"
            showIcon
            className="rc-alert"
            message="未配置联网搜索"
            description="请在「服务配置」中配置联网搜索API密钥。"
          />
        )}

        <div className="rc-row rc-row--block">
          <Typography.Text className="rc-label rc-label--block">
            缓存（本任务独立控制）
          </Typography.Text>
          <Select
            style={{ width: '100%' }}
            size="small"
            value={cacheMode}
            onChange={onCacheModeChange}
            options={[
              { value: 'default', label: '跟随系统配置（默认）' },
              { value: 'on', label: '本任务强制启用缓存' },
              { value: 'off', label: '本任务强制禁用缓存' },
            ]}
          />
          <Typography.Text type="secondary" className="rc-tip rc-tip--block">
            控制「结论缓存」与「一致性摘要缓存」：禁用后本任务每次都全量调用模型，
            不复用历史结论，也不写入缓存
          </Typography.Text>
        </div>

        <Input.TextArea
          rows={2}
          value={extraInstruction}
          onChange={(e) => onExtraInstructionChange(e.target.value)}
          placeholder="补充审核要求（选填），例如：重点核查环保资质与节能认证"
        />

        {!hideStartButton && (
          <Tooltip
            title={
              !canStart
                ? '请先在左侧选择待审文件，并至少选择一条审核规则或一个审核规则组'
                : ''
            }
          >
            <span style={{ display: 'block', width: '100%' }}>
              <Button
                type="primary"
                icon={<PlayCircleOutlined />}
                disabled={!canStart}
                onClick={onStart}
                block
              >
                开始审核（后台执行）
              </Button>
            </span>
          </Tooltip>
        )}

        {running && !hideStartButton && (
          <Alert
            type="info"
            showIcon
            className="rc-alert"
            message="审核任务在后台执行中，您可继续操作或切换到「历史任务」查看进度。"
          />
        )}
        {selectedFileNames && (
          <Typography.Text type="secondary" className="rc-tip rc-tip--block">
            待审：{selectedFileNames}
          </Typography.Text>
        )}
      </Space>
    </Card>
  )
}
