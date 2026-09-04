import { Alert, Segmented, Space, Typography } from 'antd'
import RuleGroupSelector from './RuleGroupSelector'
import RuleSelector from './RuleSelector'
import LegalRulePanel from './LegalRulePanel'
import type {
  FileType,
  ReviewMode,
  RuleGroup,
  RuleSet,
  UploadedFile,
} from '../types'

/** 规则配置策略：按规则组（预设集合）/ 按规则集（可切换来源）/ 按法规文件（自动生成规则）。 */
export type RuleConfigMode = 'group' | 'ruleset' | 'legal'

interface Props {
  configMode: RuleConfigMode
  onConfigModeChange: (m: RuleConfigMode) => void
  // —— 按规则组 ——
  ruleGroups: RuleGroup[]
  selectedGroupIds: string[]
  onSelectedGroupsChange: (ids: string[]) => void
  autoMatch: boolean
  onAutoMatchChange: (v: boolean) => void
  fileTypes: FileType[]
  files: UploadedFile[]
  // —— 按规则集 ——
  rulesets: RuleSet[]
  activeRulesetId: string
  selectedRuleIds: string[]
  onRulesetChange: (id: string) => void
  onSelectedRulesChange: (ids: string[]) => void
  onOpenManagement: () => void
  // —— 按法规文件（自动生成临时规则） ——
  mode: ReviewMode
  selectedLegalRulesetId: string
  onSelectedLegalRulesetIdChange: (id: string) => void
  legalRulesOnly: boolean
  onLegalRulesOnlyChange: (v: boolean) => void
}

/**
 * 统一审核规则配置：以「策略切换」理清原「审核规则组 / 审核规则选择」两块平行逻辑。
 *
 * - 按规则组：从预置规则组中多选，系统套用其下规则；可开启按文件类型自动匹配。
 * - 按规则集：选择某规则集（可切换至其他内置/自定义规则集）后逐项勾选规则。
 *
 * 两种策略互斥、逻辑明确、操作连贯，最终都收敛为「本次送审的规则集合」。
 */
export default function RuleSelection({
  configMode,
  onConfigModeChange,
  ruleGroups,
  selectedGroupIds,
  onSelectedGroupsChange,
  autoMatch,
  onAutoMatchChange,
  fileTypes,
  files,
  rulesets,
  activeRulesetId,
  selectedRuleIds,
  onRulesetChange,
  onSelectedRulesChange,
  onOpenManagement,
  mode,
  selectedLegalRulesetId,
  onSelectedLegalRulesetIdChange,
  legalRulesOnly,
  onLegalRulesOnlyChange,
}: Props) {
  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <div className="rule-config-switch">
        <Segmented
          size="small"
          value={configMode}
          onChange={(v) => onConfigModeChange(v as RuleConfigMode)}
          options={[
            { label: '按规则组', value: 'group' },
            { label: '按规则集', value: 'ruleset' },
            { label: '按法规文件', value: 'legal' },
          ]}
        />
        <Typography.Text type="secondary" className="rule-config-hint">
          {configMode === 'group'
            ? '从预置规则组中多选，系统套用组内规则（可开启按文件类型自动匹配）。'
            : '选择规则集后可逐项勾选规则，可切换至其他内置或自定义规则集作为规则来源。'}
        </Typography.Text>
      </div>

      {configMode === 'legal' ? (
        <LegalRulePanel
          mode={mode}
          selectedRulesetId={selectedLegalRulesetId}
          onSelectedRulesetIdChange={onSelectedLegalRulesetIdChange}
          legalRulesOnly={legalRulesOnly}
          onLegalRulesOnlyChange={onLegalRulesOnlyChange}
          ruleGroups={ruleGroups}
          selectedGroupIds={selectedGroupIds}
          onSelectedGroupIdsChange={onSelectedGroupsChange}
        />
      ) : configMode === 'group' ? (
        <RuleGroupSelector
          ruleGroups={ruleGroups}
          selectedGroupIds={selectedGroupIds}
          onSelectedGroupsChange={onSelectedGroupsChange}
          autoMatch={autoMatch}
          onAutoMatchChange={onAutoMatchChange}
          fileTypes={fileTypes}
          files={files}
        />
      ) : rulesets.length > 0 ? (
        <RuleSelector
          rulesets={rulesets}
          activeRulesetId={activeRulesetId}
          selectedRuleIds={selectedRuleIds}
          onRulesetChange={onRulesetChange}
          onSelectedRulesChange={onSelectedRulesChange}
          onOpenManagement={onOpenManagement}
        />
      ) : (
        <Alert
          type="warning"
          showIcon
          message="暂无可用规则集"
          description="请先在「规则管理」中导入或创建规则集。"
        />
      )}
    </Space>
  )
}
