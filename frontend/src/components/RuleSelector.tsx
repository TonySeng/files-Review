import { useMemo, useState } from 'react'
import {
  App as AntdApp,
  Button,
  Card,
  Checkbox,
  Empty,
  Input,
  Select,
  Space,
  Tag,
  Typography,
} from 'antd'
import { DownOutlined, RightOutlined, SettingOutlined } from '@ant-design/icons'
import type { Rule, RuleSet } from '../types'
import { categoryLabel, severityMeta } from './ruleMeta'

interface Props {
  rulesets: RuleSet[]
  activeRulesetId: string
  selectedRuleIds: string[]
  onRulesetChange: (id: string) => void
  onSelectedRulesChange: (ids: string[]) => void
  onOpenManagement: () => void
}

export default function RuleSelector({
  rulesets,
  activeRulesetId,
  selectedRuleIds,
  onRulesetChange,
  onSelectedRulesChange,
  onOpenManagement,
}: Props) {
  const { message } = AntdApp.useApp()
  const [query, setQuery] = useState('')

  const activeRuleset = useMemo(
    () => rulesets.find((r) => r.id === activeRulesetId),
    [rulesets, activeRulesetId],
  )
  const rules = activeRuleset?.rules ?? []

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return rules
    return rules.filter(
      (r) =>
        r.name.toLowerCase().includes(q) ||
        r.description.toLowerCase().includes(q),
    )
  }, [rules, query])

  const grouped = useMemo(() => {
    const map = new Map<string, Rule[]>()
    filtered.forEach((rule) => {
      const list = map.get(rule.category) ?? []
      list.push(rule)
      map.set(rule.category, list)
    })
    return [...map.entries()]
  }, [filtered])

  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set())

  // 默认折叠所有分类；搜索时自动展开以展示匹配项
  const isGroupExpanded = (cat: string) => (query.trim() ? true : expandedGroups.has(cat))
  const toggleGroupExpand = (cat: string) => {
    setExpandedGroups((prev) => {
      const next = new Set(prev)
      if (next.has(cat)) next.delete(cat)
      else next.add(cat)
      return next
    })
  }
  const allExpanded = grouped.length > 0 && grouped.every(([c]) => isGroupExpanded(c))
  const toggleAllExpand = () =>
    setExpandedGroups(allExpanded ? new Set() : new Set(grouped.map(([c]) => c)))

  const toggleRule = (id: string) => {
    onSelectedRulesChange(
      selectedRuleIds.includes(id)
        ? selectedRuleIds.filter((x) => x !== id)
        : [...selectedRuleIds, id],
    )
  }

  const toggleGroup = (list: Rule[], selectAll: boolean) => {
    const ids = list.map((r) => r.id)
    if (selectAll) {
      onSelectedRulesChange([...new Set([...selectedRuleIds, ...ids])])
    } else {
      const set = new Set(ids)
      onSelectedRulesChange(selectedRuleIds.filter((id) => !set.has(id)))
    }
  }

  const toggleAll = (selectAll: boolean) => {
    onSelectedRulesChange(selectAll ? rules.map((r) => r.id) : [])
  }

  const selectedCount = selectedRuleIds.filter((id) =>
    rules.some((r) => r.id === id),
  ).length

  return (
    <Card size="small" title="审核规则集">
      <div className="rule-summary-bar">
        <Space size={8} wrap>
          <span className="rule-count-chip">共 {rules.length} 条规则</span>
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            已选 {selectedCount} 条
          </Typography.Text>
        </Space>
        <Button
          size="small"
          type="link"
          icon={<SettingOutlined />}
          style={{ paddingInline: 0 }}
          onClick={onOpenManagement}
        >
          规则管理
        </Button>
      </div>

      {rulesets.length > 0 && (
        <Select
          size="small"
          style={{ width: '100%', marginTop: 10 }}
          value={activeRulesetId}
          onChange={onRulesetChange}
          options={rulesets.map((rs) => ({
            value: rs.id,
            label: rs.builtin ? `${rs.name}（内置）` : `★ ${rs.name}`,
          }))}
        />
      )}

      <Input.Search
        allowClear
        placeholder="搜索规则名称或说明"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        style={{ margin: '12px 0 4px' }}
      />

      <div className="rule-selector-actions">
        <Button
          size="small"
          onClick={() => {
            toggleAll(true)
            message.success('已全选当前规则集')
          }}
        >
          全选
        </Button>
        <Button size="small" onClick={() => toggleAll(false)}>
          清空
        </Button>
        <Typography.Link
          style={{ fontSize: 12, marginLeft: 'auto' }}
          onClick={toggleAllExpand}
        >
          {allExpanded ? '收起全部' : '展开全部'}
        </Typography.Link>
      </div>

      <div className="rule-selector-list">
        {grouped.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={query ? '未找到匹配的规则' : '该规则集暂无规则'}
            style={{ marginTop: 24 }}
          />
        ) : (
          grouped.map(([category, list]) => {
            const selectedInGroup = list.filter((r) =>
              selectedRuleIds.includes(r.id),
            ).length
            const allSelected = selectedInGroup === list.length
            const open = isGroupExpanded(category)
            return (
              <div key={category} className="rule-drawer-group">
                <div
                  className="rule-group-head rule-group-head--toggle"
                  onClick={() => toggleGroupExpand(category)}
                >
                  <span className="rule-group-title">
                    <span className="rule-group-caret">
                      {open ? <DownOutlined /> : <RightOutlined />}
                    </span>
                    {categoryLabel(category)}
                    <span className="upload-hint">
                      {' '}
                      · 已选 {selectedInGroup}/{list.length}
                    </span>
                  </span>
                  <Typography.Link
                    style={{ fontSize: 12 }}
                    onClick={(e) => {
                      e.stopPropagation()
                      toggleGroup(list, !allSelected)
                    }}
                  >
                    {allSelected ? '取消本组' : '全选本组'}
                  </Typography.Link>
                </div>

                {open &&
                  list.map((rule) => {
                    const selected = selectedRuleIds.includes(rule.id)
                    const meta = severityMeta(rule.severity)
                    return (
                      <div
                        key={rule.id}
                        className={`rule-drawer-item ${selected ? 'is-selected' : ''}`}
                        role="button"
                        tabIndex={0}
                        onClick={() => toggleRule(rule.id)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault()
                            toggleRule(rule.id)
                          }
                        }}
                      >
                        <Checkbox
                          checked={selected}
                          onClick={(e) => e.stopPropagation()}
                          onChange={() => toggleRule(rule.id)}
                        />
                        <div className="rule-drawer-item-main">
                          <div className="rule-drawer-item-head">
                            <span className="rule-drawer-item-name">{rule.name}</span>
                            <Space size={4} wrap>
                              <Tag color={meta.color} style={{ marginInlineEnd: 0 }}>
                                {meta.label}
                              </Tag>
                              {rule.need_legal_basis && (
                                <Tag color="blue" style={{ marginInlineEnd: 0 }}>
                                  需法规依据
                                </Tag>
                              )}
                            </Space>
                          </div>
                          {rule.description && (
                            <div className="rule-drawer-item-desc">{rule.description}</div>
                          )}
                        </div>
                      </div>
                    )
                  })}
              </div>
            )
          })
        )}
      </div>
    </Card>
  )
}
