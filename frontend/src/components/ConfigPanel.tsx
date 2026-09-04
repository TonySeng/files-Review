import { Space } from 'antd'
import FilePanel from './FilePanel'
import RuleSelector from './RuleSelector'
import type {
  FileType,
  RuleGroup,
  RuleSet,
  UploadedFile,
} from '../types'
import RuleGroupSelector from './RuleGroupSelector'

interface Props {
  files: UploadedFile[]
  selectedFiles: string[]
  onFilesChange: (files: UploadedFile[]) => void
  onSelectedChange: (ids: string[]) => void
  fileTypes: FileType[]
  onFileTypeChange: (fileId: string, fileType: string | null) => void
  rulesets: RuleSet[]
  activeRulesetId: string
  selectedRuleIds: string[]
  onRulesetChange: (id: string) => void
  onSelectedRulesChange: (ids: string[]) => void
  ruleGroups: RuleGroup[]
  selectedRuleGroupIds: string[]
  onSelectedRuleGroupsChange: (ids: string[]) => void
  autoMatch: boolean
  onAutoMatchChange: (v: boolean) => void
  onOpenManagement: () => void
}

/**
 * 左侧工作台：仅承载「文件列表 + 审核规则配置」，
 * 审核选项与「开始审核」按钮已迁移至右侧（见 ReviewConfigPanel）。
 */
export default function ConfigPanel({
  files,
  selectedFiles,
  onFilesChange,
  onSelectedChange,
  fileTypes,
  onFileTypeChange,
  rulesets,
  activeRulesetId,
  selectedRuleIds,
  onRulesetChange,
  onSelectedRulesChange,
  ruleGroups,
  selectedRuleGroupIds,
  onSelectedRuleGroupsChange,
  autoMatch,
  onAutoMatchChange,
  onOpenManagement,
}: Props) {
  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }} className="config-panel">
      <FilePanel
        files={files}
        selected={selectedFiles}
        onSelectedChange={onSelectedChange}
        onFilesChange={onFilesChange}
        fileTypes={fileTypes}
        onFileTypeChange={onFileTypeChange}
      />

      <RuleGroupSelector
        ruleGroups={ruleGroups}
        selectedGroupIds={selectedRuleGroupIds}
        onSelectedGroupsChange={onSelectedRuleGroupsChange}
        autoMatch={autoMatch}
        onAutoMatchChange={onAutoMatchChange}
        fileTypes={fileTypes}
        files={files}
      />

      <RuleSelector
        rulesets={rulesets}
        activeRulesetId={activeRulesetId}
        selectedRuleIds={selectedRuleIds}
        onRulesetChange={onRulesetChange}
        onSelectedRulesChange={onSelectedRulesChange}
        onOpenManagement={onOpenManagement}
      />
    </Space>
  )
}
