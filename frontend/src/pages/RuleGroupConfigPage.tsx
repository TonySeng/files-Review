import { useCallback, useEffect, useState } from 'react'
import {
  App as AntdApp,
  Button,
  Card,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
} from 'antd'
import { ArrowLeftOutlined, PlusOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { api } from '../services/api'
import type { Rule, RuleGroup, RuleSet } from '../types'

interface Props {
  ruleSets: RuleSet[]
  onBack: () => void
  onChanged: () => void
}

export default function RuleGroupConfigPage({ ruleSets, onBack, onChanged }: Props) {
  const { message } = AntdApp.useApp()
  const [groups, setGroups] = useState<RuleGroup[]>([])
  const [rules, setRules] = useState<Rule[]>([])
  const [loading, setLoading] = useState(false)
  const [editing, setEditing] = useState<RuleGroup | null>(null)
  const [formOpen, setFormOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [form] = Form.useForm()

  const loadGroups = useCallback(async () => {
    setLoading(true)
    try {
      const { rule_groups } = await api.listRuleGroups()
      setGroups(rule_groups)
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setLoading(false)
    }
  }, [message])

  // 汇总全部可选规则（内置三模式 + 自定义规则集），供规则组勾选
  const loadRules = useCallback(async () => {
    try {
      const [bid, tender, general] = await Promise.all([
        api.getRulesByMode('bid'),
        api.getRulesByMode('tender'),
        api.getRulesByMode('general'),
      ])
      const pool = new Map<string, Rule>()
      for (const r of [...bid.rules, ...tender.rules, ...general.rules]) {
        if (!pool.has(r.id)) pool.set(r.id, r)
      }
      for (const rs of ruleSets) {
        if (!rs.builtin) for (const r of rs.rules) if (!pool.has(r.id)) pool.set(r.id, r)
      }
      setRules([...pool.values()])
    } catch {
      /* 规则加载失败不阻断页面 */
    }
  }, [ruleSets])

  useEffect(() => {
    void loadGroups()
    void loadRules()
  }, [loadGroups, loadRules])

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({ rule_ids: [] })
    setFormOpen(true)
  }

  const openEdit = (g: RuleGroup) => {
    setEditing(g)
    form.setFieldsValue({
      name: g.name,
      description: g.description,
      rule_ids: g.rule_ids,
    })
    setFormOpen(true)
  }

  const handleSave = async () => {
    const values = await form.validateFields()
    setSaving(true)
    try {
      await api.saveRuleGroup(editing ? { ...editing, ...values } : values)
      message.success(editing ? '已更新规则组' : '已新建规则组')
      setFormOpen(false)
      await loadGroups()
      onChanged()
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async (id: string) => {
    try {
      await api.deleteRuleGroup(id)
      message.success('已删除')
      await loadGroups()
      onChanged()
    } catch (err) {
      message.error((err as Error).message)
    }
  }

  const ruleName = (id: string) => rules.find((r) => r.id === id)?.name ?? id

  const columns: ColumnsType<RuleGroup> = [
    {
      title: '规则组',
      dataIndex: 'name',
      render: (name: string, row) => (
        <Space size={6}>
          <span style={{ fontWeight: 600 }}>{name}</span>
          {row.builtin && <Tag color="blue">预置</Tag>}
        </Space>
      ),
    },
    { title: '说明', dataIndex: 'description', ellipsis: true },
    {
      title: '规则数',
      dataIndex: 'rule_ids',
      width: 90,
      render: (ids: string[]) => <Tag color="geekblue">{ids.length}</Tag>,
    },
    {
      title: '操作',
      width: 140,
      render: (_, row) => (
        <Space size={4}>
          <Button size="small" onClick={() => openEdit(row)}>
            编辑
          </Button>
          <Popconfirm
            title="确认删除该规则组？"
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            disabled={row.builtin}
            onConfirm={() => handleDelete(row.id)}
          >
            <Button size="small" danger disabled={row.builtin}>
              删除
            </Button>
          </Popconfirm>
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
            <span className="rm-title-icon">🧩</span>审核规则组
          </div>
          <div className="rm-subtitle">
            规则组是规则的命名组合，可多选套用到任务；在此管理每组包含的具体规则。
          </div>
        </div>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          style={{ marginLeft: 'auto' }}
          onClick={openCreate}
        >
          新建规则组
        </Button>
      </div>

      <Card size="small">
        <Table
          size="small"
          rowKey="id"
          loading={loading}
          columns={columns}
          dataSource={groups}
          pagination={false}
          expandable={{
            expandedRowRender: (row) => (
              <div style={{ padding: '4px 8px' }}>
                {(row.rule_ids as string[]).map((id) => (
                  <Tag key={id} style={{ marginBottom: 4 }}>
                    {ruleName(id)}
                  </Tag>
                ))}
              </div>
            ),
          }}
        />
      </Card>

      <Modal
        title={editing ? '编辑规则组' : '新建规则组'}
        open={formOpen}
        onOk={handleSave}
        confirmLoading={saving}
        onCancel={() => setFormOpen(false)}
        okText="保存"
        cancelText="取消"
        width={640}
        destroyOnClose
      >
        <Form form={form} layout="vertical" style={{ marginTop: 12 }}>
          <Form.Item
            name="name"
            label="规则组名称"
            rules={[{ required: true, message: '请输入规则组名称' }]}
          >
            <Input placeholder="例如：完整性组" />
          </Form.Item>
          <Form.Item name="description" label="说明">
            <Input.TextArea rows={2} placeholder="该规则组的审核关注点" />
          </Form.Item>
          <Form.Item
            name="rule_ids"
            label="包含的规则（可多选）"
            rules={[{ required: true, message: '请至少选择一条规则' }]}
          >
            <Select
              mode="multiple"
              showSearch
              optionFilterProp="label"
              placeholder="选择规则"
              options={rules.map((r) => ({
                value: r.id,
                label: `${r.name}（${r.severity}）`,
              }))}
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
