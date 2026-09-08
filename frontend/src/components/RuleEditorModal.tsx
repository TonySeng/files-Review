import { useEffect, useMemo, useState } from 'react'
import { App as AntdApp, Alert, Checkbox, Form, Input, Modal, Select, Space } from 'antd'
import { SaveOutlined } from '@ant-design/icons'
import { api } from '../services/api'
import type {
  ConsistencyElement,
  FileType,
  Rule,
  RuleSet,
  Section,
  Severity,
} from '../types'
import { CATEGORY_LABEL, SEVERITY_META } from './ruleMeta'
import ConsistencyElementsEditor from './ConsistencyElementsEditor'

interface Props {
  open: boolean
  ruleset: RuleSet
  rule: Rule | null
  onCancel: () => void
  onSaved: (saved: RuleSet) => void
}

export default function RuleEditorModal({ open, ruleset, rule, onCancel, onSaved }: Props) {
  const { message } = AntdApp.useApp()
  const [form] = Form.useForm()
  const [saving, setSaving] = useState(false)
  const [elements, setElements] = useState<ConsistencyElement[]>([])
  const [library, setLibrary] = useState<ConsistencyElement[]>([])
  const [fileTypes, setFileTypes] = useState<FileType[]>([])
  const [sections, setSections] = useState<Section[]>([])
  const category = Form.useWatch('category', form)
  // 关联章节的可选项随「关联文档类型」联动：章节以文件类型为维度
  const docTypes = Form.useWatch('doc_types', form) as string[] | undefined

  useEffect(() => {
    if (!open) return
    form.setFieldsValue(
      rule
        ? {
            ...rule,
            checkpoints: rule.checkpoints.join('\n'),
            doc_types: rule.doc_types ?? [],
            section_ids: rule.section_ids ?? [],
            enabled: rule.enabled ?? true,
            need_legal_basis: !!rule.need_legal_basis,
          }
        : {
            name: '',
            category: 'qualification',
            severity: 'major' as Severity,
            description: '',
            checkpoints: '',
            enabled: true,
            need_legal_basis: false,
            doc_types: [],
            section_ids: [],
          },
    )
    setElements(rule?.structured?.consistency_elements ?? [])
  }, [open, rule, form])

  useEffect(() => {
    if (!open || library.length) return
    api
      .listConsistencyElements()
      .then((r) => setLibrary(r.elements || []))
      .catch(() => setLibrary([]))
  }, [open, library.length])

  useEffect(() => {
    if (!open || fileTypes.length) return
    api
      .listFileTypes()
      .then((r) => setFileTypes(r.file_types || []))
      .catch(() => setFileTypes([]))
  }, [open, fileTypes.length])

  useEffect(() => {
    if (!open) return
    api
      .listSections()
      .then((r) => setSections(r.sections || []))
      .catch(() => setSections([]))
  }, [open])

  // 章节候选：选了文档类型则只列该类型的章节；未选则列出全部（审核时按文件自身类型匹配）
  const sectionOptions = useMemo(() => {
    const pool = docTypes?.length
      ? sections.filter((s) => docTypes.includes(s.file_type_id))
      : sections
    const groups = new Map<string, Section[]>()
    for (const s of pool) {
      const ft = fileTypes.find((f) => f.id === s.file_type_id)
      const label = ft?.name || s.file_type_id
      const list = groups.get(label) ?? []
      list.push(s)
      groups.set(label, list)
    }
    return Array.from(groups, ([label, items]) => ({
      label,
      options: items.map((s) => ({
        value: s.id,
        label: s.name + (s.enabled === false ? '（已停用）' : ''),
      })),
    }))
  }, [sections, docTypes, fileTypes])

  const isConsistency = category === 'consistency'

  const handleSave = async () => {
    const values = await form.validateFields()
    const checkpoints = String(values.checkpoints || '')
      .split('\n')
      .map((s: string) => s.trim())
      .filter(Boolean)

    // 一致性要素只随「一致性」类别落盘；切到别的类别时保留原有结构化条件但清掉要素
    const baseStructured = rule?.structured ?? undefined
    let structured: Rule['structured'] = baseStructured ?? null
    if (values.category === 'consistency') {
      const clean = elements
        .filter((e) => e.name.trim())
        .map((e) => ({
          name: e.name.trim(),
          synonyms: Array.from(
            new Set((e.synonyms || []).map((s) => s.trim()).filter(Boolean)),
          ),
          note: (e.note || '').trim(),
        }))
      structured = { ...(baseStructured || {}), consistency_elements: clean }
    } else if (structured && 'consistency_elements' in structured) {
      const { consistency_elements: _drop, ...rest } = structured
      structured = Object.keys(rest).length ? rest : null
    }

    const nextRule: Rule = {
      id: rule?.id || `custom-${Date.now().toString(36)}`,
      name: values.name,
      category: values.category,
      severity: values.severity,
      description: values.description || '',
      checkpoints,
      enabled: values.enabled ?? true,
      need_legal_basis: !!values.need_legal_basis,
      doc_types: (values.doc_types as string[] | undefined) || [],
      // 关联章节：留空=不按章节裁剪（沿用全量审核）；指定后仅对命中章节的正文审核
      section_ids: (values.section_ids as string[] | undefined) || [],
      structured,
    }

    const nextRules = rule
      ? ruleset.rules.map((r) => (r.id === rule.id ? nextRule : r))
      : [...ruleset.rules, nextRule]

    setSaving(true)
    try {
      const saved = await api.saveRuleset({
        id: ruleset.id,
        name: ruleset.name,
        description: ruleset.description,
        rules: nextRules,
      })
      message.success(rule ? '规则已更新' : '规则已新增')
      onSaved(saved)
    } catch (err) {
      message.error((err as Error).message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      title={rule ? `编辑规则：${rule.name}` : '新增审核规则'}
      open={open}
      width={640}
      okText="保存"
      cancelText="取消"
      okButtonProps={{ icon: <SaveOutlined />, loading: saving }}
      onOk={handleSave}
      onCancel={onCancel}
      destroyOnClose
    >
      <Form form={form} layout="vertical" style={{ marginTop: 12 }}>
        <Form.Item
          name="name"
          label="规则名称"
          extra="建议与审核要点一一对应，简洁明确"
          rules={[{ required: true, message: '请输入规则名称' }]}
        >
          <Input placeholder="例如：投标保证金" />
        </Form.Item>
        <Space size={12} style={{ width: '100%' }}>
          <Form.Item name="category" label="类别" style={{ width: 200 }}>
            <Select
              options={Object.entries(CATEGORY_LABEL).map(([v, l]) => ({
                value: v,
                label: l,
              }))}
            />
          </Form.Item>
          <Form.Item name="severity" label="严重级别" style={{ width: 200 }}>
            <Select
              options={Object.entries(SEVERITY_META).map(([v, m]) => ({
                value: v,
                label: m.label,
              }))}
            />
          </Form.Item>
        </Space>
        <Form.Item
          name="description"
          label="规则说明（条件）"
          extra="说明该规则的审核目的与判定条件"
        >
          <Input.TextArea rows={2} placeholder="说明该规则的审核目的与判定条件" />
        </Form.Item>
        <Form.Item name="enabled" valuePropName="checked" initialValue={true}>
          <Checkbox>启用该规则（取消勾选则审核时跳过本规则）</Checkbox>
        </Form.Item>
        <Form.Item
          name="checkpoints"
          label="审核要点"
          extra="每条要点将单独提交模型核查，建议拆到最小可判定粒度；每行一条"
          rules={[{ required: true, message: '请至少填写一个审核要点' }]}
        >
          <Input.TextArea
            rows={6}
            placeholder={'金额是否符合招标文件要求\n形式是否符合要求\n有效期是否满足'}
          />
        </Form.Item>
        <Form.Item name="need_legal_basis" valuePropName="checked" initialValue={false}>
          <Checkbox>
            该规则涉及法定标准，审核时优先检索法规知识库（需法规依据）
          </Checkbox>
        </Form.Item>
        <Form.Item
          name="doc_types"
          label="关联文档类型"
          extra="不选表示适用于全部文件；勾选后仅对匹配类型的文件执行本规则审核（其他文件不执行）"
        >
          <Select
            mode="multiple"
            allowClear
            placeholder="全部文件（不限定）"
            options={fileTypes.map((ft) => ({ value: ft.id, label: ft.name }))}
          />
        </Form.Item>
        <Form.Item
          name="section_ids"
          label="关联章节"
          extra="留空则对文档全文审核；指定章节后，仅对这些章节的正文执行本规则（其余章节跳过）。若文档未命中任何关联章节，本规则不产生结论。章节在「章节库配置」中维护。"
        >
          <Select
            mode="multiple"
            allowClear
            showSearch
            optionFilterProp="label"
            placeholder="不按章节裁剪（审核全文）"
            options={sectionOptions}
            notFoundContent="该文件类型下暂无章节，请在「章节库配置」中新增"
          />
        </Form.Item>

        {isConsistency && (
          <Form.Item label="跨文件一致性核心要素" style={{ marginTop: 8 }}>
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 12 }}
              message="配置本规则要跨文件比对的核心要素（如：报价金额、项目经理、项目编号）"
              description="规范名即跨文件对齐的 key；同义写法用于匹配不同文件中的不同表述。规则保存后，该要素配置会随规则集/规则组一起复用。"
            />
            <ConsistencyElementsEditor value={elements} library={library} onChange={setElements} />
          </Form.Item>
        )}
      </Form>
    </Modal>
  )
}
