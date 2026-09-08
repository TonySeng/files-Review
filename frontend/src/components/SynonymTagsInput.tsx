import { useState } from 'react'
import { Input, Tag, Typography } from 'antd'

interface Props {
  value: string[]
  onChange: (next: string[]) => void
  /** 未配置同义词时的提示文案 */
  emptyHint?: string
  placeholder?: string
}

/**
 * 同义写法（别名）编辑器：Tag 展示 + 输入回车添加，支持逗号/顿号/空格分隔批量录入。
 *
 * 与「一致性规则配置要素」的同义词交互保持一致（同一套组件，章节库直接复用），
 * 保证两处配置体验统一：规范名会自动作为首个别名由调用方补齐。
 */
export default function SynonymTagsInput({
  value,
  onChange,
  emptyHint = '未配置，将只按名称匹配',
  placeholder = '输入别名后回车添加，多个可用逗号分隔',
}: Props) {
  const [draft, setDraft] = useState('')

  const add = () => {
    const parts = draft
      .split(/[,，、\s]+/)
      .map((s) => s.trim())
      .filter(Boolean)
    if (!parts.length) return
    const merged = [...value]
    for (const p of parts) {
      if (!merged.includes(p)) merged.push(p)
    }
    onChange(merged)
    setDraft('')
  }

  const remove = (alias: string) => onChange(value.filter((s) => s !== alias))

  return (
    <div>
      {value.length ? (
        <div>
          {value.map((s) => (
            <Tag key={s} closable onClose={() => remove(s)} style={{ marginBottom: 4 }}>
              {s}
            </Tag>
          ))}
        </div>
      ) : (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {emptyHint}
        </Typography.Text>
      )}
      <Input
        size="small"
        value={draft}
        placeholder={placeholder}
        onChange={(e) => setDraft(e.target.value)}
        onPressEnter={add}
        onBlur={add}
        style={{ marginTop: 4 }}
      />
    </div>
  )
}
