# 确定性审核引擎改造 + 历史数据页重构 — 完成汇报

## 一、前端：历史审核数据页布局重构
- 对齐项目统一 `rm-*` 设计系统，重构 `HistoricalDataPage.tsx`
- 顶栏加「确定性引擎运行中」徽标；概览区改为信息分层卡片；列表加筛选条 + 行内采纳/不采纳徽标
- 详情抽屉分区卡片化 + 版本存证快照区；审计存证 Tab（版本化清单表）+ 一致性监控 Tab（真实漂移检测）
- 修复 JSX 语法错（`Text type="secondary>` 未闭合）

## 二、后端：确定性执行八大方向落地
1. **规则版本锁定**：`rule_content_fingerprint` 规则内容哈希，rule_set_version 现含内容哈希短码（规则变更即版本漂移可检测）
2. **规则结构化可执行化**：`rules_store` 规则加 `structured` 字段（禁止词/必含要素/金额阈值），`review_engine._apply_structured_rules` 确定性预检层锁定结论，LLM 不得推翻
3. **依赖数据版本化**：data_snapshot_version=KB_BASELINE_v2026.08.01 随存证
4. **文件输入唯一可追溯**：file MD5 + parsed_content_hash（解析快照哈希，含解析器版本）
5. **引擎版本锁定**：Dockerfile 注入 APP_VERSION=v1.0.0，engine_version() 读取
6. **代码执行确定性**：规则按 id 升序、批次排序、确定性模式串行（sem=1）、temperature=0、不依赖系统时间
7. **LLM 约束**：结构化结论锁定 + 输出归一化，LLM 仅辅助信息抽取/证据摘要
8. **结果结构化 + 存证重放**：version_manifest 完整存证，review_audit 表支持按历史版本重跑基线
9. **一致性监控**：consistency 接口改为真实规则漂移检测（rule_drifted / deterministic_ratio），不再占位

## 三、E2E 验证（真实容器）
- 单元测试：规则指纹稳定、结构化引擎同输入两次一致 ✅
- 真实任务 a8fe653e7987：completed，version_manifest 全字段填充，2 条确定性结论锁定，审计存证写入 ✅
- 二次审核 24cc1daf6458（同文件+同规则）：version_manifest 7 字段 100% 一致，确定性结论指纹 100% 一致 ✅
- 一致性监控：inconsistency_rate=0.0、rule_drifted=False、replay_ready=True ✅

## 四、修复的回归缺陷
1. `det_temperature` NameError（_summarize_doc 缺 temperature 参数）
2. version_manifest 全 None（done 分支未持久化）
3. `file_names` NameError（_drive_task 未定义，归档/存证被吞）
4. 前端 JSX 标签未闭合

## 五、交付文件
- 后端：`versioning.py` / `rules_store.py` / `review_engine.py` / `routers/review.py` / `routers/audit.py` / `Dockerfile`
- 前端：`pages/HistoricalDataPage.tsx`
- 报告：`deliverables/gstack/qa-deterministic-review-2026-08-24.md`
- 记忆：`.workbuddy/memory/2026-08-24.md`

## 结论
✅ 把审核系统变成稳定工业流水线：同样的输入、同样的规则、同样的引擎、同样的数据快照，确定性部分必然产生同样的输出。LLM 仅作辅助，关键合规结论由确定性结构化层锁定。
