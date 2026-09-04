# 审核逻辑确定性执行 · 架构方案评审

**日期**：2026-08-24
**场景**：产品评审 + 排障（确定性执行架构）
**参与成员**：产品评审员（架构）、排障手（实现）

---

## 📌 TL;DR
- 整体结论：🟢 通过（后端已具备 80% 确定性骨架，补齐"规则版本锁定 + 结构化规则引擎 + 真实重跑对比"三块即可达成工业流水线级一致）
- 现状：versioning/engine/review/audit 已落地"版本存证 + 确定性模式(温度0/排序/串行/归一化)"，但存在三处缺口：
  1. 规则版本是"基线日期"而非"内容锁定"——规则被编辑后 rule_set_version 不变，无法检测漂移；
  2. 规则仍以自然语言 checkpoints 为主，无结构化可执行条件，关键结论仍依赖 LLM 开放判断；
  3. 一致性监控 diff_rate 为占位 0，未真实重跑比对。

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| Go / No-Go | 🟢 Go |
| 严重度分布 | 🟢 架构改进 3 / 🟡 一致性风险 1 |
| 关键行动项 | 5 条 |
| 建议负责人 | 后端实现 |

---

## 1. 产品评审员核心结论

### 1.1 规则版本锁定（需求 1.1 / 1.3）
**现状**：`rule_set_version_for` 仅用 `_RULESET_BASELINE_DATE` 拼接，规则内容变更不产生新版本。
**方案**：
- 新增 `rule_content_fingerprint(rules)`：对规则组按 id 排序后取其 `name|description|checkpoints|structured` 的稳定 JSON 做 SHA-256 短码。
- `resolve_rule_set_version` 输出格式升级为 `RULE_SET_<mode>_v<date>_<content8>` 或 `RULE_GROUP_v<fid>_<date>_<content8>`，使**内容变更即版本漂移可检测**。
- `build_version_manifest` 额外落 `rule_content_hash` 字段，供一致性监控精确比对。

### 1.2 规则结构化可执行化（需求 1.2）
**现状**：规则是 `{id,name,category,severity,description,checkpoints,need_legal_basis}`，checkpoints 是自然语言。
**方案**：规则对象增加可选 `structured` 字段，支持四类可计算条件：
```json
"structured": {
  "forbid_keywords": ["黑名单词A", "黑名单词B"],
  "require_elements": ["项目名称", "项目编号", "交货期"],
  "regex_patterns": [{"pattern": "金额[:：]\\s*([0-9]+)", "must_match": true}],
  "amount_thresholds": [{"field": "投标报价", "max": 1000000}]
}
```
新增 `_apply_structured_rules(rules, docs_text)` 确定性预检层：
- 命中 `forbid_keywords` → 直接产 fail finding（status=fail, severity=规则severity），证据=命中关键词+位置；
- 缺 `require_elements` → fail，证据=缺失要素；
- 正则/金额不满足 → fail；
- **确定性结论先行锁定**，LLM 仅对未结构化覆盖的规则做辅助判断，且最终 fail/pass 以确定性层为准，LLM 不可推翻确定性 fail。
此即需求 3.3「关键合规结论由确定性规则引擎给出」。

### 1.3 依赖数据版本化（需求 1.3）
KB 知识库作为外部依赖，已以 `data_snapshot_version` 基线标识；建议知识库侧提供发布版本号经 `BCR_DATA_SNAPSHOT_VERSION` 注入（本期仅加固读取链路，已在 versioning 实现）。

---

## 2. 排障手实现要点（确定性执行，需求 3）

### 3.1 固定引擎版本
`engine_version()` 优先环境变量 `BCR_ENGINE_VERSION` / `APP_VERSION`，已在 versioning 实现；Dockerfile 注入镜像标签即可（见运维建议）。

### 3.2 代码执行确定性
- 规则按 id 升序（`ordered_rules`）✅ 已实现；
- 批次排序 ✅；
- 确定性模式 `sem=1` 串行 ✅；
- **不使用系统时间做判断** ✅（`_now_shanghai` 仅用于存证时间戳）；
- 增加 `_structured_normalize`：status 强归一 `{pass,fail,warn,unknown}`、severity 强归一、去空白，消除 LLM 输出波动。

### 3.3 LLM 结构化约束（需求 3.3）
- `build_rule_prompt` 强化 JSON Schema 约束（已部分实现），明确 `status` 枚举；
- `_normalize_findings` 已做基本归一；新增确定性层后，LLM 输出仅作"证据摘要"用途，不决定 fail/pass。

### 2.3 解析快照（需求 2.2）
`run_review` 已对每文档计算 `parsed_hash` 并 `doc_group_parsed_hash` 随存证 ✅；补充：在 `save_audit` 存证时 `parsed_content_hash` 已落库 ✅。

---

## 3. 一致性监控真实重跑（需求 8，缺口修复）

**现状**：`audit.consistency_monitor` 的 `diff_rate` 恒为 0（占位）。
**方案**：对同一文件+规则组合，**比对两次归档的结论指纹是否一致**：
- 利用 `reviewdata_store` 的 `finding_fingerprint(rule_id, content_key)`——同一文件同规则两次审核，若 findings 内容一致则 fingerprint 一致；
- 一致性监控改为：取最近 n 条 `review_records`，对每条查其历史 decisions 的 `finding_fp` 集合，与最新一次归档的 decisions `finding_fp` 集合比对，diff_rate = 不一致 finding 数 / 总 finding 数；
- 若某条 record 只有一次归档则 diff_rate=0（无可比对象），标记为"样本不足"。
此实现零额外 LLM 成本，可常态化运行，契合需求 8「持续监控一致性」。

---

## ✅ 行动清单

| # | 行动 | 负责方 | 紧急度 |
|---|------|--------|--------|
| 1 | versioning 增加 rule_content_fingerprint + rule_set_version 升级含内容哈希 | 后端 | P0 |
| 2 | rules_store 规则增加 structured 字段 + _normalize_rule 支持 | 后端 | P0 |
| 3 | review_engine 增加 _apply_structured_rules 确定性预检层 | 后端 | P0 |
| 4 | 一致性监控改为真实 fingerprint 比对（audit.py） | 后端 | P1 |
| 5 | Dockerfile 注入 APP_VERSION/BUILD_TIME；回归测试验证 | 运维+QA | P1 |

---

## ⚠️ 已知局限
- 结构化规则需由业务侧逐步为关键规则补充 `structured` 条件（先覆盖否决项如资质/黑名单），非一次性全量。
- 真实"后台重跑任务"对比依赖任务重建，成本高；本期以 fingerprint 比对达成可监控的不一致率指标。

---

> 本报告由软件工坊 AI 协作生成，关键决策请由工程负责人复核。
