import { useState } from 'react'
import {
  Alert,
  Button,
  Modal,
  Select,
  Space,
  Table,
  Typography,
  Upload,
} from 'antd'
import {
  DownloadOutlined,
  InboxOutlined,
  UploadOutlined,
} from '@ant-design/icons'
import { App as AntdApp } from 'antd'
import { api } from '../services/api'

interface Props {
  open: boolean
  customRulesets: { id: string; name: string }[]
  activeRulesetId: string
  onCancel: () => void
  onImported: (rulesetId: string) => void
}

interface ImportResult {
  imported: number
  skipped: number
  errors: string[]
  ruleset_id: string
  ruleset_name: string
  rulesets?: { ruleset_id: string; ruleset_name: string; imported: number }[]
}

export default function RuleImportModal({
  open,
  customRulesets,
  activeRulesetId,
  onCancel,
  onImported,
}: Props) {
  const { message } = AntdApp.useApp()
  const [file, setFile] = useState<File | null>(null)
  const [target, setTarget] = useState<'new' | 'append'>('new')
  const [name, setName] = useState('')
  const [importing, setImporting] = useState(false)
  const [result, setResult] = useState<ImportResult | null>(null)

  const isActiveCustom = customRulesets.some((r) => r.id === activeRulesetId)

  const reset = () => {
    setFile(null)
    setTarget('new')
    setName('')
    setResult(null)
  }

  const handleDownloadTemplate = async (format: 'csv' | 'xlsx') => {
    try {
      await api.downloadRuleTemplate(format)
      message.success(`已下载${format.toUpperCase()}导入模板`)
    } catch (err) {
      message.error((err as Error).message)
    }
  }

  const handleImport = async () => {
    if (!file) {
      message.warning('请先选择要导入的文件')
      return
    }
    setImporting(true)
    setResult(null)
    try {
      const res = await api.importRules(file, {
        rulesetName: target === 'new' ? name.trim() || undefined : undefined,
        appendTo: target === 'append' && isActiveCustom ? activeRulesetId : undefined,
      })
      setResult({
        imported: res.imported,
        skipped: res.skipped,
        errors: res.errors,
        ruleset_id: res.ruleset_id,
        ruleset_name: res.ruleset_name,
      })
      message.success(`成功导入 ${res.imported} 条规则`)
      onImported(res.ruleset_id)
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setImporting(false)
    }
  }

  const errorColumns = [{ title: '行/问题', dataIndex: 'msg', key: 'msg' }]

  return (
    <Modal
      title="批量导入规则"
      open={open}
      width={640}
      okText="开始导入"
      cancelText="关闭"
      okButtonProps={{ icon: <UploadOutlined />, loading: importing }}
      onOk={handleImport}
      onCancel={() => {
        reset()
        onCancel()
      }}
    >
      <Space direction="vertical" size={14} style={{ width: '100%', marginTop: 8 }}>
        <Alert
          type="info"
          showIcon
          message="支持 CSV / Excel（.xlsx / .xls）格式，按模板字段批量导入多条规则"
          description="点击下方「下载模板」获取标准模板与字段说明；以「#」「示例」开头的行会被跳过。"
        />

        <Space wrap>
          <Button icon={<DownloadOutlined />} onClick={() => handleDownloadTemplate('csv')}>
            下载 CSV 模板
          </Button>
          <Button icon={<DownloadOutlined />} onClick={() => handleDownloadTemplate('xlsx')}>
            下载 Excel 模板
          </Button>
        </Space>

        <div>
          <Typography.Text strong>选择文件</Typography.Text>
          <Upload
            accept=".csv,.xlsx,.xls"
            maxCount={1}
            beforeUpload={(f) => {
              setFile(f as unknown as File)
              return false
            }}
            onRemove={() => setFile(null)}
            fileList={file ? [{ uid: '-1', name: file.name } as any] : []}
          >
            <Button icon={<InboxOutlined />} style={{ marginTop: 6 }}>
              选择 CSV / Excel 文件
            </Button>
          </Upload>
        </div>

        <div>
          <Typography.Text strong>导入目标</Typography.Text>
          <Select
            style={{ width: '100%', marginTop: 6 }}
            value={target}
            onChange={setTarget}
            options={[
              {
                value: 'new',
                label: '新建规则集（按文件名或下方名称创建）',
              },
              ...(isActiveCustom
                ? [
                    {
                      value: 'append',
                      label: `追加到当前规则集「${customRulesets.find((r) => r.id === activeRulesetId)?.name}」`,
                    },
                  ]
                : []),
            ]}
          />
          {target === 'new' && (
            <Button
              type="link"
              size="small"
              style={{ paddingInline: 0, marginTop: 4 }}
              onClick={() => {
                setName('')
              }}
            >
              留空则按文件名自动命名
            </Button>
          )}
          {target === 'new' && (
            <input
              className="rm-import-name"
              placeholder="自定义规则集名称（选填）"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          )}
        </div>

        {result && (
          <Alert
            type={result.imported > 0 ? 'success' : 'error'}
            showIcon
            message={`已导入 ${result.imported} 条规则 · 跳过 ${result.skipped} 条`}
            description={
              result.imported > 0 ? (
                <span>
                  已写入{result.rulesets && result.rulesets.length > 1
                    ? ` ${result.rulesets.length} 个规则集`
                    : `规则集「${result.ruleset_name}」`}
                  ，可在左侧「我的规则集」中查看与管理。
                </span>
              ) : (
                <span>未导入任何规则，请检查文件字段或下载模板对照。</span>
              )
            }
          />
        )}

        {result && result.errors.length > 0 && (
          <div>
            <Typography.Text type="danger">
              错误明细（{result.errors.length}）
            </Typography.Text>
            <Table
              size="small"
              rowKey={(r) => r.msg}
              pagination={result.errors.length > 8 ? { pageSize: 8 } : false}
              columns={errorColumns}
              dataSource={result.errors.map((msg) => ({ msg }))}
              style={{ marginTop: 6 }}
            />
          </div>
        )}
      </Space>
    </Modal>
  )
}
