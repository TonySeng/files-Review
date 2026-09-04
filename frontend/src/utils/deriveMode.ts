import type { ReviewMode, RuleGroup, RuleSet } from '../types'

/**
 * 审核模式推导：用户不再手动选择「投标/招标/通用」，而是由所选规则集或规则组自动判定。
 *
 * 依据：
 * - 规则 id 前缀：`tender-*` → 招标文档规则；`gen-*` → 通用规则；
 *   其余（qual-/biz-/tech-/fmt-/cons- 等）→ 投标文档规则。
 * - 按规则集：若选中的是内置 `mode-*` 合成集，直接取前缀；若为自定义规则集，按所含规则推导。
 * - 按规则组：展开所选规则组包含的全部规则 id 后推导。
 *
 * 混合情形（同时含投标与招标规则）以「投标优先」判定——投标审核天然包含对招标文件的
 * 对应性核查，语义上更贴近最具体的场景。
 */

function classifyRule(ruleId: string): ReviewMode | null {
  if (ruleId.startsWith('tender-')) return 'tender'
  if (ruleId.startsWith('gen-')) return 'general'
  // 其余均属投标文档规则
  return 'bid'
}

export function deriveModeFromRuleIds(ruleIds: string[]): ReviewMode {
  let hasBid = false
  let hasTender = false
  for (const id of ruleIds) {
    const m = classifyRule(id)
    if (m === 'bid') hasBid = true
    else if (m === 'tender') hasTender = true
  }
  if (hasBid) return 'bid'
  if (hasTender) return 'tender'
  return 'general'
}

export function deriveReviewMode(params: {
  configMode: 'group' | 'ruleset' | 'legal'
  activeRulesetId: string
  selectedGroupIds: string[]
  rulesets: RuleSet[]
  ruleGroups: RuleGroup[]
}): ReviewMode {
  const { configMode, activeRulesetId, selectedGroupIds, rulesets, ruleGroups } = params

  // 按法规文件：没有可推导的内置规则，默认按投标文档审核（与一致性核查策略一致）。
  if (configMode === 'legal') return 'bid'

  if (configMode === 'ruleset') {
    if (activeRulesetId.startsWith('mode-')) {
      return activeRulesetId.slice(5) as ReviewMode
    }
    const rs = rulesets.find((r) => r.id === activeRulesetId)
    if (rs) return deriveModeFromRuleIds(rs.rules.map((r) => r.id))
    return 'general'
  }

  // 按规则组：展开所选组的规则 id 推导
  const ids: string[] = []
  for (const g of ruleGroups) {
    if (selectedGroupIds.includes(g.id)) ids.push(...(g.rule_ids || []))
  }
  return deriveModeFromRuleIds(ids)
}
