import { Button, Drawer, Empty, Space, Tag, Typography } from 'antd'
import { CopyOutlined, EditOutlined } from '@ant-design/icons'
import type { Rule } from '../types'
import { categoryLabel, severityMeta } from './ruleMeta'

interface Props {
  open: boolean
  rule: Rule | null
  isPreset: boolean
  onClose: () => void
  onEdit?: (rule: Rule) => void
  onClone?: () => void
}

export default function RuleDetailDrawer({
  open,
  rule,
  isPreset,
  onClose,
  onEdit,
  onClone,
}: Props) {
  return (
    <Drawer
      title="规则详情"
      width={560}
      open={open}
      onClose={onClose}
      styles={{ body: { paddingTop: 12 } }}
    >
      {!rule ? (
        <Empty description="未选择规则" />
      ) : (
        <Space direction="vertical" size={18} style={{ width: '100%' }}>
          <div>
            <div className="rd-title">{rule.name}</div>
            <Space size={6} wrap style={{ marginTop: 10 }}>
              <Tag color={severityMeta(rule.severity).color}>
                {severityMeta(rule.severity).label}
              </Tag>
              <Tag>{categoryLabel(rule.category)}</Tag>
              <Tag color={rule.enabled ? 'success' : 'default'}>
                {rule.enabled ? '启用' : '停用'}
              </Tag>
              <Tag color={rule.need_legal_basis ? 'blue' : 'default'}>
                {rule.need_legal_basis ? '需法规依据' : '无需法规检索'}
              </Tag>
              {rule.builtin !== undefined && (
                <Tag color={rule.builtin ? 'gold' : 'geekblue'}>
                  {rule.builtin ? '系统预置' : '自定义'}
                </Tag>
              )}
            </Space>
          </div>

          <div>
            <div className="rd-label">规则说明（判定条件）</div>
            <div className="rule-detail-block">
              {rule.description || (
                <Typography.Text type="secondary">（无说明）</Typography.Text>
              )}
            </div>
          </div>

          <div>
            <div className="rd-label">审核要点</div>
            <div className="rule-detail-block">
              {rule.checkpoints.length > 0 ? (
                <ol className="rule-detail-checkpoints">
                  {rule.checkpoints.map((c, i) => (
                    <li key={i}>{c}</li>
                  ))}
                </ol>
              ) : (
                <Typography.Text type="secondary">（无审核要点）</Typography.Text>
              )}
            </div>
          </div>

          <div className="rd-footer">
            {isPreset ? (
              <Button block icon={<CopyOutlined />} onClick={() => onClone?.()}>
                复制为自定义规则集
              </Button>
            ) : (
              <Button
                block
                type="primary"
                icon={<EditOutlined />}
                onClick={() => onEdit?.(rule)}
              >
                编辑该规则
              </Button>
            )}
          </div>
        </Space>
      )}
    </Drawer>
  )
}
