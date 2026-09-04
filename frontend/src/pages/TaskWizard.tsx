import { useMemo, useState } from 'react'
import {
  Button,
  Card,
  Descriptions,
  Space,
  Steps,
  Tag,
  Typography,
} from 'antd'
import { LeftOutlined, RightOutlined } from '@ant-design/icons'
import FilePanel from '../components/FilePanel'
import RuleSelection, { type RuleConfigMode } from '../components/RuleSelection'
import ReviewConfigPanel from '../components/ReviewConfigPanel'
import type {
  FileType,
  KnowledgeBase,
  ReviewMode,
  RuleGroup,
  RuleSet,
  UploadedFile,
} from '../types'

interface Props {
  files: UploadedFile[]
  onFilesChange: (f: UploadedFile[]) => void
  fileTypes: FileType[]
  onFileTypeChange: (fileId: string, fileType: string | null) => void
  selectedFiles: string[]
  onSelectedFilesChange: (ids: string[]) => void
  rulesets: RuleSet[]
  activeRulesetId: string
  selectedRuleIds: string[]
  onRulesetChange: (id: string) => void
  onSelectedRuleIdsChange: (ids: string[]) => void
  ruleGroups: RuleGroup[]
  selectedRuleGroupIds: string[]
  onSelectedRuleGroupsChange: (ids: string[]) => void
  autoMatch: boolean
  onAutoMatchChange: (v: boolean) => void
  ruleConfigMode: RuleConfigMode
  onRuleConfigModeChange: (m: RuleConfigMode) => void
  mode: ReviewMode
  selectedLegalRulesetId: string
  onSelectedLegalRulesetIdChange: (id: string) => void
  legalRulesOnly: boolean
  onLegalRulesOnlyChange: (v: boolean) => void
  kbEnabled: boolean
  onKbEnabledChange: (v: boolean) => void
  knowledgeBases: KnowledgeBase[]
  kbId: string
  onKbIdChange: (v: string) => void
  webSearchEnabled: boolean
  onWebSearchEnabledChange: (v: boolean) => void
  webSearchConfigured: boolean
  extraInstruction: string
  onExtraInstructionChange: (v: string) => void
  canStart: boolean
  onOpenManagement: () => void
  onCreate: () => void
  onCancel: () => void
}

const STEP_TITLES = ['上传文件', '配置规则', '其他配置', '确认创建']

/**
 * 向导式任务创建：分步收集「文件 → 规则 → 其他配置 → 确认」，
 * 每步校验通过方可进入下一步，最终提交创建审核任务。
 */
export default function TaskWizard(props: Props) {
  const [current, setCurrent] = useState(0)

  const selectedFileNames = useMemo(
    () =>
      props.files
        .filter((f) => props.selectedFiles.includes(f.file_id))
        .map((f) => f.filename)
        .join('、'),
    [props.files, props.selectedFiles],
  )

  const filesReady = props.selectedFiles.length > 0
  const rulesReady =
    props.ruleConfigMode === 'group'
      ? props.selectedRuleGroupIds.length > 0
      : props.selectedRuleIds.length > 0

  const canNext = current === 0 ? filesReady : current === 1 ? rulesReady : true
  const isLast = current === STEP_TITLES.length - 1

  const next = () => setCurrent((c) => Math.min(c + 1, STEP_TITLES.length - 1))
  const prev = () => setCurrent((c) => Math.max(c - 1, 0))

  return (
    <div className="wizard-page">
      <Card size="small" className="wizard-card">
        <Steps
          current={current}
          items={STEP_TITLES.map((t) => ({ title: t }))}
          onChange={(c) => {
            // 仅允许回退，或前进到已通过校验的步骤
            if (c < current) setCurrent(c)
            else if (c === 1 && filesReady) setCurrent(1)
            else if (c === 2 && filesReady && rulesReady) setCurrent(2)
            else if (c === 3 && filesReady && rulesReady) setCurrent(3)
          }}
          style={{ marginBottom: 20 }}
        />

        <div className="wizard-step-body">
          {current === 0 && (
            <FilePanel
              files={props.files}
              selected={props.selectedFiles}
              onSelectedChange={props.onSelectedFilesChange}
              onFilesChange={props.onFilesChange}
              fileTypes={props.fileTypes}
              onFileTypeChange={props.onFileTypeChange}
            />
          )}

          {current === 1 && (
            <RuleSelection
              configMode={props.ruleConfigMode}
              onConfigModeChange={props.onRuleConfigModeChange}
              ruleGroups={props.ruleGroups}
              selectedGroupIds={props.selectedRuleGroupIds}
              onSelectedGroupsChange={props.onSelectedRuleGroupsChange}
              autoMatch={props.autoMatch}
              onAutoMatchChange={props.onAutoMatchChange}
              fileTypes={props.fileTypes}
              files={props.files}
              rulesets={props.rulesets}
              activeRulesetId={props.activeRulesetId}
              selectedRuleIds={props.selectedRuleIds}
              onRulesetChange={props.onRulesetChange}
              onSelectedRulesChange={props.onSelectedRuleIdsChange}
              onOpenManagement={props.onOpenManagement}
              mode={props.mode}
              selectedLegalRulesetId={props.selectedLegalRulesetId}
              onSelectedLegalRulesetIdChange={props.onSelectedLegalRulesetIdChange}
              legalRulesOnly={props.legalRulesOnly}
              onLegalRulesOnlyChange={props.onLegalRulesOnlyChange}
            />
          )}

          {current === 2 && (
            <ReviewConfigPanel
              kbEnabled={props.kbEnabled}
              onKbEnabledChange={props.onKbEnabledChange}
              knowledgeBases={props.knowledgeBases}
              kbId={props.kbId}
              onKbIdChange={props.onKbIdChange}
              webSearchEnabled={props.webSearchEnabled}
              onWebSearchEnabledChange={props.onWebSearchEnabledChange}
              webSearchConfigured={props.webSearchConfigured}
              extraInstruction={props.extraInstruction}
              onExtraInstructionChange={props.onExtraInstructionChange}
              canStart
              onStart={() => undefined}
              running={false}
              selectedFileNames={selectedFileNames}
              hideStartButton
            />
          )}

          {current === 3 && (
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
                请确认以下审核配置，点击「创建任务」后将在后台启动审核。
              </Typography.Paragraph>
              <Descriptions size="small" bordered column={1}>
                <Descriptions.Item label="审核文件">
                  {selectedFileNames || <Typography.Text type="secondary">（未选择）</Typography.Text>}
                </Descriptions.Item>
                <Descriptions.Item label="规则组">
                  {props.selectedRuleGroupIds.length ? (
                    <Space size={4} wrap>
                      {props.selectedRuleGroupIds.map((id) => (
                        <Tag key={id} color="blue">
                          {props.ruleGroups.find((g) => g.id === id)?.name || id}
                        </Tag>
                      ))}
                    </Space>
                  ) : (
                    '—'
                  )}
                </Descriptions.Item>
                <Descriptions.Item label="规则条数">
                  {props.selectedRuleIds.length} 条
                  {props.autoMatch && <Tag color="green" style={{ marginLeft: 6 }}>自动匹配开启</Tag>}
                </Descriptions.Item>
                <Descriptions.Item label="知识库">
                  {props.kbEnabled
                    ? props.kbId
                      ? props.knowledgeBases.find((k) => k.id === props.kbId)?.name || '已指定'
                      : '使用系统默认'
                    : '未启用'}
                </Descriptions.Item>
                <Descriptions.Item label="联网搜索">
                  {props.webSearchEnabled ? '已启用' : '未启用'}
                </Descriptions.Item>
                <Descriptions.Item label="补充要求">
                  {props.extraInstruction || <Typography.Text type="secondary">（无）</Typography.Text>}
                </Descriptions.Item>
              </Descriptions>
            </Space>
          )}
        </div>

        <div className="wizard-footer">
          <Button onClick={props.onCancel}>取消</Button>
          <Space>
            {current > 0 && (
              <Button onClick={prev} icon={<LeftOutlined />}>
                上一步
              </Button>
            )}
            {!isLast ? (
              <Button
                type="primary"
                disabled={!canNext}
                onClick={next}
                icon={<RightOutlined />}
              >
                下一步
              </Button>
            ) : (
              <Button
                type="primary"
                disabled={!props.canStart}
                onClick={props.onCreate}
              >
                创建任务
              </Button>
            )}
          </Space>
        </div>
      </Card>
    </div>
  )
}
