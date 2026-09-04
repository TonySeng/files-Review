import { useRef, useState } from 'react'
import {
  App as AntdApp,
  Alert,
  Button,
  Card,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Upload,
} from 'antd'
import {
  DeleteOutlined,
  EyeOutlined,
  FileOutlined,
  InboxOutlined,
  ScanOutlined,
} from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { api } from '../services/api'
import type { FileRole, FileType, UploadedFile } from '../types'

interface Props {
  files: UploadedFile[]
  selected: string[]
  onSelectedChange: (ids: string[]) => void
  onFilesChange: (files: UploadedFile[]) => void
  fileTypes: FileType[]
  onFileTypeChange: (fileId: string, fileType: string | null) => void
  disabled?: boolean
}

/** 依据扩展名返回文件图标颜色，提升列表可读性（符合大厂列表规范）。 */
function fileIconColor(ext?: string): string {
  const e = (ext || '').toLowerCase()
  if (e === '.pdf') return '#ff4d4f'
  if (e === '.docx' || e === '.doc') return '#1677ff'
  if (e === '.xlsx' || e === '.xls') return '#52c41a'
  if (['.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.webp', '.gif'].includes(e))
    return '#13c2c2'
  return '#8c8c8c'
}

export default function FilePanel({
  files,
  selected,
  onSelectedChange,
  onFilesChange,
  fileTypes,
  onFileTypeChange,
  disabled,
}: Props) {
  const { message } = AntdApp.useApp()
  const [uploading, setUploading] = useState(false)
  const [preview, setPreview] = useState<{ title: string; text: string } | null>(null)

  // 批量上传队列：beforeUpload 每次回调收集一个真实文件对象，用微任务合并为一次上传，
  // 避免 antd 多文件场景下的重复上传与 fileList 包装对象误用。
  const uploadQueue = useRef<File[]>([])
  const flushTimer = useRef<number | null>(null)

  const flushUpload = () => {
    flushTimer.current = null
    const batch = uploadQueue.current
    uploadQueue.current = []
    if (batch.length) void handleUpload(batch, 'bid')
  }

  const handleUpload = async (fileList: File[], role: FileRole) => {
    setUploading(true)
    try {
      const res = await api.uploadFiles(fileList, fileList.map(() => role))
      if (res.errors.length) {
        message.error(
          `${res.errors.length} 个文件被拒绝：${res.errors
            .map((e) => `${e.filename}（${e.message}）`)
            .join('；')}`,
        )
      }
      if (res.files.length) {
        const next = [...files, ...res.files]
        onFilesChange(next)
        onSelectedChange([...selected, ...res.files.map((f) => f.file_id)])
        message.success(`成功解析 ${res.files.length} 个文件，请为每个文件指定类型`)
      }
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setUploading(false)
    }
  }

  const handleFileTypeChange = (fileId: string, value: string) => {
    onFileTypeChange(fileId, value || null)
  }

  const handleDelete = async (fileId: string) => {
    try {
      await api.deleteFile(fileId)
      onFilesChange(files.filter((f) => f.file_id !== fileId))
      onSelectedChange(selected.filter((id) => id !== fileId))
    } catch (err) {
      message.error((err as Error).message)
    }
  }

  const handlePreview = async (file: UploadedFile) => {
    try {
      const res = await api.getFileText(file.file_id)
      setPreview({
        title: `${res.filename}（${res.char_count} 字${res.truncated ? '，已截断' : ''}）`,
        text: res.text || '（无文本内容）',
      })
    } catch (err) {
      message.error((err as Error).message)
    }
  }

  const columns: ColumnsType<UploadedFile> = [
    {
      title: '文件名',
      dataIndex: 'filename',
      ellipsis: true,
      render: (name: string, row) => (
        <Space size={6}>
          <FileOutlined style={{ color: fileIconColor(row.ext), flex: '0 0 auto' }} />
          <Tooltip title={name}>
            <span className="file-name-cell">{name || '（未命名）'}</span>
          </Tooltip>
          {row.used_ocr && (
            <Tooltip title="包含扫描页，已通过 OCR 识别">
              <ScanOutlined style={{ color: '#fa8c16', flex: '0 0 auto' }} />
            </Tooltip>
          )}
        </Space>
      ),
    },
    {
      title: '文件类型（手动指定）',
      dataIndex: 'file_type',
      width: 160,
      render: (ft: string | null | undefined, row) => (
        <Select
          size="small"
          value={ft || undefined}
          disabled={disabled}
          style={{ width: 140 }}
          placeholder="请指定类型"
          options={fileTypes.map((t) => ({ value: t.id, label: t.name }))}
          onChange={(v) => handleFileTypeChange(row.file_id, v)}
        />
      ),
    },
    {
      title: '字数',
      dataIndex: 'char_count',
      width: 90,
      align: 'right',
      render: (n: number) => n.toLocaleString(),
    },
    {
      title: '状态',
      dataIndex: 'validation',
      width: 120,
      render: (_: unknown, row: UploadedFile) => {
        if (row.parse_error) {
          return (
            <Tooltip title={row.parse_error}>
              <Tag color="error">解析异常</Tag>
            </Tooltip>
          )
        }
        const lvl = row.validation?.level
        if (lvl === 'error' || lvl === 'warn') {
          return (
            <Tooltip title={(row.validation?.messages || []).join('；')}>
              <Tag color={lvl === 'error' ? 'error' : 'warning'}>
                {lvl === 'error' ? '校验未通过' : '校验提醒'}
              </Tag>
            </Tooltip>
          )
        }
        return <Tag color="success">已解析</Tag>
      },
    },
    {
      title: '操作',
      width: 90,
      render: (_, row) => (
        <Space size={2}>
          <Button
            type="text"
            size="small"
            icon={<EyeOutlined />}
            onClick={() => handlePreview(row)}
          />
          <Popconfirm
            title="确认删除该文件？"
            description="删除后需重新上传"
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            disabled={disabled}
            onConfirm={() => handleDelete(row.file_id)}
          >
            <Button
              type="text"
              size="small"
              danger
              disabled={disabled}
              icon={<DeleteOutlined />}
            />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <Card
      title="待审文件"
      size="small"
      extra={<span className="upload-hint">已选 {selected.length} / {files.length}</span>}
    >
      <Upload.Dragger
        multiple
        disabled={disabled || uploading}
        showUploadList={false}
        accept=".pdf,.docx,.xlsx,.xls,.txt,.md,.png,.jpg,.jpeg,.bmp,.tiff,.webp"
        beforeUpload={(file) => {
          // file 为真实文件对象（RcFile，继承自 File）；多文件时在微任务中合并成一次上传
          uploadQueue.current.push(file as unknown as File)
          if (flushTimer.current == null) {
            flushTimer.current = window.setTimeout(flushUpload, 0)
          }
          return false
        }}
        style={{ marginBottom: 12, padding: '10px 0' }}
      >
        <p style={{ margin: 0 }}>
          <InboxOutlined style={{ fontSize: 22, color: '#1668dc' }} />
        </p>
        <p style={{ margin: '6px 0 2px', fontSize: 13 }}>点击或拖拽文件到此处上传</p>
        <p className="upload-hint" style={{ margin: 0 }}>
          支持 PDF / Word / Excel / 图片，扫描件自动 OCR，上传后请手动指定文件类型
        </p>
      </Upload.Dragger>

      {files.length > 0 && files.some((f) => !f.file_type) ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="请为每个文件指定类型"
          description="系统不会自动识别文件内容，需手动指定类型；开启「自动匹配规则组」后将依类型自动套用对应规则组。"
        />
      ) : null}

      <Table
        size="small"
        rowKey="file_id"
        loading={uploading}
        columns={columns}
        dataSource={files}
        pagination={false}
        scroll={{ y: 260 }}
        locale={{ emptyText: '暂无文件，请上传招标文件、投标文件或附件' }}
        rowSelection={{
          selectedRowKeys: selected,
          onChange: (keys) => onSelectedChange(keys as string[]),
          getCheckboxProps: () => ({ disabled }),
        }}
      />

      <Modal
        open={!!preview}
        title={preview?.title}
        width={860}
        footer={null}
        onCancel={() => setPreview(null)}
      >
        <div
          style={{
            maxHeight: '62vh',
            overflowY: 'auto',
            whiteSpace: 'pre-wrap',
            fontSize: 13,
            lineHeight: 1.8,
            background: '#fafafa',
            padding: 12,
            borderRadius: 4,
          }}
        >
          {preview?.text}
        </div>
      </Modal>
    </Card>
  )
}
