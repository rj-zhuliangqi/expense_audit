# opt_plan.md 架构评审 + 修订设计

## 总评

方案核心判断成立：经代码核实，通讯费耦合确实小且叠加式——`core.py:363` 硬编码 `telecom_list`、`writeback.py:275-281` 的 `*电信服务*` 合规判断、`writeback.py:26-27` 两个空数组、`writeback_client.py:17` 的 save path 常量，共 4 处。"单一共享管线 + 注入插槽 + Profile"方向正确，Out-of-scope（不捆绑包重命名、不引入类继承、travel 只放骨架）克制得当。

但结合用户三点反馈，方案需从"插槽化"升级为"**底座最小通用 + 两级 enricher + 回写策略包**"：

1. telecom_list 改为启动时加载的离线资产（可配置路径）。
2. 新费用类型的数据准备差异，通过收据级 enricher 注入（离线资产 / 在线 fetch 统一为 enricher 两种实现）。
3. 回写组装差异通过策略包注入，且**注入点必须在 payload 组装层而非 client 层**（修正原方案结构性错误）。

---

## 修订设计

### A. 离线资产：启动加载 + 可配置路径（用户点 1）

现状：`operator_city.csv` 已是数据文件，但 `core.py:363` 每单据重读、路径硬编码包根（`core.py:17`）。

设计：
- CSV 留作资产，路径解析走 **env + CLI 双覆盖**（CLI > env > 默认包内路径），与现有 `GRAPH_RUNTIME_URL` + `--graph-runtime-url`（bootstrap.py:30）同套路。新增 `TELECOM_OPERATOR_CITY_CSV` env + `--telecom-asset-dir` CLI。
- `load_telecom_list` 迁到 `profiles/telecom/data.py`。
- **bootstrap 构造 telecom profile 时加载一次、缓存内存**，enricher 闭包引用缓存返回 `{"telecom_list": cached}`。改数据 = 改 CSV + 重启 worker，零代码改动。
- 此"启动加载离线资产"作为 profile 通用能力：travel 里程标准表、差旅政策表同理，各自 profile 启动时加载。

### B. 数据准备：底座最小通用 + 两级 enricher（用户点 2）

判断：现有 8 个 `fetch_*` 大多真通用（`audit_info`/`company_list`/`company_blacklist`/`audit_invoice_files`/`field_mappings` 每个费用类型都要）。费用类型差异是叠加式的：telecom 要 `telecom_list`，travel 要行程 + 里程标准。所谓"缺一部分"= 用 enricher 补费用类型专属数据。

**WHERE fetch**（按通用性分两类）：
- 通用审计服务接口（所有费用类型可调）→ 新增 `fetch_*` 放 `audit_client.py`，与现有 9 个并列。
- 费用类型专属数据源 → 放 `profiles/<type>/data.py`，分两种实现：离线资产（启动加载缓存）/ 在线接口（调差旅服务等）。

**HOW 注入**：底座 `ReceiptDataPreparer` 加**收据级**插槽 `receipt_enrichers`（区别于现有发票级 `extra_enrichers`，core.py:445）。在 `prepare_receipt_context` 构建完通用 `service_data` 后遍历合并。enricher 签名：

```
ReceiptEnricher = Callable[[str, Mapping], dict[str, Any]]
# (receipt_code, service_data_so_far) -> 合并进 serviceData 的键值
```

传 `service_data_so_far` 是关键：travel enricher 能从 `service_data["auditInfo"]["instanceCode"]` 取 instance 查行程，返回 `{"travelItinerary": [...], "mileageStandard": [...]}`；telecom enricher 闭包引用点 A 缓存返回 `{"telecom_list": cached}`。**点 A 与点 B 统一**：离线资产和在线 fetch 都是 enricher 的两种实现。

**原则**：底座只保留"所有费用类型都要"的 fetch；任何"只有某类型要"的数据（在线或离线）都走 enricher。若发现某底座 fetch 不通用（如 travel 不需 `expense_invoice_types`），**从底座降级成 enricher**、由需要的 profile 提供。保证底座始终最小通用、差异永远在 profile。

### C. 回写组装：策略包注入 payload 组装层（用户点 3，修正原方案）

`assemble_result_audit_info`(writeback.py:8) 现硬编码 10 字段。travel 差异：`auditTravels` 不再空 `[]`（填行程明细，数据来自点 B enricher 注入的 serviceData）、`compliance` 规则不同（无 `*电信服务*` 判断）、`formInvoiceTaxViews` 可能填。

设计：
- `assemble_result_audit_info` 保留为**通用字段组装器**（~8 个所有类型都回写的字段），加可选参数接收 profile **回写策略包**：`compliance_rule` / `audit_travels_builder` / `form_invoice_tax_views_builder`。
- builder 签名 `(invoice_pairs, service_data) -> list/dict`——从 serviceData 取点 B enricher 注入的行程数据填 `auditTravels`。数据流闭环：**enricher 注入 serviceData → writeback builder 从 serviceData 取出填回写**。
- **关键修正**：策略注入在 **payload 组装层**，不在 client 层。`assemble_result_audit_info` 实际有三个调用点（核实：`writeback_client.py:76` 经 `_build_writeback_payload_from_result` 服务 HTTP sink 和文件 sink 两条路径、`prepare_from_queue.py:114` 直接调用），而 `AuditInfoWritebackClient`(writeback_client.py:21) 只管 HTTP 传输、不碰组装。若只注入 client，文件 sink 和 prepare_from_queue 两路径会**静默回退默认合规**，通讯费 `*电信服务*违约金` 判断丢失，违反"行为不变"。
- 落地：新增单一入口 `build_writeback_payload(receipt_result, *, 策略包)`，三个调用点全走它；`AuditInfoWritebackClient` 只加 `save_path`、保持纯传输。

---

## 目标架构（修订）

```
expense_audit_orchestrator/
  core.py                 # 底座 ReceiptDataPreparer：去 telecom 硬编码，加 receipt_enrichers 插槽（最小通用 fetch）
  application.py          # ReceiptAuditService 编排（不动）
  audit_client.py         # 通用 fetch_*（不动；新费用类型的通用接口在此新增）
  kingdee_ocr.py / runtime_client.py  # 通用（不动）
  writeback.py            # assemble_result_audit_info 加可选策略包参数；通用字段组装
  writeback_client.py     # 新增 build_writeback_payload(receipt_result, *, 策略包) 单一入口；client 加 save_path
  bootstrap.py            # create_receipt_audit_service(profile=...)：启动加载离线资产、装配 enricher + 策略包
  assets/                 # 离线资产目录（operator_city.csv 迁入；路径可 env/CLI 覆盖）
  profiles/
    __init__.py           # ExpenseProfile dataclass + get_profile(name) 注册表
    telecom/
      data.py             # load_telecom_list + telecom_receipt_enricher（闭包缓存）
      writeback.py        # telecom_compliance_rule + audit_travels_builder=None + save_path
    travel/               # 骨架（接入时填）
      data.py             # travel_receipt_enricher（行程 fetch + 里程标准离线资产）
      writeback.py        # travel_compliance_rule + travel_audit_travels_builder
```

ExpenseProfile 契约（修订，含离线资产 + 两级 enricher + 回写策略包）：

```python
@dataclass
class ExpenseProfile:
    name: str
    default_graph_path: Path | str | None = None
    receipt_enrichers: Mapping[str, ReceiptEnricher] = field(default_factory=dict)   # 收据级（含离线资产/在线 fetch）
    invoice_enrichers: Mapping[str, DataEnricher] = field(default_factory=dict)      # 发票级（透传 extra_enrichers）
    compliance_rule: ComplianceRule = _default_compliance
    audit_travels_builder: AuditTravelsBuilder | None = None
    form_invoice_tax_views_builder: FormBuilder | None = None
    writeback_save_path: str = AUDIT_INFO_SAVE_PATH
    # 离线资产在 profile 构造时加载缓存，enricher 闭包引用；不在此字段暴露
```

---

## 改动清单（修订）

### 1. 通用底座插槽化（core.py）
- 新增字段 `receipt_enrichers: Mapping[str, ReceiptEnricher] = field(default_factory=dict)`。
- `prepare_receipt_context`(~L335)：删 L363 硬编码 `"telecom_list": self.telecom_list_provider()`；构建完基础 `service_data` 后遍历 `receipt_enrichers` 合并（enricher 收 `service_data_so_far`，可读已 fetch 的 `auditInfo` 等）。
- 删字段 `telecom_list_provider` + 默认 `load_telecom_list` + `DEFAULT_OPERATOR_CITY_CSV_PATH`，迁到 `profiles/telecom/data.py`。底座不认识"通讯费"。
- 发票级 `extra_enrichers`(L445) 保留，profile 的 `invoice_enrichers` 透传给它。建议改名 `invoice_enrichers` 对齐命名（或加 alias）。

### 2. 回写层（writeback.py + writeback_client.py）
- `assemble_result_audit_info`：签名加可选 `compliance_rule`/`audit_travels_builder`/`form_invoice_tax_views_builder`，均有默认值。L275-281 `*电信服务*` 判断 → `compliance_rule(goods_name, item)`（逻辑迁 `profiles/telecom/writeback.py`，逐字保持）。L26-27 空 `[]` → `builder(invoice_pairs, service_data) if builder else []`。
- **新增** `build_writeback_payload(receipt_result, *, compliance_rule=None, audit_travels_builder=None, form_builder=None)` 单一入口，内部调 `assemble_result_audit_info` 透传策略。放 `writeback_client.py` 或 `writeback.py`。
- **三个调用点全改走新入口**：`build_receipt_writeback_sink`(writeback_client.py:54)、`build_receipt_writeback_file_sink`(:62)、`prepare_from_queue.export_writeback_payload`(prepare_from_queue.py:114)。策略由 `create_receipt_audit_service` 从 profile 取出注入。
- `AuditInfoWritebackClient.__init__` 加 `save_path: str = AUDIT_INFO_SAVE_PATH`，`save_result_audit_info` 用 `self._save_path` 替代模块常量(L32)。client 保持纯传输，不碰组装。

### 3. bootstrap 按 profile 装配（bootstrap.py）
- `create_receipt_audit_service` 加 `profile: ExpenseProfile | str = "telecom"`，字符串走 `get_profile`。
- **签名默认 `graph_path: Path | str | None = None`**（原 `DEFAULT_GRAPH_PATH`，否则 profile 的 `default_graph_path` 永不生效），函数体三级回落 `graph_path or profile.default_graph_path or DEFAULT_GRAPH_PATH`。
- 启动加载离线资产（telecom 的 `operator_city.csv`，路径走 env/CLI 覆盖），缓存喂给 enricher。
- `ReceiptDataPreparer` 传 `receipt_enrichers=profile.receipt_enrichers`、`extra_enrichers=profile.invoice_enrichers`。
- 回写策略包从 profile 取出，注入三个 sink 调用点 + `prepare_from_queue` 路径。`AuditInfoWritebackClient` 传 `save_path=profile.writeback_save_path`。

### 4. telecom profile（行为不变）
- `profiles/telecom/data.py`：搬入 `load_telecom_list` + `DEFAULT_OPERATOR_CITY_CSV_PATH`；`telecom_receipt_enricher(receipt_code, service_data) -> {"telecom_list": cached}`（闭包引用启动加载的缓存）。
- `profiles/telecom/writeback.py`：`telecom_compliance_rule(goods_name, item)` 复刻 L275-281；`audit_travels_builder/form_builder=None`；`writeback_save_path=现有 AUDIT_INFO_SAVE_PATH`。
- `profiles/__init__.py`：组装 `TELECOM = ExpenseProfile(...)`；`get_profile` 注册表。

### 5. travel profile 骨架（deferred）
- `profiles/travel/` 空壳：`data.py` 占位 `travel_receipt_enricher`（行程在线 fetch + 里程标准离线资产，接入时实现）；`writeback.py` 占位 `travel_compliance_rule` + `travel_audit_travels_builder`（从 serviceData 取行程填 `auditTravels`）。骨架先不注册进 `get_profile`，`get_profile("travel")` 抛带提示的错。接入时填实现再注册，避免 YAGNI 半成品。

### 6. 调用方加 `--profile`（默认 telecom）
- `rabbitmq_worker.py`：`create_worker` 加 `profile` 参数透传；CLI 加 `--profile` + `--telecom-asset-dir`（及后续 travel 资产参数）。`--graph-path` 不传时回落 `profile.default_graph_path`。
- `execute_graph.py`、`prepare_from_queue.py`：同样加 `--profile`，默认 telecom。
- **`main.py` 移出 scope**：它是死代码（`process_receipt_pipeline` 直接 `ReceiptDataPreparer()`、不走 bootstrap、无 argparse；`test_main.py:1593-1594` 断言已移除 `build_cli_parser`/`main_cli`），加 `--profile` 无意义。单独清理或不动。

---

## 测试影响与验证

**重构前先补 characterization test**（关键）：`writeback.py:275-281` 的 `*电信服务*` 合规判断目前**零测试覆盖**（`test_writeback.py` grep `compliance` 无命中）。搬迁前补：`goodsName="*电信服务*违约金"` → `compliance=False`、`"普通商品"` → `True`、`""` → `True`。搬迁后跑同断言。

**test_main.py 改动量**（明确列出）：10 处直接构造 `ReceiptDataPreparer` 并传 `telecom_list_provider=`（L644/719/788/880/932/1426/1509/1573 等，均 `lambda: []` 或 `lambda: [["电信","深圳"]]`），加断言 `serviceData["telecom_list"]`（L1308/1340-1341）。字段移除后这 10 处全 break，需逐处补 `receipt_enrichers={"telecom_list": telecom_receipt_enricher}`（或等价 lambda）让断言继续成立。10+ 处带判断的机械改动，非"定位一下"那么轻。

**其余**：`assemble_result_audit_info` 新增参数均有默认值 → 现有 2 参数调用不破。`@patch("expense_audit_orchestrator.bootstrap.xxx")` 路径不变（包未改名）。

**回归命令**（readme.md:391）：
```
cd /mnt/d/gorules/expense_audit
.venv/bin/python -m unittest test_execute_graph.py test_main.py -v
```

**端到端验证**（readme.md:30-59 mock 链路）：
- 启动 mock_server + graph_runtime(8090) + node_gateway(8091)。
- `execute_graph.py --profile telecom --receipt-code REC20260603001` 跑通，`serviceData.telecom_list` 仍存在，回写 payload 中 `*电信服务*` 合规判断与改动前一致。
- 对照改动前导出的 `output/` 里一份 prepared/writeback JSON，diff 确认字段无回归。
- **补一条**：对 `prepare_from_queue.py --writeback-output-path` 导出路径跑一次，断言导出 JSON 里 `auditInvoiceInfoContents[*].compliance` 对 `*电信服务*违约金` 为 False——专门覆盖直接调用路径（防点 C 注入漏改）。
- `test_writeback_client.py` 现有 `build_receipt_writeback_file_sink` 测试(L116)补 compliance 断言，覆盖文件 sink 路径。

---

## 不做（Out of scope）
- 包重命名 `expense_audit_orchestrator` → `receipt_audit_client`：独立后续 PR，不与本重构捆绑（~50 处 import + 50 处 patch 路径替换会让 diff 不可审）。profile 落地、测试绿后再单独重命名。
- 不引入 `BaseReceiptDataPreparer` 类继承体系。
- travel profile 只放骨架，不实现具体数据源（等真实接入）。
- 离线资产热重载不做（重启 worker 生效，标准做法）。
