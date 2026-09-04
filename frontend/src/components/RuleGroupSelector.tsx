import { useMemo } from 'react'
import { Alert, Card, Select, Space, Switch, Typography } from 'antd'
import type { FileType, RuleGroup, UploadedFile } from '../types'

interface Props {
  ruleGroups: RuleGroup[]
  selectedGroupIds: string[]
  onSelectedGroupsChange: (ids: string[]) => void
  autoMatch: boolean
  onAutoMatchChange: (v: boolean) => void
  fileTypes: FileType[]
  files: UploadedFile[]
}

/**
 * 审核规则组多选 + 自动匹配开关。
 * - 规则组可多选套用到本次任务。
 * - 开启「自动匹配规则组」后，依用户手动指定的文件类型并集套用其关联规则组；
 *   系统不自动识别文件内容，仅依用户指定的类型匹配。
 */
export default function RuleGroupSelector({
  ruleGroups,
  selectedGroupIds,
  onSelectedGroupsChange,
  autoMatch,
  onAutoMatchChange,
  fileTypes,
  files,
}: Props) {
  // 自动匹配：依所选文件的类型并集其关联规则组
  const matchedByTypes = useMemo(() => {
    if (!autoMatch) return [] as string[]
    const set = new Set<string>()
    files.forEach((f) => {
      const ft = fileTypes.find((t) => t.id === f.file_type)
      if (ft) ft.rule_group_ids.forEach((g) => set.add(g))
    })
    return [...set]
  }, [autoMatch, fileTypes, files])

  const matchedNames = useMemo(
    () => matchedByTypes.map((g) => ruleGroups.find((r) => r.id === g)?.name).filter(Boolean) as string[],
    [matchedByTypes, ruleGroups],
  )

  return (
    <Card size="small" title="审核规则组">
      <Space direction="vertical" size={10} style={{ width: '100%' }}>
        <div>
          <Space>
            <Switch checked={autoMatch} onChange={onAutoMatchChange} />
            <span style={{ fontSize: 13 }}>自动根据文件类型匹配规则组</span>
          </Space>
          <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 4 }}>
            开启后，系统依你手动指定的文件类型自动套用其关联规则组（系统不自动识别文件内容）。
          </Typography.Text>
        </div>

        {autoMatch && (
          <Alert
            type={matchedNames.length ? 'info' : 'warning'}
            showIcon
            style={{ fontSize: 12 }}
            message={
              matchedNames.length
                ? `已依据所选文件类型自动匹配规则组：${matchedNames.join(' / ')}`
                : '当前文件尚未指定类型或未关联规则组，请先为每个文件指定类型。'
            }
          />
        )}

        <div>
          <Typography.Text style={{ fontSize: 13, display: 'block', marginBottom: 6 }}>
            选择本次审核使用的规则组（可多选）
          </Typography.Text>
          <Select
            mode="multiple"
            size="small"
            style={{ width: '100%' }}
            placeholder="请选择规则组"
            value={selectedGroupIds}
            onChange={onSelectedGroupsChange}
            options={ruleGroups.map((g) => ({
              value: g.id,
              label: `${g.name}（${g.rule_ids.length} 条规则）`,
            }))}
          />
          <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 4 }}>
            规则组在「审核规则组」页管理；其下具体规则见「审核规则」页。
          </Typography.Text>
        </div>
      </Space>
    </Card>
  )
}
