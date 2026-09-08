import { useEffect, useState } from 'react'
import {
  App as AntdApp,
  Button,
  Descriptions,
  Divider,
  Drawer,
  Form,
  Input,
  InputNumber,
  Select,
  Space,
  Switch,
  Tag,
  Typography,
} from 'antd'
import { ApiOutlined, CopyOutlined, DatabaseOutlined, ReloadOutlined } from '@ant-design/icons'
import { api } from '../services/api'
import type { AppConfig, KnowledgeBase, StorageStatus, User, UserRole } from '../types'

interface Props {
  open: boolean
  onClose: () => void
  onSaved: (config: AppConfig) => void
  /** 当前登录角色；普通用户仅可见自身 api-key，看不到服务配置。 */
  role?: UserRole
  /** 当前登录用户（用于普通用户展示自己的 api-key）。 */
  currentUser?: User
}

type TestState = Record<string, { ok: boolean; message: string } | 'loading' | undefined>

// OCR 各方服务的默认地址/路径（切换服务类型时自动带出）
const TULING_BASE = 'http://223.111.149.152:8090'
const TULING_PATH = '/tuling/uocr/v2/recognize'
const BAIDU_BASE = 'https://aip.baidubce.com'
const BAIDU_PATH = '/rest/2.0/ocr/v1/general_basic'
const BAIDU_TOKEN_PATH = '/oauth/2.0/token'
const XFYUN_BASE = 'https://api.xf-yun.com'
const XFYUN_PATH = '/v1/private/sf8e6aca1'

export default function SettingsDrawer({ open, onClose, onSaved, role, currentUser }: Props) {
  const { message } = AntdApp.useApp()
  const isAdmin = role === 'admin'
  const [form] = Form.useForm<AppConfig>()
  const [saving, setSaving] = useState(false)
  const [tests, setTests] = useState<TestState>({})
  const [kbs, setKbs] = useState<KnowledgeBase[]>([])
  // 已配置密钥字段（回显脱敏为 "***"）标记，便于展示且不向表单写入掩码
  const [configured, setConfigured] = useState<Record<string, boolean>>({})
  const [storage, setStorage] = useState<StorageStatus | null>(null)
  const [reloadingStorage, setReloadingStorage] = useState(false)
  // 当前选中的 OCR 服务类型（响应式：切换后表单随之展示对应字段）
  const ocrProv = (Form.useWatch('ocr_provider', form) as string | undefined) ?? 'tuling'

  useEffect(() => {
    if (!open || !isAdmin) return
    api
      .getSettings()
      .then(({ config }) => {
        // 密钥字段后端脱敏为 "***"；回显时清空并标记“已配置”，
        // 避免把掩码写入表单后测试/保存时当成真实密钥导致 401 或误清空。
        const KEY_FIELDS = [
          'llm_api_key',
          'web_search_api_key',
          'kb_api_key',
          'ocr_api_key',
          'ocr_secret_key',
          'ocr_xfyun_api_key',
          'ocr_xfyun_api_secret',
        ] as const
        const echo = { ...config }
        const configuredState: Record<string, boolean> = {}
        for (const k of KEY_FIELDS) {
          if ((echo as Record<string, unknown>)[k] === '***') {
            configuredState[k] = true
            ;(echo as Record<string, unknown>)[k] = ''
          }
        }
        setConfigured(configuredState)
        form.setFieldsValue(echo)
      })
      .catch((err) => message.error((err as Error).message))
    api
      .listKnowledgeBases()
      .then(({ knowledge_bases }) => setKbs(knowledge_bases))
      .catch(() => setKbs([]))
    api
      .getStorageStatus()
      .then(setStorage)
      .catch(() => setStorage(null))
  }, [open, form])

  const handleStorageReload = async () => {
    setReloadingStorage(true)
    try {
      const res = await api.reloadStorage()
      setStorage(res)
      message.success(res.changed ? '存储配置已重载并应用' : '配置无变更')
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setReloadingStorage(false)
    }
  }

  const handleTest = async (target: 'llm' | 'ocr' | 'kb' | 'web_search') => {
    const urlField = (
      { llm: 'llm_base_url', ocr: 'ocr_base_url', kb: 'kb_base_url', web_search: null } as const
    )[target]
    const keyField = (
      { llm: 'llm_api_key', ocr: null, kb: 'kb_api_key', web_search: 'web_search_api_key' } as const
    )[target]
    setTests((prev) => ({ ...prev, [target]: 'loading' }))
    try {
      // 密钥为空（已配置但未改动）时传 undefined，由后端沿用已存密钥；
      // 绝不向下发送 "***" 掩码，否则会被当成真实密钥导致 401。
      const rawKey = keyField ? form.getFieldValue(keyField) : undefined
      // OCR 各服务的密钥字段不同：baidu 用 ocr_api_key/ocr_secret_key，
      // xfyun 用 ocr_xfyun_api_key/ocr_xfyun_api_secret + ocr_xfyun_app_id。
      // 一并下发未保存的服务类型，支持「填完即测」，无需先保存
      const rawProvider = target === 'ocr' ? form.getFieldValue('ocr_provider') : undefined
      const ocrIsXfyun = rawProvider === 'xfyun'
      // 大模型“先测后存”时需要把表单里的模型名一起带过去（默认基地址为本地 vLLM 用 /model，
      // 切到讯飞星火等需显式填如 4.0Ultra），否则测试会用到已保存的旧模型名而误报 400。
      const rawModel = target === 'llm' ? form.getFieldValue('llm_model') : undefined
      const rawSecret = target === 'ocr'
        ? form.getFieldValue(ocrIsXfyun ? 'ocr_xfyun_api_secret' : 'ocr_secret_key')
        : undefined
      const rawOcrKey = target === 'ocr' && !ocrIsXfyun
        ? form.getFieldValue('ocr_api_key')
        : undefined
      const rawAppId = target === 'ocr' && ocrIsXfyun
        ? form.getFieldValue('ocr_xfyun_app_id')
        : undefined
      const res = await api.testConnection(
        target,
        urlField ? form.getFieldValue(urlField) : undefined,
        ((rawKey && rawKey !== '***' ? rawKey : undefined) ??
          (rawOcrKey && rawOcrKey !== '***' ? rawOcrKey : undefined)),
        rawSecret && rawSecret !== '***' ? rawSecret : undefined,
        rawProvider || undefined,
        rawAppId && rawAppId !== '***' ? rawAppId : undefined,
        rawModel && rawModel !== '***' ? rawModel : undefined,
      )
      setTests((prev) => ({ ...prev, [target]: { ok: res.ok, message: res.message } }))
      if (res.ok) {
        message.info('连接测试通过。注意：测试使用的是表单当前值，不会保存配置；请点击「保存」按钮才会持久化到服务端。')
      }
      if (target === 'kb' && res.ok) {
        const list = (res.detail as { knowledge_bases?: KnowledgeBase[] })?.knowledge_bases
        if (list) setKbs(list)
      }
    } catch (err) {
      setTests((prev) => ({
        ...prev,
        [target]: { ok: false, message: (err as Error).message },
      }))
    }
  }

  const handleSave = async () => {
    setSaving(true)
    try {
      const values = await form.validateFields()
      // 密钥字段：已配置但留空表示“保持不变”，剔除后再提交，避免覆盖/清空已存密钥
      const patch = { ...values } as Partial<AppConfig> & Record<string, unknown>
      for (const k of [
        'llm_api_key',
        'web_search_api_key',
        'kb_api_key',
        'ocr_api_key',
        'ocr_secret_key',
        'ocr_xfyun_api_key',
        'ocr_xfyun_api_secret',
      ] as const) {
        if (!patch[k]) delete patch[k]
      }
      const { config } = await api.updateSettings(patch as Partial<AppConfig>)
      message.success('配置已保存')
      onSaved(config)
      onClose()
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const handleReset = async () => {
    try {
      const { config } = await api.resetSettings()
      form.setFieldsValue(config)
      onSaved(config)
      message.success('已恢复默认配置')
    } catch (err) {
      message.error((err as Error).message)
    }
  }

  const testTag = (target: string) => {
    const state = tests[target]
    if (state === 'loading') return <Tag color="processing">测试中…</Tag>
    if (!state) return null
    return (
      <Tag color={state.ok ? 'success' : 'error'}>
        {state.ok ? '连接正常' : state.message.slice(0, 40)}
      </Tag>
    )
  }

  // 已配置密钥的“已配置”标记（回显为 "***" 已清空，用此提示用户无需重复填写）
  const keyTag = (name: string) =>
    configured[name] ? <Tag color="default">已配置</Tag> : null

  // 普通用户：仅展示自身 api-key，服务配置（模型/知识库/OCR）仅管理员可改
  if (!isAdmin) {
    const key = currentUser?.api_key
    return (
      <Drawer title="我的凭证" width={460} open={open} onClose={onClose}>
        <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
          你是普通用户。调用接口时需在请求头携带你的 <Typography.Text code>api-key</Typography.Text>；
          系统将据此解析你归属的规则、规则集、规则组与文件类型，并记录你的每一次请求。
          服务配置（模型 / 知识库 / OCR）由管理员统一管理，普通用户不可修改。
        </Typography.Paragraph>
        <Form layout="vertical" size="small">
          <Form.Item
            label="我的 API Key"
            extra={<span style={{ fontSize: 12 }}>请妥善保管，切勿泄露；重置请联系管理员。</span>}
          >
            <Input.Password
              readOnly
              value={key || ''}
              placeholder="（未获取到，请重新登录）"
              addonAfter={
                <Button
                  type="text"
                  size="small"
                  icon={<CopyOutlined />}
                  disabled={!key}
                  onClick={() => {
                    if (key) {
                      navigator.clipboard?.writeText(key)
                      message.success('API Key 已复制到剪贴板')
                    }
                  }}
                />
              }
            />
          </Form.Item>
        </Form>
      </Drawer>
    )
  }

  return (
    <Drawer
      title="服务配置"
      width={520}
      open={open}
      onClose={onClose}
      extra={
        <Space>
          <Button icon={<ReloadOutlined />} onClick={handleReset}>
            恢复默认
          </Button>
          <Button type="primary" loading={saving} onClick={handleSave}>
            保存
          </Button>
        </Space>
      }
    >
      <Form form={form} layout="vertical" size="small">
        <Divider orientation="left" plain style={{ marginTop: 0 }}>
          大模型
        </Divider>
        <Form.Item
          label={
            <Space>
              服务地址 {testTag('llm')}
              <Button
                size="small"
                type="link"
                icon={<ApiOutlined />}
                onClick={() => handleTest('llm')}
              >
                测试
              </Button>
            </Space>
          }
          name="llm_base_url"
          rules={[{ required: true, message: '请填写大模型地址' }]}
        >
          <Input placeholder="http://223.111.149.152:8000" />
        </Form.Item>
        <Space size={10} style={{ width: '100%' }}>
          <Form.Item label="模型名称" name="llm_model" style={{ flex: 1, minWidth: 150 }}>
            <Input placeholder="/model（讯飞星火填 4.0Ultra 等）" />
          </Form.Item>
          <Form.Item
            label={
              <Space size={4}>
                API Key {keyTag('llm_api_key')}
              </Space>
            }
            name="llm_api_key"
            style={{ minWidth: 130 }}
          >
            <Input.Password placeholder="已配置则留空保持不变" />
          </Form.Item>
        </Space>
        <Space size={10}>
          <Form.Item label="超时(秒)" name="llm_timeout">
            <InputNumber min={30} max={3600} style={{ width: 100 }} />
          </Form.Item>
          <Form.Item label="温度" name="llm_temperature">
            <InputNumber min={0} max={2} step={0.1} style={{ width: 90 }} />
          </Form.Item>
          <Form.Item label="最大输出" name="llm_max_tokens">
            <InputNumber min={512} max={32768} step={512} style={{ width: 110 }} />
          </Form.Item>
        </Space>

        <Divider orientation="left" plain>
          OCR 服务
        </Divider>
        <Form.Item label="服务类型" name="ocr_provider">
          <Select
            options={[
              { value: 'tuling', label: '图聆云（免鉴权，multipart 上传）' },
              {
                value: 'baidu',
                label: '百度智能云 OCR（API Key + Secret Key 授权）',
              },
              {
                value: 'xfyun',
                label: '讯飞开放平台 OCR（AppID + APIKey + APISecret 签名）',
              },
            ]}
            onChange={(v: 'tuling' | 'baidu' | 'xfyun') => {
              // 切换服务类型时，若地址/路径仍是另一方的默认值则自动带出对应默认值
              const patch: Record<string, string> = {}
              const curBase = form.getFieldValue('ocr_base_url')
              const curPath = form.getFieldValue('ocr_path')
              if (v === 'baidu') {
                if (!curBase || curBase === TULING_BASE || curBase === XFYUN_BASE)
                  patch.ocr_base_url = BAIDU_BASE
                if (!curPath || curPath === TULING_PATH || curPath === XFYUN_PATH)
                  patch.ocr_path = BAIDU_PATH
                if (!form.getFieldValue('ocr_token_path')) {
                  patch.ocr_token_path = BAIDU_TOKEN_PATH
                }
              } else if (v === 'xfyun') {
                if (!curBase || curBase === TULING_BASE || curBase === BAIDU_BASE)
                  patch.ocr_base_url = XFYUN_BASE
                // 仅当前路径是其他服务的默认值时才带出讯飞默认；用户已手动选择的讯飞路径（含 intsig）保持不动
                if (!curPath || curPath === TULING_PATH || curPath === BAIDU_PATH)
                  patch.ocr_path = XFYUN_PATH
              } else {
                if (!curBase || curBase === BAIDU_BASE || curBase === XFYUN_BASE)
                  patch.ocr_base_url = TULING_BASE
                if (!curPath || curPath === BAIDU_PATH || curPath === XFYUN_PATH)
                  patch.ocr_path = TULING_PATH
              }
              if (Object.keys(patch).length) form.setFieldsValue(patch)
            }}
          />
        </Form.Item>
        <Form.Item
          label={
            <Space>
              服务地址 {testTag('ocr')}
              <Button
                size="small"
                type="link"
                icon={<ApiOutlined />}
                onClick={() => handleTest('ocr')}
              >
                测试
              </Button>
            </Space>
          }
          name="ocr_base_url"
        >
          <Input
            placeholder={
              ocrProv === 'baidu'
                ? 'https://aip.baidubce.com'
                : ocrProv === 'xfyun'
                  ? 'https://api.xf-yun.com'
                  : 'http://223.111.149.152:8090'
            }
          />
        </Form.Item>
        <Space size={10} style={{ width: '100%' }}>
          <Form.Item label="接口路径" name="ocr_path" style={{ flex: 1, minWidth: 200 }}>
            <Input
              placeholder={
                ocrProv === 'baidu'
                  ? '/rest/2.0/ocr/v1/general_basic（通用文字识别）'
                  :                 ocrProv === 'xfyun'
                  ? '/v1/private/sf8e6aca1（通用文字识别）或 /v1/private/hh_ocr_recognize_doc（intsig 52语种）'
                  : '/tuling/uocr/v2/recognize'
              }
            />
          </Form.Item>
          <Form.Item label="超时(秒)" name="ocr_timeout">
            <InputNumber min={5} max={600} style={{ width: 100 }} />
          </Form.Item>
        </Space>

        {ocrProv === 'baidu' ? (
          <>
            <Space size={10} style={{ width: '100%' }}>
              <Form.Item
                label="API Key"
                name="ocr_api_key"
                style={{ flex: 1, minWidth: 220 }}
                extra={configured.ocr_api_key ? '已保存密钥，留空表示不修改' : undefined}
              >
                <Input.Password placeholder="百度智能云应用的 API Key" autoComplete="new-password" />
              </Form.Item>
              <Form.Item
                label="Secret Key"
                name="ocr_secret_key"
                style={{ flex: 1, minWidth: 220 }}
                extra={configured.ocr_secret_key ? '已保存密钥，留空表示不修改' : undefined}
              >
                <Input.Password
                  placeholder="百度智能云应用的 Secret Key"
                  autoComplete="new-password"
                />
              </Form.Item>
            </Space>
            <Form.Item label="Token 接口路径" name="ocr_token_path">
              <Input placeholder="/oauth/2.0/token" />
            </Form.Item>
            <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: -8 }}>
              百度智能云通用文字识别：用 API Key / Secret Key 换取 access_token（缓存至过期前 5
              分钟自动刷新），图片以 base64 表单提交。免费额度与 QPS 限制以控制台为准；
              高精度版可将接口路径改为 /rest/2.0/ocr/v1/accurate_basic。
            </Typography.Paragraph>
          </>
        ) : ocrProv === 'xfyun' ? (
          <>
            <Space size={10} style={{ width: '100%' }}>
              <Form.Item
                label="AppID"
                name="ocr_xfyun_app_id"
                style={{ flex: 1, minWidth: 160 }}
              >
                <Input placeholder="讯飞开放平台应用 AppID" autoComplete="off" />
              </Form.Item>
              <Form.Item
                label="APIKey"
                name="ocr_xfyun_api_key"
                style={{ flex: 1, minWidth: 200 }}
                extra={configured.ocr_xfyun_api_key ? '已保存密钥，留空表示不修改' : undefined}
              >
                <Input.Password placeholder="讯飞开放平台 APIKey" autoComplete="new-password" />
              </Form.Item>
              <Form.Item
                label="APISecret"
                name="ocr_xfyun_api_secret"
                style={{ flex: 1, minWidth: 200 }}
                extra={configured.ocr_xfyun_api_secret ? '已保存密钥，留空表示不修改' : undefined}
              >
                <Input.Password placeholder="讯飞开放平台 APISecret" autoComplete="new-password" />
              </Form.Item>
            </Space>
            <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: -8 }}>
              讯飞开放平台通用文字识别（支持印刷体与手写体）：请求 URL 按 hmac-sha256
              签名（host/date/authorization，服务器时钟偏差需小于 5 分钟），图片以 base64
              JSON 提交（≤4M）。接口路径二选一：/v1/private/sf8e6aca1（通用文字识别，
              中英文）或 /v1/private/hh_ocr_recognize_doc（intsig，52
              种语种），需在控制台为应用开通对应服务并核对密钥（重置过的密钥立即失效）。
              免费额度与并发限制以控制台为准。
            </Typography.Paragraph>
          </>
        ) : (
          <Form.Item label="识别类别" name="ocr_category" style={{ maxWidth: 320 }}>
            <Input placeholder="atlas.doc" />
          </Form.Item>
        )}

        <Divider orientation="left" plain>
          知识库服务
        </Divider>
        <Form.Item
          label={
            <Space>
              服务地址 {testTag('kb')}
              <Button
                size="small"
                type="link"
                icon={<ApiOutlined />}
                onClick={() => handleTest('kb')}
              >
                测试
              </Button>
            </Space>
          }
          name="kb_base_url"
        >
          <Input placeholder="http://localhost:8000" />
        </Form.Item>
        <Form.Item
          label="知识库"
          name="kb_id"
          extra={
            <span style={{ fontSize: 12 }}>
              审核时模型自主判断是否检索该库获取法规依据
            </span>
          }
        >
          <Select
            allowClear
            placeholder="选择知识库（先点上方「测试」加载列表）"
            options={kbs.map((kb) => ({
              value: kb.id,
              label: `${kb.name}（${kb.document_count} 篇文档）`,
            }))}
          />
        </Form.Item>
        <Form.Item
          label={
            <Space size={4}>
              知识库 API 密钥 {keyTag('kb_api_key')}
            </Space>
          }
          name="kb_api_key"
          extra={
            <span style={{ fontSize: 12 }}>
              可选；若知识库部署开启了鉴权，请填写访问令牌（留空表示无需密钥）
            </span>
          }
        >
          <Input.Password placeholder="已配置则留空保持不变" />
        </Form.Item>
        <Space size={10}>
          <Form.Item label="启用知识库" name="kb_enabled" valuePropName="checked">
            <Switch size="small" />
          </Form.Item>
          <Form.Item label="单批最大检索次数" name="kb_max_queries">
            <InputNumber min={1} max={20} style={{ width: 100 }} />
          </Form.Item>
          <Form.Item label="超时(秒)" name="kb_timeout">
            <InputNumber min={30} max={1200} style={{ width: 100 }} />
          </Form.Item>
        </Space>

        <Divider orientation="left" plain>
          法规解析
        </Divider>
        <Space size={10}>
          <Form.Item
            label="任务默认审核规则条数"
            name="legal_max_rules"
            extra={
              <span style={{ fontSize: 12 }}>
                法规解析结果全量保存、不截断；创建审核任务引用法规规则集时，每个规则集默认按分数取前 N 条参与审核，默认 30 条。
              </span>
            }
          >
            <InputNumber min={1} max={200} style={{ width: 110 }} />
          </Form.Item>
        </Space>

        <Divider orientation="left" plain>
          联网搜索（可选）
        </Divider>
        <Form.Item label="启用联网搜索" name="web_search_enabled" valuePropName="checked">
          <Switch size="small" />
        </Form.Item>
        <Form.Item label="搜索API" name="web_search_api">
          <Select
            options={[
              { value: 'tavily', label: 'Tavily Search' },
              { value: 'bing', label: 'Bing Web Search' },
              { value: 'serper', label: 'Serper (Google)' },
            ]}
          />
        </Form.Item>
        <Form.Item
          label={
            <Space>
              API 密钥 {keyTag('web_search_api_key')} {testTag('web_search')}
              <Button
                size="small"
                type="link"
                icon={<ApiOutlined />}
                onClick={() => handleTest('web_search')}
              >
                测试
              </Button>
            </Space>
          }
          name="web_search_api_key"
          extra={
            <span style={{ fontSize: 12 }}>
              用于获取最新政策、行业动态等实时信息（需自行申请API）
            </span>
          }
        >
          <Input.Password placeholder="已配置则留空保持不变" />
        </Form.Item>
        <Space size={10}>
          <Form.Item label="最大结果数" name="web_search_max_results">
            <InputNumber min={1} max={10} style={{ width: 100 }} />
          </Form.Item>
          <Form.Item label="超时(秒)" name="web_search_timeout">
            <InputNumber min={5} max={120} style={{ width: 100 }} />
          </Form.Item>
        </Space>

        <Divider orientation="left" plain>
          审核参数
        </Divider>
        <Space size={10}>
          <Form.Item
            label="单文件最大字数"
            name="max_chars_per_doc"
            extra={<span style={{ fontSize: 12 }}>超出部分保留首尾</span>}
          >
            <InputNumber min={5000} max={200000} step={5000} style={{ width: 130 }} />
          </Form.Item>
          <Form.Item
            label="并发批次数"
            name="concurrency"
            extra={<span style={{ fontSize: 12 }}>确定性模式下强制串行</span>}
          >
            <InputNumber min={1} max={16} style={{ width: 100 }} />
          </Form.Item>
        </Space>

        <Divider orientation="left" plain>
          缓存配置
        </Divider>
        <Form.Item
          label="启用结论缓存"
          name="findings_cache_enabled"
          valuePropName="checked"
          extra={
            <span style={{ fontSize: 12 }}>
              开启后相同「文件(MD5)+规则(内容指纹)」批次复用历史审核结论，跳过 LLM 调用以提速；
              文件或规则任一变动即自动失效重跑。关闭则每次均重新审核（强制重跑场景用）。
            </span>
          }
        >
          <Switch size="small" />
        </Form.Item>
        <Form.Item
          label="启用一致性摘要缓存"
          name="consistency_cache_enabled"
          valuePropName="checked"
          extra={
            <span style={{ fontSize: 12 }}>
              开启后一致性阶段的分段摘要与要素提取结果复用历史缓存，二次审核秒回；
              关闭则一致性阶段每次重新生成摘要（超大文档耗时明显增加）。
            </span>
          }
        >
          <Switch size="small" />
        </Form.Item>

        <Divider orientation="left" plain>
          跨文件一致性核查
        </Divider>
        <Form.Item
          label="一致性核查开关"
          name="consistency_enabled"
          extra={
            <span style={{ fontSize: 12 }}>
              规则驱动（默认）：仅当任务所含规则集中包含「一致性」类规则时才执行；
              强制开启：始终执行（旧行为）；关闭：跳过一致性核查。
              核查的核心要素与要点均取自一致性类规则本身，可在规则编辑器中配置。
            </span>
          }
        >
          <Select
            style={{ width: 220 }}
            options={[
              { value: 'auto', label: '规则驱动（默认）' },
              { value: 'on', label: '强制开启' },
              { value: 'off', label: '关闭' },
            ]}
          />
        </Form.Item>

        <Divider orientation="left" plain>
          确定性执行（可重复审核一致）
        </Divider>
        <Form.Item
          label="启用确定性执行"
          name="deterministic_mode"
          valuePropName="checked"
          extra={
            <span style={{ fontSize: 12 }}>
              开启后固定规则执行顺序、温度=0、串行执行、不依赖系统时间，并对输出做归一化，
              确保相同文件+相同规则多次审核结果稳定可追溯。关闭时回落高吞吐并发模式。
            </span>
          }
        >
          <Switch size="small" />
        </Form.Item>
        <Space size={10}>
          <Form.Item
            label="确定性温度"
            name="deterministic_temperature"
            extra={<span style={{ fontSize: 12 }}>固定采样温度（0=关闭采样）</span>}
          >
            <InputNumber min={0} max={1} step={0.1} style={{ width: 90 }} />
          </Form.Item>
          <Form.Item
            label="数据快照版本"
            name="data_snapshot_version"
            extra={<span style={{ fontSize: 12 }}>规则依赖的外部数据(如KB)版本</span>}
          >
            <Input style={{ width: 200 }} placeholder="KB_BASELINE_v2026.08.01" />
          </Form.Item>
        </Space>
        <Form.Item
          label="以系统时间作为判定依据"
          name="use_current_time_as_basis"
          valuePropName="checked"
          extra={
            <span style={{ fontSize: 12 }}>
              强烈建议保持关闭：审核结论必须仅依赖文档内容与规则，不依赖运行时刻。
            </span>
          }
        >
          <Switch size="small" />
        </Form.Item>

        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8 }}>
          配置保存在后端本地文件，重启后仍生效。也可用环境变量 BCR_* 覆盖。
        </Typography.Paragraph>

        {storage && (
          <>
            <Divider orientation="left" plain>
              <Space size={4}>
                <DatabaseOutlined />
                数据存储
              </Space>
            </Divider>
            <Descriptions
              size="small"
              column={1}
              labelStyle={{ width: 130 }}
              items={[
                {
                  key: 'driver',
                  label: '结构化数据',
                  children: (
                    <Space size={6}>
                      <Tag color="blue">{storage.structured_driver}</Tag>
                      <span style={{ fontSize: 12, color: '#6b7280' }}>
                        集合后端 {storage.collections_backend}
                      </span>
                    </Space>
                  ),
                },
                {
                  key: 'files',
                  label: '文件存储',
                  children: (
                    <Space size={6}>
                      <Tag color="blue">{storage.files_driver}</Tag>
                      <Typography.Text code style={{ fontSize: 12 }}>
                        {storage.files_base_dir}
                      </Typography.Text>
                    </Space>
                  ),
                },
                {
                  key: 'hot',
                  label: '热加载',
                  children: (
                    <Space size={6}>
                      {storage.hot_reload.enabled ? (
                        <Tag color="success">开启（{storage.hot_reload.interval_seconds}s）</Tag>
                      ) : (
                        <Tag>关闭</Tag>
                      )}
                      {storage.last_error && (
                        <Tag color="error" style={{ maxWidth: 260 }}>
                          最近错误：{storage.last_error.slice(0, 60)}
                        </Tag>
                      )}
                      {storage.last_reload_at && !storage.last_error && (
                        <span style={{ fontSize: 12, color: '#6b7280' }}>
                          最近变更 {storage.last_reload_at}
                        </span>
                      )}
                    </Space>
                  ),
                },
                {
                  key: 'cfg',
                  label: '配置文件',
                  children: (
                    <Space size={6}>
                      <Typography.Text code style={{ fontSize: 12 }}>
                        {storage.config_path}
                      </Typography.Text>
                      {storage.config_file_exists ? (
                        <Tag color="default">已创建</Tag>
                      ) : (
                        <Tag color="warning">未创建（使用内置默认）</Tag>
                      )}
                    </Space>
                  ),
                },
              ]}
            />
            <Typography.Paragraph type="secondary" style={{ fontSize: 12, margin: '6px 0 0' }}>
              修改服务器上的存储配置文件后自动生效（结构化数据驱动 / 文件目录均可切换），
              也可点击下方按钮立即重载。样例见后端 storage_config.example.json。
            </Typography.Paragraph>
            <Button
              size="small"
              icon={<ReloadOutlined />}
              loading={reloadingStorage}
              onClick={() => void handleStorageReload()}
              style={{ marginTop: 8 }}
            >
              立即重载存储配置
            </Button>
          </>
        )}
      </Form>
    </Drawer>
  )
}
