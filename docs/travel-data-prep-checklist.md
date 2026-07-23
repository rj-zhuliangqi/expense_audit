# 交通费工作流 - 数据准备清单

> 对应工作流文件：`graph-latest-travel-0722.json`（25 节点 / 41 边）
> 规则来源：`交通费/交通费-AI-20260721 - Sheet1.csv`（14 条规则）

---

## 一、需提前准备的数据（4 项）

### 1. 票种清单（expenseInvoiceTypes）— E35 票种检查

| 项目 | 说明 |
|---|---|
| **用途** | E35 票据类型检查，图表达式 `invoiceType in map(serviceData.expenseInvoiceTypes as c, c.manufacturerBillCode)` |
| **来源** | `audit_client.fetch_expense_invoice_types(eiCode)` → 审计服务接口 |
| **需准备** | 确保交通费费用项的 `eiCode` 对应的票种清单包含以下票种 |
| **字段** | `manufacturerBillCode`（票种名称） |

**交通费允许票种清单：**

```
数电普通发票、电子普通发票、过路过桥费发票、火车票、客运车票、
的士票/出租车票、财政收据、数电铁路
```

---

### 2. 旅客姓名字段（passengerName）— E15 本人姓名检查 / W29 出行人信息检查

| 项目 | 说明 |
|---|---|
| **用途** | E15 校验旅客姓名==核销人姓名；W29 校验数电普票是否开具出行人信息 |
| **来源** | OCR 识别结果 |
| **字段名** | `passengerName`（**需与 OCR 团队确认实际输出字段名**） |
| **比对对象** | `serviceData.auditInfo.verifiUserName`（核销人姓名） |
| **适用票种** | 火车票、客运汽车票、数电铁路、数电普通发票 |

**⚠️ 待确认事项：**
- OCR 是否能识别上述票种的旅客姓名字段
- 字段名是否为 `passengerName`，还是在 `invoiceData` 顶层或 `items` 内
- 数电普通发票的出行人姓名+有效身份证件号是否都能识别

---

### 3. 出行人有效身份证件号 — W29 出行人信息检查（相关）

| 项目 | 说明 |
|---|---|
| **用途** | W29 数电普通发票出行人信息检查（WARNING 级别） |
| **来源** | OCR 识别 |
| **字段名** | 待确认（可能为 `passengerIdNo` 或在 `remark` 中） |
| **说明** | 当前 W29 仅校验 `passengerName` 是否非空，证件号字段为后续增强项 |

---

### 4. 交通费发票内容允许关键词（已硬编码，无需额外准备）

| 项目 | 说明 |
|---|---|
| **用途** | E36 发票内容项目检查 |
| **实现方式** | 硬编码在图 expressionNode 中，表达式 `some(["代驾","停车",...], contains(contents, #))` |
| **关键词清单** | 代驾、停车、电费、供电、充电、客运、车位管理费、通行费、代订车、信息系统增值服务、车辆停放、运输服务、停车占道费 |
| **后续** | 规则 CSV 提到"后续 AI 在规则学习中不断更新完善"，可考虑做成 `serviceData` enricher 动态维护 |

---

## 二、复用现有数据（无需额外准备，5 项）

以下数据已在通讯费工作流中使用，交通费工作流直接复用：

| 数据项 | 字段路径 | 用途 |
|---|---|---|
| 公司清单 | `serviceData.companyList` | E02 抬头检查 / 税号检查 |
| 公司黑名单 | `serviceData.companyBlacklist` | E09 销方黑名单检查 |
| 发票使用历史 | `serviceData.invoiceUsageHistory` | E05 发票重复使用检查 |
| 核销单信息 | `serviceData.auditInfo` | 各项检查（含 `verifiUserName`/`verifiUserPhone`/`submitTime`/`applyAmount`/`instanceComCode`） |
| 发票验真结果 | `verifyResult` | sys-001 真伪 / sys-003 作废 / sys-004 红冲检查 |

---

## 三、新增节点与数据依赖关系

| 节点 | 规则代码 | 控制级别 | 输入字段 | 数据依赖 |
|---|---|---|---|---|
| 本人姓名检查 | E15 | 阻断(REJECT) | `isPassengerNameMatch` | `passengerName` + `verifiUserName` |
| 出行人信息检查 | W29 | 标记(WARNING) | `hasPassengerInfo` | `passengerName` + `invoiceType` |
| 出租车连号检查 | W19 | 阻断(REJECT) | `isTaxiConsecutiveMock` | **占位 mock**，后期代码实现跨发票状态 |
| 发票内容项目检查 | E36 | 阻断(REJECT) | `isTravelContent` | `contents`（硬编码关键词） |

---

## 四、待办与后续工作

1. **OCR 字段对齐**：确认 `passengerName` 字段名及适用票种，与 OCR 团队对齐
2. **票种清单配置**：在审计服务中为交通费费用项配置上述 8 种票种
3. **出租车连号检查实现**：W19 当前为 mock 占位（固定通过），后期在 `expense_audit_orchestrator` 中维护跨发票状态实现（参考金额充足检查的实现模式）
4. **内容关键词动态化**：当前硬编码在图中，后续可改为 `serviceData` enricher（类似 `telecom_list`）
5. **travel profile 注册**：本次仅改图 JSON，`profiles/travel` 代码注册留待后续

---

## 五、验证结果

交通费工作流已通过 4 个场景测试：

| 场景 | 输入 | 预期结果 | 实际结果 |
|---|---|---|---|
| 火车票-姓名一致 | passengerName=刘雪涛, verifiUserName=刘雪涛 | 全部 PASS | ✅ 全部 PASS |
| 火车票-姓名不一致 | passengerName=李四, verifiUserName=刘雪涛 | E15 REJECT | ✅ E15 REJECT |
| 数电普票-无出行人信息 | invoiceType=数电普通发票, passengerName="" | W29 WARNING | ✅ W29 WARNING |
| 非交通费内容 | contents=餐饮服务 | E36 REJECT | ✅ E36 REJECT |
