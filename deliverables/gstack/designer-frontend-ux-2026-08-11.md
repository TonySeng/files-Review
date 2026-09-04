# 前端交互设计与体验优化说明

**日期**：2026-08-11
**范围**：`frontend/src`（React 18 + TS + Vite + Ant Design 5）
**执行方式**：软件工坊主理人直调（团队调度不可用，降级为单 Agent 模式）
**构建结果**：`npm run build` 通过（✓ 3025 modules transformed，产物 `dist/` 已生成）

---

## 1. 现状评估（改造前）

原前端整体质量较高：暗色日志面板、结论状态色（通过/不合规/存疑/待确认）、`Empty` 空态、`Table` loading、审核中按钮 `loading` 等均已具备。仍存在的体验短板：

| 维度 | 问题 |
|---|---|
| 布局 | 左/右栏固定 `Col span={9}/{15}`，窄屏不堆叠，小屏挤压严重 |
| 交互逻辑 | 文件删除无二次确认，易误删；首屏数据异步加载期间界面空白、可误触 |
| 视觉反馈 | 顶栏无后端连通状态提示；审核流建立 SSE 连接、解析文件的等待期无任何占位 |

---

## 2. 优化项与改动清单

### 2.1 页面布局（响应式）
- **文件**：`App.tsx`
- 左栏 `Col` 由 `span={9}` 改为 `xs={24} sm={24} md={24} lg={9}`；右栏由 `span={15}` 改为 `xs={24} sm={24} md={24} lg={15}`。
- `Row` gutter 改为 `[16,16]`，窄屏自动上下堆叠，大屏保持双栏。
- `index.css` 增加 `@media (max-width:768px)`：缩小 header/body 内边距，移动端更紧凑。

### 2.2 组件交互逻辑
- **文件**：`App.tsx` + `services/api.ts`
  - 新增 `api.health()` 调用 `/api/health`，首屏并行拉取「规则 / 配置 / 文件列表 / 健康」四项，用 `Promise.allSettled` 容错。
  - 新增 `booting` 状态：未加载完前渲染居中 `Spin`（tip「正在加载工作台…」），避免空白与误触。
- **文件**：`components/FilePanel.tsx`
  - 删除按钮外包 `Popconfirm`（标题「确认删除该文件？」+ 描述 + 危险确认按钮），且 `disabled` 时 `Popconfirm` 同步禁用；审核进行中无法删除。

### 2.3 视觉反馈
- **文件**：`App.tsx`
  - 顶栏新增后端连通状态标签：绿色「后端已连接」/ 红色「后端未连接」（带 Tooltip 说明）/ 蓝色「连接中…」，让用户第一时间感知服务可用性。
- **文件**：`components/ProgressPanel.tsx`
  - 审核流已 `running` 但 `logs` 为空时（SSE 连接与文件解析阶段），渲染带 `Spin` 的「正在连接审核服务并解析文件…」占位卡，消除等待焦虑。
- **文件**：`components/FilePanel.tsx`
  - `Table` 增加 `locale.emptyText`：「暂无文件，请上传招标文件、投标文件或附件」，空态引导更明确。

---

## 3. 关键交互改进点（摘要）

1. **状态可见性**：顶栏后端连通灯 + 首屏加载态 + 审核连接占位，三层加载/连通反馈闭环。
2. **防误操作**：文件删除二次确认；审核进行中危险操作（上传/删除/开始）统一禁用。
3. **响应式**：双栏 → 单栏自适应，桌面与笔记本/平板均可用。
4. **空态引导**：文件列表与结论区均有明确引导文案。

---

## 4. 构建验证

```
> tsc -b && vite build
✓ 3025 modules transformed.
dist/index.html                  0.40 kB │ gzip: 0.31 kB
dist/assets/index-*.css         1.40 kB │ gzip: 0.71 kB
dist/assets/index-*.js      1,183.34 kB │ gzip: 372.83 kB
✓ built in 4.84s
```

> 注：JS 包体 >500kB 仅为 antd 整体打包的常规提示，非错误。后续如需优化首屏，可对 antd 做 `manualChunks` 分包或路由级 `import()` 懒加载（不在本次范围内）。

---

## 5. 修改文件清单

- `frontend/src/App.tsx`
- `frontend/src/components/FilePanel.tsx`
- `frontend/src/components/ProgressPanel.tsx`
- `frontend/src/services/api.ts`
- `frontend/src/index.css`

未改动后端代码、未引入任何新 npm 依赖，保持中文 UI 文案。
