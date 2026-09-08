import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  App as AntdApp,
  Button,
  Card,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { ArrowLeftOutlined, PlusOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { api } from '../services/api'
import type { FileType, Section } from '../types'
import SynonymTagsInput from '../components/SynonymTagsInput'

interface Props {
  onBack: () => void
}

/**
 * 章节库配置：以「文件类型」为维度维护章节（含同义词）。
 *
 * 章节用于按章节裁剪送审正文——规则关联章节后，仅对命中章节的正文执行审核。
 * 匹配策略：归一化（全角/大小写/空白/标点/编号前缀）后，精确相等 > 前缀 > 包含，
 * 同优先级取更长关键词（「投标人须知前附表」不会被「投标人须知」抢走）。
 */
export default function SectionManagementPage({ onBack }: Props) {
  const { message } = AntdApp.useApp()
  const [fileTypes, setFileTypes] = useState<FileType[]>([])
  const [activeType, setActiveType] = useState<string>('')
  const [sections, setSections] = useState<Section[]>([])
  const [all, setAll] = useState<Section[]>([])
  const [loading, setLoading] = useState(false)

  const [editing, setEditing] = useState<Section | null>(null)
  const [formOpen, setFormOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [synonyms, setSynonyms] = useState<string[]>([])
  const [form] = Form.useForm()

  const loadTypes = useCallback(async () => {
    try {
      const { file_types } = await api.listFileTypes()
      setFileTypes(file_types)
    } catch (err) {
      message.error((err as Error).message)
    }
  }, [message])

  const loadSections = useCallback(async () => {
    setLoading(true)
    try {
      const r = await api.listSections()
      setAll(r.sections || [])
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setLoading(false)
    }
  }, [message])

  useEffect(() => {
    void loadTypes()
    void loadSections()
  }, [loadTypes, loadSections])

  useEffect(() => {
    setSections(activeType ? all.filter((s) => s.file_type_id === activeType) : all)
  }, [all, activeType])

  const counts = useMemo(() => {
    const map: Record<string, number> = {}
    for (const s of all) map[s.file_type_id] = (map[s.file_type_id] || 0) + 1
    return map
  }, [all])

  const openCreate = () => {
    if (!activeType) {
      message.warning('请先选择文件类型')
      return
    }
    setEditing(null)
    setSynonyms([])
    form.resetFields()
    form.setFieldsValue({ file_type_id: activeType, enabled: true, name: '' })
    setFormOpen(true)
  }

  const openEdit = (sec: Section) => {
    setEditing(sec)
    setSynonyms(sec.synonyms?.length ? [...sec.synonyms] : [sec.name])
    form.setFieldsValue({
      name: sec.name,
      note: sec.note || '',
      enabled: sec.enabled !== false,
      file_type_id: sec.file_type_id,
    })
    setFormOpen(true)
  }

  const handleSave = async () => {
    const values = await form.validateFields()
    const name = String(values.name || '').trim()
    // 规范名始终作为首同义词（与后端 save_section 行为一致，前端先补齐便于展示）
    const merged = synonyms.filter((s) => s !== name)
    const payload: Partial<Section> = {
      id: editing?.id,
      file_type_id: values.file_type_id || activeType,
      name,
      synonyms: [name, ...merged],
      note: (values.note || '').trim(),
      enabled: values.enabled !== false,
    }
    setSaving(true)
    try {
      const res = await api.saveSection(payload)
      if (res.warnings?.length) {
        message.warning(res.warnings.join('；'))
      } else {
        message.success(editing ? '章节已更新' : '章节已新增')
      }
      setFormOpen(false)
      await loadSections()
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async (id: string) => {
    try {
      await api.deleteSection(id)
      message.success('已删除')
      await loadSections()
    } catch (err) {
      message.error((err as Error).message)
    }
  }

  const columns: ColumnsType<Section> = [
    {
      title: '章节名',
      dataIndex: 'name',
      width: 180,
      render: (name: string, row) => (
        <Space size={6}>
          <span style={{ fontWeight: 600 }}>{name}</span>
          {row.builtin && <Tag color="blue">预置</Tag>}
          {row.enabled === false && <Tag>已停用</Tag>}
        </Space>
      ),
    },
    {
      title: '同义写法（用于匹配文档中的各种标题写法）',
      dataIndex: 'synonyms',
      render: (syns: string[]) => {
        const list = syns || []
        return (
          <Space size={4} wrap>
            {list.slice(0, 6).map((s) => (
              <Tag key={s}>{s}</Tag>
            ))}
            {list.length > 6 && (
              <Tooltip title={list.slice(6).join('、')}>
                <Tag>+{list.length - 6}</Tag>
              </Tooltip>
            )}
            {!list.length && <Typography.Text type="secondary">未配置</Typography.Text>}
          </Space>
        )
      },
    },
    { title: '备注', dataIndex: 'note', width: 160, ellipsis: true },
    {
      title: '操作',
      width: 140,
      render: (_, row) => (
        <Space size={4}>
          <Button size="small" onClick={() => openEdit(row)}>
            编辑
          </Button>
          <Popconfirm
            title="确认删除该章节？"
            description={row.builtin ? '预置章节不可删除，可改为停用' : undefined}
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            disabled={row.builtin}
            onConfirm={() => handleDelete(row.id)}
          >
            <Tooltip title={row.builtin ? '预置章节不可删除，可改为停用' : ''}>
              <Button size="small" danger disabled={row.builtin}>
                删除
              </Button>
            </Tooltip>
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
            <span className="rm-title-icon">📑</span>章节库配置
          </div>
          <div className="rm-subtitle">
            以文件类型为维度维护章节与同义写法；规则关联章节后，仅对命中章节的正文执行审核。
          </div>
        </div>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          style={{ marginLeft: 'auto' }}
          onClick={openCreate}
        >
          新建章节
        </Button>
      </div>

      <Card size="small" style={{ marginBottom: 12 }}>
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Space wrap>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              文件类型维度：
            </Typography.Text>
            <Select
              value={activeType || 'all'}
              onChange={(v) => setActiveType(!v || v === 'all' ? '' : String(v))}
              style={{ width: 260 }}
              options={[
                { label: `全部（${all.length}）`, value: 'all' },
                ...fileTypes.map((ft) => ({
                  label: `${ft.name}（${counts[ft.id] || 0}）`,
                  value: ft.id,
                })),
              ]}
            />
          </Space>
          <Alert
            type="info"
            showIcon
            style={{ fontSize: 12 }}
            message="匹配规则：忽略大小写/空格/标点与「第X章、1.1、（一）」等编号前缀；精确相等优先，其次前缀/包含，同级取更长的写法（如「投标人须知前附表」不会被「投标人须知」抢走）。"
          />
        </Space>
      </Card>

      <Card size="small">
        <Table
          size="small"
          rowKey="id"
          loading={loading}
          columns={columns}
          dataSource={sections}
          pagination={false}
          locale={{
            emptyText: <Empty description="该文件类型下暂无章节，点击右上角「新建章节」添加" />,
          }}
        />
      </Card>

      <Modal
        title={editing ? `编辑章节：${editing.name}` : '新建章节'}
        open={formOpen}
        onOk={handleSave}
        confirmLoading={saving}
        onCancel={() => setFormOpen(false)}
        okText="保存"
        cancelText="取消"
        destroyOnClose
        width={560}
      >
        <Form form={form} layout="vertical" style={{ marginTop: 12 }}>
          <Form.Item
            name="file_type_id"
            label="所属文件类型"
            rules={[{ required: true, message: '请选择文件类型' }]}
          >
            <Select
              placeholder="选择文件类型"
              options={fileTypes.map((ft) => ({ value: ft.id, label: ft.name }))}
            />
          </Form.Item>
          <Form.Item
            name="name"
            label="章节名（规范名）"
            extra="即匹配的关键词之一；建议与文档中的标题写法一致"
            rules={[{ required: true, message: '请输入章节名' }]}
          >
            <Input placeholder="例如：投标函" />
          </Form.Item>
          <Form.Item
            label="同义写法"
            extra="文档里同一章节的不同标题写法，如「投标书」「投标函及投标函附录」"
          >
            <SynonymTagsInput
              value={synonyms}
              onChange={setSynonyms}
              emptyHint="未配置，将只按章节名匹配"
            />
          </Form.Item>
          <Form.Item name="note" label="备注">
            <Input.TextArea rows={2} placeholder="选填，仅用于说明，不参与匹配" />
          </Form.Item>
          <Form.Item name="enabled" label="启用" valuePropName="checked" initialValue>
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
