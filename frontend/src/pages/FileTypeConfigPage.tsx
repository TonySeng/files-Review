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
import type { FileType, RuleGroup } from '../types'

interface Props {
  ruleGroups: RuleGroup[]
  onBack: () => void
  onChanged: () => void
}

export default function FileTypeConfigPage({ ruleGroups, onBack, onChanged }: Props) {
  const { message } = AntdApp.useApp()
  const [types, setTypes] = useState<FileType[]>([])
  const [loading, setLoading] = useState(false)
  const [editing, setEditing] = useState<FileType | null>(null)
  const [formOpen, setFormOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [form] = Form.useForm()

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const { file_types } = await api.listFileTypes()
      setTypes(file_types)
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setLoading(false)
    }
  }, [message])

  useEffect(() => {
    void load()
  }, [load])

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({ extensions: [], rule_group_ids: [] })
    setFormOpen(true)
  }

  const openEdit = (ft: FileType) => {
    setEditing(ft)
    form.setFieldsValue({
      name: ft.name,
      description: ft.description,
      extensions: ft.extensions,
      rule_group_ids: ft.rule_group_ids,
    })
    setFormOpen(true)
  }

  const handleSave = async () => {
    const values = await form.validateFields()
    setSaving(true)
    try {
      await api.saveFileType(
        editing ? { ...editing, ...values } : values,
      )
      message.success(editing ? '已更新文件类型' : '已新建文件类型')
      setFormOpen(false)
      await load()
      onChanged()
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async (id: string) => {
    try {
      await api.deleteFileType(id)
      message.success('已删除')
      await load()
      onChanged()
    } catch (err) {
      message.error((err as Error).message)
    }
  }

  const columns: ColumnsType<FileType> = [
    {
      title: '类型名称',
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
      title: '允许后缀',
      dataIndex: 'extensions',
      render: (exts: string[]) =>
        exts.length ? (
          exts.map((e) => (
            <Tag key={e}>{e}</Tag>
          ))
        ) : (
          <Tag>不限制</Tag>
        ),
    },
    {
      title: '关联规则组',
      dataIndex: 'rule_group_ids',
      render: (ids: string[]) =>
        ids.map((id) => {
          const g = ruleGroups.find((r) => r.id === id)
          return <Tag key={id} color="geekblue">{g?.name ?? id}</Tag>
        }),
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
            title="确认删除该文件类型？"
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
        <Button icon={<ArrowLeftOutlined />} onClick={onBack} style={{ borderRadius: 999 }}>
          返回工作台
        </Button>
        <div>
          <div className="rm-title">
            <span className="i">🗂</span>文件类型配置
          </div>
          <div className="rm-sub">
            定义可审核的文件类型，并关联审核规则组（用于自动匹配）；系统不自动识别文件内容。
          </div>
        </div>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          style={{ marginLeft: 'auto' }}
          onClick={openCreate}
        >
          新建文件类型
        </Button>
      </div>

      <Card size="small">
        <Table
          size="small"
          rowKey="id"
          loading={loading}
          columns={columns}
          dataSource={types}
          pagination={false}
        />
      </Card>

      <Modal
        title={editing ? '编辑文件类型' : '新建文件类型'}
        open={formOpen}
        onOk={handleSave}
        confirmLoading={saving}
        onCancel={() => setFormOpen(false)}
        okText="保存"
        cancelText="取消"
        destroyOnClose
      >
        <Form form={form} layout="vertical" style={{ marginTop: 12 }}>
          <Form.Item
            name="name"
            label="类型名称"
            rules={[{ required: true, message: '请输入类型名称' }]}
          >
            <Input placeholder="例如：招标文件" />
          </Form.Item>
          <Form.Item name="description" label="说明">
            <Input.TextArea rows={2} placeholder="该类型文件的审核关注点" />
          </Form.Item>
          <Form.Item name="extensions" label="允许的后缀（空表示不限制）">
            <Select
              mode="tags"
              placeholder="输入后缀后回车，如 .pdf"
              tokenSeparators={[',', ' ']}
            />
          </Form.Item>
          <Form.Item name="rule_group_ids" label="关联审核规则组（可多选）">
            <Select
              mode="multiple"
              placeholder="选择自动匹配时套用的规则组"
              options={ruleGroups.map((g) => ({
                value: g.id,
                label: `${g.name}（${g.rule_ids.length} 条规则）`,
              }))}
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
