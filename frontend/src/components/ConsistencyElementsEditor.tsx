import { useMemo } from 'react'
import { Button, Empty, Input, Select, Space, Tooltip, Typography } from 'antd'
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons'
import type { ConsistencyElement } from '../types'
import SynonymTagsInput from './SynonymTagsInput'

interface Props {
  value: ConsistencyElement[]
  library: ConsistencyElement[]
  onChange: (next: ConsistencyElement[]) => void
}

/**
 * 一致性规则的「核心要素」编辑器。
 *
 * 每条要素 = 规范名 + 同义写法（别名）+ 比对要求。要素会注入跨文件一致性核查提示词，
 * 模型据此逐文件提取取值并统一归并到规范名下比对，因此规范名即跨文件对齐的 key。
 */
export default function ConsistencyElementsEditor({ value, library, onChange }: Props) {
  const groupedOptions = useMemo(() => {
    const map = new Map<string, ConsistencyElement[]>()
    for (const e of library) {
      const g = e.group || '未分组'
      const list = map.get(g) ?? []
      list.push(e)
      map.set(g, list)
    }
    return Array.from(map, ([label, items]) => ({
      label,
      options: items.map((e) => ({ value: e.name, label: e.name })),
    }))
  }, [library])

  const usedNames = useMemo(() => new Set(value.map((e) => e.name)), [value])

  const commit = (next: ConsistencyElement[]) => onChange(next)

  const addFromLibrary = (name: string) => {
    if (!name || usedNames.has(name)) return
    const preset = library.find((e) => e.name === name)
    commit([
      ...value,
      {
        name,
        synonyms: preset?.synonyms?.length ? [...preset.synonyms] : [name],
        note: preset?.note || '',
      },
    ])
  }

  const addCustom = () => {
    let name = '自定义要素'
    let n = 1
    while (usedNames.has(name)) {
      n += 1
      name = `自定义要素${n}`
    }
    commit([...value, { name, synonyms: [name], note: '' }])
  }

  const patch = (idx: number, part: Partial<ConsistencyElement>) => {
    commit(value.map((e, i) => (i === idx ? { ...e, ...part } : e)))
  }

  return (
    <div>
      <Space direction="vertical" size={10} style={{ width: '100%' }}>
        <Space.Compact style={{ width: '100%' }}>
          <Select
            style={{ width: '100%' }}
            placeholder="从要素库添加（支持搜索，如：报价金额、项目经理）"
            value={null}
            showSearch
            optionFilterProp="label"
            options={groupedOptions}
            onChange={addFromLibrary}
            notFoundContent="要素库中无匹配项"
          />
          <Tooltip title="新增一个要素库之外的自定义要素">
            <Button icon={<PlusOutlined />} onClick={addCustom}>
              自定义要素
            </Button>
          </Tooltip>
        </Space.Compact>

        {value.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="尚未配置要素；留空时引擎会按规则文本自动推断"
            style={{ margin: '8px 0' }}
          />
        ) : (
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            {value.map((el, idx) => (
              <div
                key={`${el.name}-${idx}`}
                style={{
                  border: '1px solid var(--color-border-tertiary, rgba(255,255,255,0.15))',
                  borderRadius: 8,
                  padding: 10,
                }}
              >
                <Space.Compact style={{ width: '100%' }}>
                  <Input
                    value={el.name}
                    style={{ width: '100%' }}
                    placeholder="要素规范名，如：报价金额"
                    onChange={(e) => patch(idx, { name: e.target.value })}
                  />
                  <Tooltip title="移除该要素">
                    <Button
                      danger
                      icon={<DeleteOutlined />}
                      onClick={() => commit(value.filter((_, i) => i !== idx))}
                    />
                  </Tooltip>
                </Space.Compact>

                <div style={{ marginTop: 8 }}>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    同义写法（用于定位该要素的不同表述）
                  </Typography.Text>
                  {/* 与章节库共用同一套同义词编辑交互 */}
                  <SynonymTagsInput
                    value={el.synonyms || []}
                    onChange={(next) => patch(idx, { synonyms: next })}
                    emptyHint="未配置，将只按规范名查找"
                  />
                </div>

                <Input
                  size="small"
                  style={{ marginTop: 8 }}
                  value={el.note || ''}
                  placeholder="比对要求（选填），如：大小写金额须一致"
                  onChange={(e) => patch(idx, { note: e.target.value })}
                />
              </div>
            ))}
          </Space>
        )}
      </Space>
    </div>
  )
}
