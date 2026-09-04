import type { Severity } from '../types'

export const CATEGORY_LABEL: Record<string, string> = {
  qualification: '资格性',
  commercial: '商务',
  technical: '技术',
  format: '格式',
  consistency: '一致性',
  legal: '法规',
  tender_quality: '招标文件质量',
  general_quality: '通用质量',
  general_text: '通用文字',
  proper_noun: '专有名词',
  punct_num: '标点数字',
  word_usage: '语句用词',
  format_spec: '格式规范',
}

export const SEVERITY_META: Record<
  Severity,
  { label: string; color: string; tone: string }
> = {
  critical: { label: '否决项', color: 'red', tone: '#f5222d' },
  major: { label: '重要', color: 'orange', tone: '#fa8c16' },
  minor: { label: '一般', color: 'blue', tone: '#1677ff' },
  info: { label: '提示', color: 'default', tone: '#8c8c8c' },
}

export function categoryLabel(category: string): string {
  return CATEGORY_LABEL[category] || category
}

export function severityMeta(severity: Severity) {
  return SEVERITY_META[severity] || { label: severity, color: 'default' }
}
