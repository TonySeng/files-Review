import { useState } from 'react'
import {
  App as AntdApp,
  Button,
  Form,
  Input,
  Modal,
  Typography,
} from 'antd'
import { UserOutlined } from '@ant-design/icons'
import { api, setAuth } from '../services/api'
import type { User } from '../types'

interface Props {
  open: boolean
  onSuccess: (user: User) => void
}

export default function LoginModal({ open, onSuccess }: Props) {
  const { message } = AntdApp.useApp()
  const [loginForm] = Form.useForm<{ username: string; password: string }>()
  const [regForm] = Form.useForm<{ username: string; password: string; display_name?: string }>()
  const [loginLoading, setLoginLoading] = useState(false)
  const [regOpen, setRegOpen] = useState(false)
  const [regLoading, setRegLoading] = useState(false)

  const finishLogin = (token: string, user: User) => {
    setAuth({ kind: user.role === 'admin' ? 'admin' : 'user', token, user })
    message.success(`欢迎，${user.display_name || user.username}`)
    onSuccess(user)
  }

  const handleLogin = async () => {
    const values = await loginForm.validateFields()
    setLoginLoading(true)
    try {
      const res = await api.login(values)
      finishLogin(res.token, res.user)
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setLoginLoading(false)
    }
  }

  const handleRegister = async () => {
    const values = await regForm.validateFields()
    setRegLoading(true)
    try {
      await api.register({
        username: values.username,
        password: values.password,
        display_name: values.display_name || null,
      })
      message.success('注册成功，请等待管理员审批通过后即可登录')
      setRegOpen(false)
      regForm.resetFields()
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setRegLoading(false)
    }
  }

  return (
    <Modal
      open={open}
      title="登录"
      closable={false}
      maskClosable={false}
      footer={null}
      width={420}
    >
      <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
        管理员与已激活的普通用户均使用同一套账号密码登录：系统会依据账号角色自动识别身份。
        新用户可点击下方「注册账号」提交申请，由管理员审批通过后方可使用。登录后可前往
        「API Key 管理」自助生成多个密钥。
      </Typography.Paragraph>
      <Form form={loginForm} layout="vertical" size="large" style={{ marginTop: 8 }}>
        <Form.Item
          name="username"
          label="用户名"
          rules={[{ required: true, message: '请输入用户名' }]}
        >
          <Input prefix={<UserOutlined />} placeholder="用户名 / admin" />
        </Form.Item>
        <Form.Item
          name="password"
          label="密码"
          rules={[{ required: true, message: '请输入密码' }]}
        >
          <Input.Password placeholder="请输入密码" onPressEnter={handleLogin} />
        </Form.Item>
        <Button type="primary" block loading={loginLoading} onClick={handleLogin}>
          登录
        </Button>
        <Button
          type="link"
          block
          style={{ marginTop: 4, paddingLeft: 0 }}
          onClick={() => setRegOpen(true)}
        >
          没有账号？注册新账号
        </Button>
      </Form>

      <Modal
        title="注册账号"
        open={regOpen}
        onCancel={() => setRegOpen(false)}
        onOk={handleRegister}
        confirmLoading={regLoading}
        okText="提交申请"
        destroyOnClose
      >
        <Form form={regForm} layout="vertical" initialValues={{ username: '', password: '', display_name: '' }}>
          <Form.Item
            name="username"
            label="用户名"
            rules={[{ required: true, message: '请输入用户名' }]}
          >
            <Input placeholder="登录与标识名" />
          </Form.Item>
          <Form.Item
            name="password"
            label="密码"
            rules={[{ required: true, min: 6, message: '密码至少 6 位' }]}
          >
            <Input.Password placeholder="至少 6 位" />
          </Form.Item>
          <Form.Item name="display_name" label="显示名">
            <Input placeholder="可选，默认为用户名" />
          </Form.Item>
        </Form>
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
          提交后账号处于「待审核」状态，管理员审批通过后方可登录。
        </Typography.Paragraph>
      </Modal>
    </Modal>
  )
}
