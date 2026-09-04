import { useCallback, useEffect, useState } from 'react'
import {
  App as AntdApp,
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import { api } from '../services/api'
import type { ApiKey, ApiKeyScope, User } from '../types'

interface Props {
  currentUser: User | null
  isAdmin: boolean
  onBack: () => void
  /** 嵌入 Tab 容器时为 true：隐藏自带的返回按钮，由容器统一导航。 */
  embedded?: boolean
}

const ALL_SCOPES: ApiKeyScope[] = ['review', 'kb', 'feedback', 'audit', 'rules', 'settings']
const SCOPE_LABEL: Record<ApiKeyScope, string> = {
  review: '审核',
  kb: '知识库',
  feedback: '反馈',
  audit: '审计',
  rules: '规则',
  settings: '配置',
}

function scopeText(scopes?: string[]): string {
  if (!scopes || scopes.length === 0) return '全部'
  if (scopes.includes('*')) return '全部'
  return scopes.map((s) => SCOPE_LABEL[s as ApiKeyScope] ?? s).join('、')
}

function ScopeSelect() {
  return (
    <Select
      mode="multiple"
      placeholder="选择权限范围"
      options={ALL_SCOPES.map((s) => ({ value: s, label: SCOPE_LABEL[s] }))}
    />
  )
}

export default function ApiKeyManagementPage({ currentUser, isAdmin, onBack, embedded }: Props) {
  const { message } = AntdApp.useApp()
  const [users, setUsers] = useState<User[]>([])
  const [selectedUserId, setSelectedUserId] = useState<string | null>(
    currentUser?.id ?? null,
  )
  const [keys, setKeys] = useState<ApiKey[]>([])
  const [loading, setLoading] = useState(false)
  const [newOpen, setNewOpen] = useState(false)
  const [newForm] = Form.useForm<{ name?: string; all: boolean; scopes: ApiKeyScope[] }>()
  const [newSaving, setNewSaving] = useState(false)
  const [newResult, setNewResult] = useState<{ key: string; name: string | null } | null>(null)

  // 管理员可切换目标用户；普通用户固定为自己
  useEffect(() => {
    if (!isAdmin) {
      setSelectedUserId(currentUser?.id ?? null)
      return
    }
    api
      .listUsers()
      .then((us) => {
        setUsers(us)
        setSelectedUserId((prev) => prev ?? us[0]?.id ?? null)
      })
      .catch((err) => message.error((err as Error).message))
  }, [isAdmin, currentUser, message])

  const loadKeys = useCallback(async () => {
    if (!selectedUserId) return
    setLoading(true)
    try {
      const ks = isAdmin
        ? await api.listUserKeys(selectedUserId)
        : await api.listMyKeys()
      setKeys(ks)
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setLoading(false)
    }
  }, [isAdmin, selectedUserId, message])

  useEffect(() => {
    void loadKeys()
  }, [loadKeys])

  const createKey = async () => {
    const values = await newForm.validateFields()
    setNewSaving(true)
    try {
      const scopes: string[] = values.all ? ['*'] : (values.scopes ?? [])
      const res = isAdmin
        ? await api.adminCreateKey(selectedUserId!, { name: values.name || null, scopes })
        : await api.createMyKey({ name: values.name || null, scopes })
      setNewResult({ key: res.api_key, name: res.name })
      setNewOpen(false)
      newForm.resetFields()
      await loadKeys()
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setNewSaving(false)
    }
  }

  const updateKey = async (keyId: string, payload: { status?: 'active' | 'disabled' | 'revoked'; name?: string; scopes?: string[] }) => {
    if (!selectedUserId) return
    try {
      if (isAdmin) await api.adminUpdateKey(selectedUserId, keyId, payload)
      else await api.updateMyKey(keyId, payload)
      await loadKeys()
    } catch (err) {
      message.error((err as Error).message)
    }
  }

  const revokeKey = (keyId: string) => updateKey(keyId, { status: 'revoked' })

  const columns = [
    { title: '名称', dataIndex: 'name', key: 'name', render: (n: string | null) => n || '未命名' },
    { title: '前缀', dataIndex: 'prefix', key: 'prefix', render: (p: string) => <Typography.Text code>{p}…</Typography.Text> },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      render: (s: string) => (
        <Tag color={s === 'active' ? 'green' : s === 'disabled' ? 'default' : 'red'}>
          {s === 'active' ? '启用' : s === 'disabled' ? '禁用' : '已吊销'}
        </Tag>
      ),
    },
    { title: '权限范围', key: 'scopes', render: (_: unknown, k: ApiKey) => scopeText(k.scopes) },
    { title: '创建时间', dataIndex: 'created_at', key: 'created_at' },
    { title: '最近使用', dataIndex: 'last_used_at', key: 'last_used_at', render: (v: string | null) => v || '—' },
    {
      title: '操作',
      key: 'actions',
      render: (_: unknown, k: ApiKey) => (
        <Space>
          {k.status === 'active' && (
            <Button size="small" onClick={() => updateKey(k.key_id, { status: 'disabled' })}>
              禁用
            </Button>
          )}
          {k.status === 'disabled' && (
            <Button size="small" type="primary" onClick={() => updateKey(k.key_id, { status: 'active' })}>
              启用
            </Button>
          )}
          {k.status !== 'revoked' && (
            <Popconfirm title="确认吊销该密钥？吊销后不可恢复。" onConfirm={() => revokeKey(k.key_id)}>
              <Button size="small" danger>
                吊销
              </Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ]

  return (
    <div style={{ padding: 16 }}>
      <Space style={{ marginBottom: 12 }} wrap>
        {!embedded && <Button onClick={onBack}>返回</Button>}
        {isAdmin && (
          <Select
            style={{ width: 240 }}
            placeholder="选择用户"
            value={selectedUserId ?? undefined}
            onChange={(v) => setSelectedUserId(v)}
            options={users.map((u) => ({ value: u.id, label: `${u.username}（${u.display_name}）` }))}
          />
        )}
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setNewOpen(true)}>
          新建密钥
        </Button>
      </Space>
      <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
        {isAdmin
          ? '管理员视图：可切换任意用户并管理其全部 API Key（启用 / 禁用 / 吊销）。'
          : '以下为你的 API Key。每个密钥可设名称与权限范围，请妥善保管，仅创建时显示一次。'}
      </Typography.Paragraph>
      <Table
        rowKey="key_id"
        loading={loading}
        dataSource={keys}
        columns={columns}
        pagination={false}
        size="small"
        locale={{ emptyText: '暂无密钥，点击「新建密钥」生成' }}
      />

      <Modal
        title="新建 API Key"
        open={newOpen}
        onCancel={() => setNewOpen(false)}
        onOk={createKey}
        confirmLoading={newSaving}
        destroyOnClose
      >
        <Form form={newForm} layout="vertical" initialValues={{ all: true, scopes: [] }}>
          <Form.Item name="name" label="密钥名称">
            <Input placeholder="如：生产环境密钥" />
          </Form.Item>
          <Form.Item name="all" label="权限范围" valuePropName="checked">
            <Select
              options={[
                { value: true, label: '全部权限（*）' },
                { value: false, label: '自定义范围' },
              ]}
            />
          </Form.Item>
          <Form.Item noStyle shouldUpdate={(p, c) => p.all !== c.all}>
            {({ getFieldValue }) =>
              getFieldValue('all') === false ? (
                <Form.Item
                  name="scopes"
                  label="选择范围"
                  rules={[{ required: true, message: '请至少选择一个范围' }]}
                >
                  <ScopeSelect />
                </Form.Item>
              ) : null
            }
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="API Key 已创建"
        open={!!newResult}
        onCancel={() => setNewResult(null)}
        footer={[
          <Button
            key="copy"
            type="primary"
            onClick={() => {
              if (newResult) {
                navigator.clipboard?.writeText(newResult.key)
                message.success('已复制到剪贴板')
              }
            }}
          >
            复制密钥
          </Button>,
        ]}
      >
        <Typography.Paragraph type="secondary">
          请立即复制并妥善保存；此密钥仅显示一次。
        </Typography.Paragraph>
        <Input.Password readOnly value={newResult?.key || ''} />
      </Modal>
    </div>
  )
}
