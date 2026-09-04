import { useCallback, useEffect, useState } from 'react'
import {
  App as AntdApp,
  Button,
  Drawer,
  Form,
  Input,
  Modal,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
} from 'antd'
import { PlusOutlined, ReloadOutlined, KeyOutlined, LockOutlined } from '@ant-design/icons'
import { api } from '../services/api'
import type { ApiKey, ApiKeyScope, User, UserCreate, UserRole, UserStatus } from '../types'

interface Props {
  onBack: () => void
  /** 嵌入 Tab 容器时为 true：隐藏自带的返回按钮，由容器统一导航。 */
  embedded?: boolean
}

const STATUS_META: Record<UserStatus, { color: string; text: string }> = {
  pending: { color: 'gold', text: '待审核' },
  active: { color: 'green', text: '已激活' },
  disabled: { color: 'default', text: '已停用' },
  rejected: { color: 'red', text: '已驳回' },
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

function scopeTags(scopes?: string[]): string {
  if (!scopes || scopes.length === 0) return '全部'
  if (scopes.includes('*')) return '全部'
  return scopes.map((s) => SCOPE_LABEL[s as ApiKeyScope] ?? s).join('、')
}

export default function UserManagementPage({ onBack, embedded }: Props) {
  const { message } = AntdApp.useApp()
  const [users, setUsers] = useState<User[]>([])
  const [loading, setLoading] = useState(false)
  const [statusFilter, setStatusFilter] = useState<UserStatus | 'all'>('all')
  const [createOpen, setCreateOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [form] = Form.useForm<UserCreate>()
  const [keyModal, setKeyModal] = useState<{ user: User; key: string } | null>(null)

  // 重置密码
  const [resetOpen, setResetOpen] = useState(false)
  const [resetUser, setResetUser] = useState<User | null>(null)
  const [resetForm] = Form.useForm<{ password: string }>()
  const [resetSaving, setResetSaving] = useState(false)

  // 密钥管理抽屉
  const [drawerUser, setDrawerUser] = useState<User | null>(null)
  const [keys, setKeys] = useState<ApiKey[]>([])
  const [keysLoading, setKeysLoading] = useState(false)
  const [newKeyOpen, setNewKeyOpen] = useState(false)
  const [newKeyForm] = Form.useForm<{ name?: string; all: boolean; scopes: ApiKeyScope[] }>()
  const [newKeySaving, setNewKeySaving] = useState(false)
  const [newKeyResult, setNewKeyResult] = useState<{ key: string; name: string | null } | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const users = await api.listUsers()
      setUsers(users)
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setLoading(false)
    }
  }, [message])

  useEffect(() => {
    void load()
  }, [load])

  const filtered = statusFilter === 'all' ? users : users.filter((u) => u.status === statusFilter)

  const handleCreate = async () => {
    const values = await form.validateFields()
    setSaving(true)
    try {
      const u = await api.createUser({
        username: values.username,
        display_name: values.display_name || null,
        role: values.role,
        password: values.password || null,
      })
      message.success(
        u.role === 'user'
          ? `已创建用户 ${u.username}（待其登录后可在「API Key 管理」自助生成密钥）`
          : `已创建管理员 ${u.username}`,
      )
      setCreateOpen(false)
      form.resetFields()
      void load()
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const handleToggleActive = async (u: User, active: boolean) => {
    try {
      await api.updateUser(u.id, { is_active: active })
      message.success(active ? '已启用' : '已停用')
      void load()
    } catch (err) {
      message.error((err as Error).message)
    }
  }

  const handleApprove = async (u: User) => {
    try {
      await api.approveUser(u.id)
      message.success(`已通过 ${u.username} 的注册申请`)
      void load()
    } catch (err) {
      message.error((err as Error).message)
    }
  }

  const handleReject = async (u: User) => {
    try {
      await api.rejectUser(u.id)
      message.success(`已驳回 ${u.username} 的注册申请`)
      void load()
    } catch (err) {
      message.error((err as Error).message)
    }
  }

  const handleRotate = async (u: User) => {
    try {
      const res = await api.rotateKey(u.id)
      setKeyModal({ user: u, key: res.api_key })
      void load()
    } catch (err) {
      message.error((err as Error).message)
    }
  }

  const handleDelete = async (u: User) => {
    try {
      await api.deleteUser(u.id)
      message.success('已删除')
      void load()
    } catch (err) {
      message.error((err as Error).message)
    }
  }

  const handleResetPassword = async () => {
    if (!resetUser) return
    const values = await resetForm.validateFields()
    setResetSaving(true)
    try {
      // 后端 PATCH /api/admin/users/{id} 的 password 字段即"管理员重置用户密码"
      await api.updateUser(resetUser.id, { password: values.password })
      message.success(`已为 ${resetUser.username} 重置密码，请尽快通知其登录`)
      setResetOpen(false)
      resetForm.resetFields()
      void load()
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setResetSaving(false)
    }
  }

  // ---------------- 密钥管理抽屉 ----------------
  const openDrawer = async (u: User) => {
    setDrawerUser(u)
    setKeysLoading(true)
    try {
      const ks = await api.listUserKeys(u.id)
      setKeys(ks)
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setKeysLoading(false)
    }
  }

  const refreshKeys = async () => {
    if (!drawerUser) return
    setKeysLoading(true)
    try {
      setKeys(await api.listUserKeys(drawerUser.id))
    } finally {
      setKeysLoading(false)
    }
  }

  const setKeyStatus = async (keyId: string, status: 'active' | 'disabled' | 'revoked') => {
    if (!drawerUser) return
    try {
      await api.adminUpdateKey(drawerUser.id, keyId, { status })
      message.success(status === 'revoked' ? '已吊销密钥' : status === 'disabled' ? '已禁用' : '已启用')
      await refreshKeys()
    } catch (err) {
      message.error((err as Error).message)
    }
  }

  const handleCreateKey = async () => {
    if (!drawerUser) return
    const values = await newKeyForm.validateFields()
    setNewKeySaving(true)
    try {
      const scopes: string[] = values.all ? ['*'] : (values.scopes ?? [])
      const res = await api.adminCreateKey(drawerUser.id, {
        name: values.name || null,
        scopes,
      })
      setNewKeyResult({ key: res.api_key, name: res.name })
      setNewKeyOpen(false)
      newKeyForm.resetFields()
      await refreshKeys()
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setNewKeySaving(false)
    }
  }

  const columns = [
    { title: '用户名', dataIndex: 'username', key: 'username' },
    { title: '显示名', dataIndex: 'display_name', key: 'display_name' },
    {
      title: '角色',
      dataIndex: 'role',
      key: 'role',
      render: (r: UserRole) => (
        <Tag color={r === 'admin' ? 'gold' : 'blue'}>{r === 'admin' ? '管理员' : '普通用户'}</Tag>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      render: (s: UserStatus) => <Tag color={STATUS_META[s].color}>{STATUS_META[s].text}</Tag>,
    },
    {
      title: 'API Key',
      key: 'apikeys',
      render: (_: unknown, u: User) => (
        <Space>
          <Typography.Text code>{(u.api_keys?.length ?? 0)} 枚</Typography.Text>
          <Button size="small" icon={<KeyOutlined />} onClick={() => openDrawer(u)}>
            管理
          </Button>
        </Space>
      ),
    },
    {
      title: '启用',
      dataIndex: 'is_active',
      key: 'is_active',
      render: (a: boolean, u: User) => (
        <Switch size="small" checked={a} onChange={(v) => handleToggleActive(u, v)} />
      ),
    },
    { title: '创建者', dataIndex: 'created_by', key: 'created_by' },
    { title: '最近登录', dataIndex: 'last_login_at', key: 'last_login_at' },
    {
      title: '操作',
      key: 'actions',
      render: (_: unknown, u: User) => (
        <Space>
          {u.status === 'pending' && (
            <>
              <Button size="small" type="primary" onClick={() => handleApprove(u)}>
                通过
              </Button>
              <Popconfirm title="确认驳回该注册申请？" onConfirm={() => handleReject(u)}>
                <Button size="small" danger>
                  驳回
                </Button>
              </Popconfirm>
            </>
          )}
          <Button size="small" onClick={() => handleRotate(u)}>
            重置密钥
          </Button>
          <Button
            size="small"
            icon={<LockOutlined />}
            onClick={() => {
              setResetUser(u)
              setResetOpen(true)
            }}
          >
            重置密码
          </Button>
          <Popconfirm
            title="确认删除该用户？"
            description="删除后其所有密钥立即失效。"
            onConfirm={() => handleDelete(u)}
          >
            <Button size="small" danger>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  const keyColumns = [
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
    { title: '权限范围', key: 'scopes', render: (_: unknown, k: ApiKey) => scopeTags(k.scopes) },
    { title: '创建时间', dataIndex: 'created_at', key: 'created_at' },
    { title: '最近使用', dataIndex: 'last_used_at', key: 'last_used_at', render: (v: string | null) => v || '—' },
    {
      title: '操作',
      key: 'actions',
      render: (_: unknown, k: ApiKey) => (
        <Space>
          {k.status === 'active' && (
            <Button size="small" onClick={() => setKeyStatus(k.key_id, 'disabled')}>
              禁用
            </Button>
          )}
          {k.status === 'disabled' && (
            <Button size="small" type="primary" onClick={() => setKeyStatus(k.key_id, 'active')}>
              启用
            </Button>
          )}
          {k.status !== 'revoked' && (
            <Popconfirm title="确认吊销该密钥？吊销后不可恢复。" onConfirm={() => setKeyStatus(k.key_id, 'revoked')}>
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
      <Space className="page-sticky-row" style={{ marginBottom: 12 }} wrap>
        {!embedded && <Button onClick={onBack}>返回</Button>}
        <Button icon={<PlusOutlined />} type="primary" onClick={() => setCreateOpen(true)}>
          新建用户
        </Button>
        <Button icon={<ReloadOutlined />} onClick={() => void load()}>
          刷新
        </Button>
        <Segmented
          value={statusFilter}
          onChange={(v) => setStatusFilter(v as UserStatus | 'all')}
          options={[
            { label: '全部', value: 'all' },
            { label: '待审核', value: 'pending' },
            { label: '已激活', value: 'active' },
            { label: '已停用', value: 'disabled' },
            { label: '已驳回', value: 'rejected' },
          ]}
        />
      </Space>
      <Table
        rowKey="id"
        loading={loading}
        dataSource={filtered}
        columns={columns}
        pagination={{ pageSize: 10 }}
        size="small"
      />

      <Modal
        title="新建用户"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={handleCreate}
        confirmLoading={saving}
        destroyOnClose
      >
        <Form form={form} layout="vertical" initialValues={{ role: 'user' }}>
          <Form.Item name="username" label="用户名" rules={[{ required: true, message: '请输入用户名' }]}>
            <Input placeholder="登录与标识名" />
          </Form.Item>
          <Form.Item name="display_name" label="显示名">
            <Input placeholder="可选，默认为用户名" />
          </Form.Item>
          <Form.Item name="role" label="角色" rules={[{ required: true }]}>
            <Select
              options={[
                { value: 'user', label: '普通用户' },
                { value: 'admin', label: '管理员' },
              ]}
            />
          </Form.Item>
          <Form.Item
            name="password"
            label="初始密码"
            rules={[{ required: true, min: 6, message: '密码至少 6 位' }]}
          >
            <Input.Password placeholder="用户首次登录密码（至少 6 位）" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="API Key 凭证"
        open={!!keyModal}
        onCancel={() => setKeyModal(null)}
        footer={[
          <Button
            key="copy"
            type="primary"
            onClick={() => {
              if (keyModal) {
                navigator.clipboard?.writeText(keyModal.key)
                message.success('已复制到剪贴板')
              }
            }}
          >
            复制
          </Button>,
        ]}
      >
        <Typography.Paragraph>
          用户 <b>{keyModal?.user.username}</b> 重置后的 API Key（请妥善下发给用户）：
        </Typography.Paragraph>
        <Input.Password readOnly value={keyModal?.key || ''} />
      </Modal>

      <Drawer
        title={drawerUser ? `API Key 管理 · ${drawerUser.username}` : 'API Key 管理'}
        width={720}
        open={!!drawerUser}
        onClose={() => setDrawerUser(null)}
        extra={
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setNewKeyOpen(true)}>
            新建密钥
          </Button>
        }
      >
        <Table
          rowKey="key_id"
          loading={keysLoading}
          dataSource={keys}
          columns={keyColumns}
          pagination={false}
          size="small"
          locale={{ emptyText: '该用户暂无密钥' }}
        />
      </Drawer>

      <Modal
        title="新建 API Key"
        open={newKeyOpen}
        onCancel={() => setNewKeyOpen(false)}
        onOk={handleCreateKey}
        confirmLoading={newKeySaving}
        destroyOnClose
      >
        <Form form={newKeyForm} layout="vertical" initialValues={{ all: true, scopes: [] }}>
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
        open={!!newKeyResult}
        onCancel={() => setNewKeyResult(null)}
        footer={[
          <Button
            key="copy"
            type="primary"
            onClick={() => {
              if (newKeyResult) {
                navigator.clipboard?.writeText(newKeyResult.key)
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
        <Input.Password readOnly value={newKeyResult?.key || ''} />
      </Modal>

      <Modal
        title="重置密码"
        open={resetOpen}
        onCancel={() => setResetOpen(false)}
        onOk={handleResetPassword}
        confirmLoading={resetSaving}
        okText="确认重置"
        destroyOnClose
      >
        <Typography.Paragraph>
          为 <b>{resetUser?.username}</b> 设置新密码（至少 6 位）。重置后旧密码立即失效，
          该用户下次需使用新密码登录。
        </Typography.Paragraph>
        <Form form={resetForm} layout="vertical" initialValues={{ password: '' }}>
          <Form.Item
            name="password"
            label="新密码"
            rules={[{ required: true, min: 6, message: '密码至少 6 位' }]}
          >
            <Input.Password placeholder="请输入新密码（至少 6 位）" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}

// 权限范围多选
function ScopeSelect() {
  return (
    <Select
      mode="multiple"
      placeholder="选择权限范围"
      options={ALL_SCOPES.map((s) => ({ value: s, label: SCOPE_LABEL[s] }))}
    />
  )
}
