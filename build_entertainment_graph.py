"""
构建业务招待费稽核工作流 graph JSON。

基于通讯费 graph-latest-0722-1100.json 的结构，为业务招待费生成新的 graph：
- 复用通用稽核节点（E35/E31/E33/sys-001-004/E09/E05/E17）
- 删除通讯费旧版节点，并重建业务招待费所需的 E01 抬头检查
- 新增 4 个业务招待费特有节点（E36 禁止内容、E15 员工本人费用、W33 礼品数量、W31 虚开发票预警）
- 泛化判断用 LLM+prompt 解决，不用规则

用法:
    python build_entertainment_graph.py
输出:
    graph-latest-entertainment-0722.json
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SRC_GRAPH = REPO_ROOT / "graph-latest-0722-1100.json"
DST_GRAPH = REPO_ROOT / "graph-latest-entertainment-0722.json"

# 通讯费中需要删除的节点 ID（通讯费特有）
NODE_IDS_TO_DELETE = {
    "d3046965-dbaf-41cd-ba93-0d957fe67ec8",  # 抬头检查 E01
    "e2b630c8-096d-4fea-ad4c-986473d8a880",  # 抬头个人检查 W28
    "d13f7062-96e4-4d74-a552-dfcc60d98ff4",  # 税号检查 E02
    "b9c42bf4-8560-4493-bf19-b8623f635c4d",  # 发票运营商检查 E19
    "df8506e4-11fd-4e0d-8198-17a25fcb4f50",  # 发票抬头预处理
    "f27969dd-cdcd-43da-9e75-328e4546239c",  # 发票报销年份检查 E32
    "bbdba655-bd68-47bd-9944-cca70b73f06d",  # 手机号检查 W32
    "6acb7b84-51a3-4d7d-9556-960d459d518d",  # 手机号账期抽取prompt
    "c7aa4eb5-df79-49aa-8e16-c5d7e455c903",  # 手机号账期调用llm
    "a6e16f2e-1e43-4f71-8895-0ee44210a4c7",  # 手机号账期抽取后处理
    "c442459e-4762-485c-b4bf-deae05a2b896",  # 发票合规prompt (通讯费特有)
    "514e15db-3657-4fa3-9228-88b750ea08f8",  # 调用llm (发票合规)
    "e0603963-d6e9-47da-adbc-a23c0841d087",  # 发票内容金额检查 E34
    "06969d8b-16b9-4784-8883-a872f3667838",  # 发票内容预处理（内容已在数据准备阶段生成）
}

# 决策表标准输出字段定义（复用通讯费的 field id，保证一致性）
# 这些 field id 在通讯费 graph 中已定义，复用可保持回写兼容
STD_OUTPUTS = [
    ("f88cbb46-eb13-4ceb-9c68-0983f58e985f", "核销单号", "instance_code"),
    ("48a29115-f542-44d3-8c02-3ff71e19ee38", "稽核代码", "reason_code"),
    ("f35ede49-0eae-4dda-b39e-11a11383697a", "识别结果（warning  reject   pass）", "distinguish_result"),
    ("ae1e04a0-7a6d-4a62-ba4b-734954c8ed5e", "稽核内容", "audit_content"),
    ("14801e5c-ebef-43a4-a56a-23cb5adf4a83", "稽核类型", "audit_type"),
    ("d9a8e0e2-8a13-4a39-b50e-c339519e811e", "识别内容", "distinguish_content"),
    ("f47902a7-1dc2-41c9-8375-fa15174317bd", "发票文件主键", "invoice_file_id"),
    ("629d6a9c-0e78-40c1-baef-0abea4b1e67f", "发票信息主键", "invoice_info_id"),
    ("509fd9ba-3996-4e4a-9021-df6513ed6807", "日志内容", "message"),
    ("a1b2c3d4-0000-0000-0000-regulation0", "制度", "policiesIndex"),
    ("a1b2c3d4-0000-0000-0000-suggestion0", "建议", "employeeSuggestionTips"),
    ("a1b2c3d4-0000-0000-0000-createtime0", "创建时间", "create_time"),
]

# 输入条件列 field id（复用通讯费的 input field id）
INPUT_FIELD_ID = "dea9a1bc-66ae-47b3-885f-9e9a1bb07571"


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _std_outputs() -> list[dict]:
    """生成标准 12 列输出定义。"""
    return [
        {"id": fid, "name": name, "field": field}
        for fid, name, field in STD_OUTPUTS
    ]


def _std_rule_row(
    input_value: str,
    reason_code: str,
    distinguish_result: str,
    audit_content: str,
    audit_type: str,
    message: str,
    policies_index: str,
    suggestion: str,
) -> dict:
    """生成一条决策表规则行（标准 12 字段）。"""
    return {
        "_id": _new_uuid(),
        "_description": "",
        INPUT_FIELD_ID: input_value,
        "f88cbb46-eb13-4ceb-9c68-0983f58e985f": "instance_code",
        "48a29115-f542-44d3-8c02-3ff71e19ee38": f'"{reason_code}"',
        "f35ede49-0eae-4dda-b39e-11a11383697a": f'"{distinguish_result}"',
        "ae1e04a0-7a6d-4a62-ba4b-734954c8ed5e": f'"{audit_content}"',
        "14801e5c-ebef-43a4-a56a-23cb5adf4a83": f'"{audit_type}"',
        "d9a8e0e2-8a13-4a39-b50e-c339519e811e": '""',
        "f47902a7-1dc2-41c9-8375-fa15174317bd": "invoice_file_id",
        "629d6a9c-0e78-40c1-baef-0abea4b1e67f": "invoice_info_id",
        "509fd9ba-3996-4e4a-9021-df6513ed6807": message,
        "a1b2c3d4-0000-0000-0000-regulation0": policies_index,
        "a1b2c3d4-0000-0000-0000-suggestion0": suggestion,
        "a1b2c3d4-0000-0000-0000-createtime0": "context.executionTime",
    }


def _make_decision_table(
    node_id: str,
    name: str,
    input_field: str,
    input_name: str,
    rules: list[dict],
    output_path: str,
    position: dict,
    extra_outputs: list[dict] | None = None,
) -> dict:
    """构造一个 decisionTableNode。"""
    outputs = _std_outputs()
    if extra_outputs:
        outputs = extra_outputs + outputs
    return {
        "type": "decisionTableNode",
        "content": {
            "hitPolicy": "first",
            "rules": rules,
            "inputs": [
                {"id": INPUT_FIELD_ID, "name": input_name, "field": input_field}
            ],
            "outputs": outputs,
            "passThrough": False,
            "inputField": None,
            "outputPath": output_path,
            "executionMode": "single",
        },
        "id": node_id,
        "name": name,
        "position": position,
    }


# ---------------------------------------------------------------------------
# Phase 1: 改造数据校验预处理 expressionNode
# ---------------------------------------------------------------------------

ENTERTAINMENT_PREPROCESS_EXPRESSIONS = [
    {
        "id": _new_uuid(),
        "key": "isInvoicType",
        # 业务招待费禁止：增值税专用发票、增值税电子专用发票、电子发票（增值税专用发票）、海关专用缴款书
        # isInvoicType = True 表示在适用票种范围（即不在禁止列表）
        # 注意：serviceData.expenseInvoiceTypes 是允许的票种列表，与通讯费逻辑一致
        "value": 'invoiceType in map(serviceData.expenseInvoiceTypes as c, c.manufacturerBillCode)',
    },
    {
        "id": _new_uuid(),
        "key": "isCompanyExists",
        # 先用发票 orgNumber 匹配财务体系公司编码 ccode，再比较发票抬头公司名称。
        # 必须同时满足组织编码和公司名称一致，避免仅因公司名称存在于全量 companyList 而误通过。
        "value": '(orgNumber ?? "") != "" and some((serviceData.companyList ?? []) as c, (c.ccode ?? c.cCode ?? c.accountingCode ?? "") != "" and (c.ccode ?? c.cCode ?? c.accountingCode ?? "") == (orgNumber ?? "") and (orgName ?? buyerName ?? "") != "" and (orgName ?? buyerName ?? "") == (c.companyName ?? c.cName ?? c.cname ?? ""))',
    },
    {
        "id": _new_uuid(),
        "key": "isBuyerCompany",
        # 购买方名称带公司/企业类后缀时，按公司抬头处理；否则按员工号/个人抬头处理。
        # 个人抬头不做公司税号比对，避免员工号被误判为税号不一致。
        "value": '(endsWith((buyerName ?? orgName ?? ""), "公司") or endsWith((buyerName ?? orgName ?? ""), "企业") or endsWith((buyerName ?? orgName ?? ""), "集团") or endsWith((buyerName ?? orgName ?? ""), "事务所") or endsWith((buyerName ?? orgName ?? ""), "商行") or endsWith((buyerName ?? orgName ?? ""), "合作社"))',
    },
    {
        "id": _new_uuid(),
        "key": "isTaxExists",
        # 公司抬头必须有税号且与 orgNumber 对应的财务体系公司税号一致；
        # 非公司抬头按员工号处理，直接通过 E02。
        "value": '((endsWith((buyerName ?? orgName ?? ""), "公司") or endsWith((buyerName ?? orgName ?? ""), "企业") or endsWith((buyerName ?? orgName ?? ""), "集团") or endsWith((buyerName ?? orgName ?? ""), "事务所") or endsWith((buyerName ?? orgName ?? ""), "商行") or endsWith((buyerName ?? orgName ?? ""), "合作社")) == false) or ((buyerTaxNo ?? buyerTaxNO ?? "") != "" and (orgNumber ?? "") != "" and some((serviceData.companyList ?? []) as c, (c.ccode ?? c.cCode ?? c.accountingCode ?? "") == (orgNumber ?? "") and (buyerTaxNo ?? buyerTaxNO ?? "") == (c.companyTax ?? c.taxNo ?? c.taxpayerIdentificationNumber ?? "")))',
    },
    {
        "id": _new_uuid(),
        "key": "isAmountEnough",
        "value": 'number(serviceData.auditInfo.applyAmount ?? 0) <= number(invoiceAmount ?? 0)',
    },
    {
        "id": _new_uuid(),
        "key": "isCompanyBlacklist",
        "value": 'salerName not in  map(serviceData.companyBlacklist as c, c.value)',
    },
    {
        "id": _new_uuid(),
        "key": "isWriteOff",
        "value": 'invoiceNo not in map((serviceData.invoiceUsageHistory ?? []) as c, c.chequeNo)',
    },
    {
        "id": _new_uuid(),
        "key": "isCurrentYearInvoice",
        "value": '(invoiceDate ?? "") != "" and (serviceData.auditInfo.submitTime ?? "") != "" and d(invoiceDate).year() == d(serviceData.auditInfo.submitTime).year()',
    },
    {
        # 规则3：员工本人费用检查 - 旅客姓名不允许与核销人姓名一致
        # isNotSelfExpense = True 表示不是员工本人费用（通过）
        # 仅对 E15 配置的六类票据进行姓名比对；未知/非适用票种按通过处理。
        # 适用票据：飞机行程单、火车票、客运票、数电铁路、数电航空、数电普票。
        # isNotSelfExpense = True 表示不是员工本人费用（通过）。
        "id": _new_uuid(),
        "key": "isNotSelfExpense",
        "value": '(serviceData.e15InvoiceType.isApplicable ?? false) == false or (passengerName ?? "") == "" or (passengerName ?? "") != (serviceData.auditInfo.verifiUserName ?? "")',
    },
    {
        # 规则4：礼品数量合理性 - 接待人数≤全部发票中购买商品数量总和则合理
        # isGiftCountReasonable = True 表示合理（通过）。执行时由核销单循环注入
        # 当前累计商品数量；最后一张发票上的累计值即全部发票商品数量总和。
        # 接待人数来自核销单业务费用明细接口，项目类别由 hasGiftItem 标识。
        "id": _new_uuid(),
        "key": "isGiftCountReasonable",
        "value": '((isLastInvoice ?? true) == false) or (serviceData.entertainment_data.hasGiftItem ?? false) == false or number(serviceData.entertainment_data.giftReceptionCount ?? 0) <= number(totalGoodsCount ?? sum(map((items ?? []) as i, number(i.num ?? i.quantity ?? 0))))',
    },
    {
        # 规则：同一核销单内发票号应连续。预处理层可提供完整发票号序列；
        # 未提供序列时不作猜测，按通过处理。
        "id": _new_uuid(),
        "key": "isInvoiceNumberContinuous",
        "value": '(previousInvoiceNumbers ?? []).length == 0 or invoiceNo == "" or some((previousInvoiceNumbers ?? []) as prev, abs(number(invoiceNo) - number(prev)) == 1)',
    },
]


# ---------------------------------------------------------------------------
# Phase 2: 新增业务招待费特有规则节点
# ---------------------------------------------------------------------------

def _build_company_header_check_node() -> dict:
    """规则：发票购买方公司与核销单财务体系公司检查 E01。"""
    node_id = "d3046965-dbaf-41cd-ba93-0d957fe67ec8"
    rules = [
        _std_rule_row(
            input_value="true",
            reason_code="E01",
            distinguish_result="PASS",
            audit_content="检查发票购买方公司名称与核销单财务体系映射公司名称是否一致",
            audit_type="general-rules",
            message='""',
            policies_index='""',
            suggestion='""',
        ),
        _std_rule_row(
            input_value="false",
            reason_code="E01",
            distinguish_result="REJECT",
            audit_content="检查发票购买方公司名称与核销单财务体系映射公司名称是否一致",
            audit_type="general-rules",
            message='"发票号【"+(invoiceNo??"")+"】✗ 发票抬头公司【"+(orgName??buyerName??"")+"】未与核销单财务体系公司匹配（组织编码【"+(orgNumber??"")+"】），✓ 发票购买方公司名称必须与核销单财务体系映射公司一致"',
            policies_index='"《锐捷网络员工费用管理与报销制度》\n5.2.1.1 员工需要确保票据中公司信息的准确性。"',
            suggestion='"【重新开票】请按核销单财务体系映射的公司名称和组织编码重新开具发票，并重新上传"',
        ),
    ]
    return _make_decision_table(
        node_id=node_id,
        name="发票购买方公司检查",
        input_field="isCompanyExists",
        input_name="发票购买方公司是否与核销单财务体系公司一致",
        rules=rules,
        output_path="header_result",
        position={"x": 660, "y": 900},
    )


def _build_tax_number_check_node() -> dict:
    """规则：公司抬头的发票购买方税号与财务体系公司税号检查 E02。"""
    node_id = "d13f7062-96e4-4d74-a552-dfcc60d98ff4"
    audit_content = "检查发票购买方纳税人识别号与核销单财务体系映射纳税人识别号是否一致"
    rules = [
        _std_rule_row(
            input_value="true",
            reason_code="E02",
            distinguish_result="PASS",
            audit_content=audit_content,
            audit_type="general-rules",
            message='""',
            policies_index='""',
            suggestion='""',
        ),
        _std_rule_row(
            input_value="false",
            reason_code="E02",
            distinguish_result="REJECT",
            audit_content=audit_content,
            audit_type="general-rules",
            message='"发票号【"+(invoiceNo??"")+"】✗ 发票购买方【"+(buyerName??orgName??"")+"】按公司抬头处理，但纳税人识别号【"+(buyerTaxNo??buyerTaxNO??"")+"】未与组织编码【"+(orgNumber??"")+"】对应的财务体系公司纳税人识别号一致，✓ 公司抬头发票必须使用核销单财务体系映射公司的纳税人识别号"',
            policies_index='"《锐捷网络员工费用管理与报销制度》\n5.2.1.1 员工需要确保票据中公司信息的准确性。"',
            suggestion='"【重新开票】请按核销单财务体系映射公司的正确抬头和纳税人识别号重新开具发票，并重新上传"',
        ),
    ]
    return _make_decision_table(
        node_id=node_id,
        name="发票购买方税号检查",
        input_field="isTaxExists",
        input_name="公司抬头发票购买方纳税人识别号是否与财务体系公司一致",
        rules=rules,
        output_path="tax_result",
        position={"x": 660, "y": 940},
    )



def _build_self_expense_check_node() -> dict:
    """规则3：员工本人费用检查 E15。"""
    node_id = "ent-self-expense-check"
    rules = [
        _std_rule_row(
            input_value="true",
            reason_code="E15",
            distinguish_result="PASS",
            audit_content="检查是否有员工本人费用（员工本人的交通费票据不属于业务招待费）",
            audit_type="general-rules",
            message='""',
            policies_index='""',
            suggestion='""',
        ),
        _std_rule_row(
            input_value="false",
            reason_code="E15",
            distinguish_result="REJECT",
            audit_content="检查是否有员工本人费用（员工本人的交通费票据不属于业务招待费）",
            audit_type="general-rules",
            message='"发票号【"+(invoiceNo??"")+"】✗ 票面旅客姓名【"+(passengerName??"")+"】与核销单核销人姓名【"+(serviceData.auditInfo.verifiUserName??"")+"】一致，✓ 业务招待费不得报销核销人本人的交通、住宿等票据"',
            policies_index='"《锐捷网络员工费用管理与报销制度》\\n4.4.2.2 禁止报销：员工本人的差旅费开支"',
            suggestion='"【删除发票】删除本票据，业务招待费不得报销员工本人的费用"',
        ),
    ]
    return _make_decision_table(
        node_id=node_id,
        name="员工本人费用检查",
        input_field="isNotSelfExpense",
        input_name="是否非员工本人费用",
        rules=rules,
        output_path="self_expense_result",
        position={"x": 660, "y": 980},
    )


def _build_gift_count_check_node() -> dict:
    """规则4：礼品数量合理性检查 W33（弱控 WARNING）。"""
    node_id = "ent-gift-count-check"
    rules = [
        _std_rule_row(
            input_value="true",
            reason_code="W33",
            distinguish_result="PASS",
            audit_content="检查【项目类别】为赠送纪念品中接待人数量与发票中购买商品数量的合理性",
            audit_type="staff-behavior",
            message='""',
            policies_index='""',
            suggestion='""',
        ),
        _std_rule_row(
            input_value="false",
            reason_code="W33",
            distinguish_result="WARNING",
            audit_content="检查【项目类别】为赠送纪念品中接待人数量与发票中购买商品数量的合理性",
            audit_type="staff-behavior",
            message='"发票号【"+(invoiceNo??"")+"】✗ 全部发票购买商品数量【"+string(totalGoodsCount??sum(map((items??[]) as i, number(i.num??i.quantity??0))))+"】少于赠送纪念品接待人数【"+string(serviceData.entertainment_data.giftReceptionCount??"")+"】，存在礼品数量与接待人数不匹配的异常风险，✓ 赠送纪念品的商品数量需≥接待人数，确保一人一份的合理配比"',
            policies_index='"《锐捷网络员工费用管理与报销制度》\\n5.2票据使用规范"',
            suggestion='"【业务确认】请确认礼品数量与接待人数是否匹配，如为实际业务发生的合理配比，请在单据备注栏说明具体接待情况及礼品分配逻辑，财务将进行人工复核"',
        ),
    ]
    return _make_decision_table(
        node_id=node_id,
        name="礼品数量合理性检查",
        input_field="isGiftCountReasonable",
        input_name="礼品数量是否合理",
        rules=rules,
        output_path="gift_count_result",
        position={"x": 660, "y": 1080},
    )


def _build_invoice_number_check_node() -> dict:
    """发票号码连续性检查 W34（普通规则）。"""
    node_id = "ent-invoice-number-check"
    rules = [
        _std_rule_row(
            input_value="true",
            reason_code="W34",
            distinguish_result="PASS",
            audit_content="检查同一核销单内发票号码是否连续",
            audit_type="general-rules",
            message='""',
            policies_index='""',
            suggestion='""',
        ),
        _std_rule_row(
            input_value="false",
            reason_code="W34",
            distinguish_result="WARNING",
            audit_content="检查同一核销单内发票号码是否连续",
            audit_type="general-rules",
            message='"发票号【"+(invoiceNo??"")+"】与本核销单其他发票号码不连续，存在发票缺失风险"',
            policies_index='"《锐捷网络员工费用管理与报销制度》\\n5.2票据使用规范"',
            suggestion='"【补充确认】请核对本核销单发票号码是否完整连续，并补充缺失票据或说明原因"',
        ),
    ]
    return _make_decision_table(
        node_id=node_id,
        name="发票号码连续性检查",
        input_field="isInvoiceNumberContinuous",
        input_name="发票号码是否连续",
        rules=rules,
        output_path="invoice_number_result",
        position={"x": 660, "y": 1180},
    )


# ---------------------------------------------------------------------------
# Phase 3: LLM 内容合规节点改造（合并规则2 E36 + 规则6 E17）
# ---------------------------------------------------------------------------

ENTERTAINMENT_CONTENT_PROMPT_SOURCE = r"""export const handler = async (input) => {
  // 发票内容唯一使用数据准备阶段由 items[*].goodsName 汇总出的单据级 goodsName。
  const goodsName = String(input.goodsName ?? '');
  // 用 JSON.stringify 注入，避免商品名称中的反引号或 ${...} 破坏模板字符串。
  const goodsNameForPrompt = JSON.stringify(goodsName);

  const optimizedPrompt = `# 角色
你是企业费用制度合规审核专家，负责判断业务招待费发票【项目名称】是否命中公司制度的禁止报销清单。

# 审核范围
只根据下面给出的发票项目名称判断，不要根据金额、销货方、发票类型或关键词的单独出现做推断。
公司制度明确禁止报销的项目为：
1. 黄金（作为贵金属、金条、金币、黄金制品等）；
2. 珠宝；
3. 首饰；
4. 茅台；
5. 五粮液；
6. 礼品卡；
7. 充值卡（包括明确属于预付/储值/充值卡的项目）。

# 语义判断要求（非常重要）
这是语义审核，不是字符串包含判断：
- “黄金针菇”是蔬菜，不是黄金，必须通过；
- “黄金饰品”“黄金手链”“黄金摆件”属于黄金/首饰，必须不通过；
- “珠宝首饰”“钻石珠宝”等明确属于珠宝或首饰，必须不通过；
- “茅台酒”“五粮液白酒”明确属于对应品牌，必须不通过；
- 仅因出现“黄金”二字但实际是食品、植物、地名或其他普通商品（例如黄金针菇、黄金果），不能判为不通过；
- 仅有“礼品”二字不等于礼品卡；只有明确是礼品卡、充值卡或预付/储值卡等卡类/资金预存项目时才不通过；
- 项目名称含义不清、无法确认命中禁止清单时，默认通过；
- 多个项目名称中只要有一个明确命中禁止清单，就判定不通过。

# 输出要求
必须只返回一个标准 JSON 对象，不得返回 Markdown、解释、推理过程或其他文字：
{
  "passed": true 或 false,
  "violationType": "none"、"prohibited_item" 或 "recharge_card"
}

字段约束：
- passed=true 时，violationType 必须为 "none"；
- 命中黄金、珠宝、首饰、茅台、五粮液时，violationType 返回 "prohibited_item"；
- 命中礼品卡、充值卡或明确的预付/储值卡时，violationType 返回 "recharge_card"。

# 待审核的发票项目名称（JSON 字符串）
${goodsNameForPrompt}`;

  return {
    prompt: optimizedPrompt,
    context: input.context || {}
  };
};
"""


def _build_content_compliance_prompt_node() -> dict:
    """改造充值卡检查prompt → 招待费内容合规prompt（合并规则2+规则6）。"""
    return {
        "type": "functionNode",
        "content": {"source": ENTERTAINMENT_CONTENT_PROMPT_SOURCE},
        "id": "ent-content-compliance-prompt",
        "name": "招待费内容合规prompt",
        "position": {"x": -365, "y": 1335},
    }


def _build_content_compliance_llm_node() -> dict:
    """调用llm 节点（复用通讯费的 LLM 调用代码）。

    LLM 网关地址必须从 context.llmGatewayUrl 读取（由 orchestrator 注入，
    统一来自 .env 的 NODE_GATEWAY_URL）；未注入时抛错，不再回退到硬编码地址。
    """
    source = (
        "import http from 'http';\r\n"
        "\r\n"
        "export const handler = async (input) => {\r\n"
        "  try {\r\n"
        "    const context = input && input.context ? input.context : {};\r\n"
        "    if (!context.llmGatewayUrl) {\r\n"
        "      throw new Error('llmGatewayUrl not injected in context; configure NODE_GATEWAY_URL in .env');\r\n"
        "    }\r\n"
        "    const LOCAL_LLM_URL = context.llmGatewayUrl;\r\n"
        "    const prompt = input.prompt || context.prompt || (input.prev && input.prev.prompt) || '';\r\n"
        "\r\n"
        "    if (!prompt) {\r\n"
        "      throw new Error('missing prompt from previous component output');\r\n"
        "    }\r\n"
        "\r\n"
        "    const payload = {\r\n"
        "      prompt\r\n"
        "    };\r\n"
        "\r\n"
        "    if (input.systemPrompt || context.systemPrompt) {\r\n"
        "      payload.systemPrompt = input.systemPrompt || context.systemPrompt;\r\n"
        "    }\r\n"
        "\r\n"
        "    if (typeof input.temperature === 'number' || typeof context.temperature === 'number') {\r\n"
        "      payload.temperature = typeof input.temperature === 'number' ? input.temperature : context.temperature;\r\n"
        "    }\r\n"
        "\r\n"
        "    if (input.context && input.context.runId) { payload.runId = input.context.runId; }\r\n"
        "    if (input.context && input.context.receiptCode) { payload.receiptCode = input.context.receiptCode; }\r\n"
        "    if (input.context && input.context.invoiceKey) { payload.invoiceKey = input.context.invoiceKey; }\r\n"
        "    const response = await http.post(LOCAL_LLM_URL, payload);\r\n"
        "\r\n"
        "    if (!response || response.status < 200 || response.status >= 300) {\r\n"
        "      throw new Error('local llm service failed at ' + LOCAL_LLM_URL + ': ' + (response && response.status ? response.status : 'unknown'));\r\n"
        "    }\r\n"
        "\r\n"
        "    const result = typeof response.data === 'string' ? JSON.parse(response.data) : (response.data || {});\r\n"
        "\r\n"
        "    return {\r\n"
        "      llm_status: result.llmStatus || 'error',\r\n"
        "      llm_result: result.llmResult || null,\r\n"
        "      raw_content: result.rawContent || null,\r\n"
        "      error_message: result.errorMessage || null\r\n"
        "    };\r\n"
        "  } catch (error) {\r\n"
        "    return {\r\n"
        "      llm_status: 'error',\r\n"
        "      llm_result: null,\r\n"
        "      raw_content: null,\r\n"
        "      error_message: error && error.message ? error.message : String(error)\r\n"
        "    };\r\n"
        "  }\r\n"
        "};\r\n"
    )
    return {
        "type": "functionNode",
        "content": {"source": source},
        "id": "ent-content-compliance-llm",
        "name": "调用llm",
        "position": {"x": -100, "y": 1335},
    }


def _build_content_compliance_postprocess_node() -> dict:
    """改造发票内容检查后处理 → 招待费内容合规后处理。"""
    return {
        "type": "expressionNode",
        "content": {
            "expressions": [
                {
                    "id": _new_uuid(),
                    "key": "contentCheckResult",
                    "value": '(llm_status ?? "error") == "success" ? (((llm_result ?? {}).passed == true) ? "pass" : (((llm_result ?? {}).violationType == "recharge_card") ? "recharge_card" : "prohibited_item")) : "error"',
                },
            ],
            "passThrough": True,
            "inputField": None,
            "outputPath": None,
            "executionMode": "single",
        },
        "id": "ent-content-compliance-postprocess",
        "name": "招待费内容合规后处理",
        "position": {"x": 200, "y": 1400},
    }


def _build_content_compliance_check_node() -> dict:
    """改造发票充值卡检查 → 招待费内容合规检查（合并 E36 + E17）。"""
    node_id = "ent-content-compliance-check"
    rules = [
        # LLM 调用失败
        {
            "_id": _new_uuid(),
            "_description": "LLM调用失败",
            INPUT_FIELD_ID: '"error"',
            "f88cbb46-eb13-4ceb-9c68-0983f58e985f": "instance_code",
            "48a29115-f542-44d3-8c02-3ff71e19ee38": '"E36"',
            "f35ede49-0eae-4dda-b39e-11a11383697a": '"FAILED"',
            "ae1e04a0-7a6d-4a62-ba4b-734954c8ed5e": '"检查发票内容是否含禁止核销内容或充值卡信息"',
            "14801e5c-ebef-43a4-a56a-23cb5adf4a83": '"general-rules"',
            "d9a8e0e2-8a13-4a39-b50e-c339519e811e": '""',
            "f47902a7-1dc2-41c9-8375-fa15174317bd": "invoice_file_id",
            "629d6a9c-0e78-40c1-baef-0abea4b1e67f": "invoice_info_id",
            "509fd9ba-3996-4e4a-9021-df6513ed6807": '"LLM服务调用失败，内容合规检查无法执行"',
            "a1b2c3d4-0000-0000-0000-regulation0": '""',
            "a1b2c3d4-0000-0000-0000-suggestion0": '""',
            "a1b2c3d4-0000-0000-0000-createtime0": "context.executionTime",
        },
        # 通过
        _std_rule_row(
            input_value='"pass"',
            reason_code="E36",
            distinguish_result="PASS",
            audit_content="检查发票内容是否含禁止核销内容或充值卡信息",
            audit_type="general-rules",
            message='""',
            policies_index='""',
            suggestion='""',
        ),
        # 禁止核销内容（高档消费品/现金等价物）
        _std_rule_row(
            input_value='"prohibited_item"',
            reason_code="E36",
            distinguish_result="REJECT",
            audit_content="检查发票内容是否含禁止核销内容或充值卡信息",
            audit_type="general-rules",
            message='"发票号【"+(invoiceNo??"")+"】✗ 发票内容【"+(goodsName??"")+"】属于公司制度禁止报销的范围，✓ 业务招待费应遵循\"厉行节约，合理开支\"的原则，不得报销黄金、珠宝、首饰、茅台、五粮液、礼品卡、充值卡等违规内容"',
            policies_index='"《锐捷网络员工费用管理与报销制度》\\n4.4.2.2 禁止报销（高档烟酒如茅台、五粮液；现金及其等价物如黄金、珠宝首饰、预付卡、礼品卡等）；4.4.3.1 业务招待要遵循\"厉行节约，合理开支\"原则"',
            suggestion='"【删除发票】删除本票据，不得上传禁止报销范围内的发票"',
        ),
        # 礼品卡/充值卡单独保留匹配项，确保 E36 对清单中的卡类项目也有结果。
        _std_rule_row(
            input_value='"recharge_card"',
            reason_code="E36",
            distinguish_result="REJECT",
            audit_content="检查发票内容是否含禁止核销内容或充值卡信息",
            audit_type="general-rules",
            message='"发票号【"+(invoiceNo??"")+"】✗ 发票内容【"+(goodsName??"")+"】属于公司制度禁止报销的礼品卡/充值卡等卡类项目"',
            policies_index='"《锐捷网络员工费用管理与报销制度》\\n4.4.2.2 禁止报销：礼品卡、充值卡等现金及其等价物"',
            suggestion='"【删除发票】删除本票据，不得上传礼品卡、充值卡或其他预付/储值卡类发票"',
        ),
    ]
    return _make_decision_table(
        node_id=node_id,
        name="招待费内容合规检查",
        input_field="contentCheckResult",
        input_name="内容合规检查结果",
        rules=rules,
        output_path="content_compliance_result",
        position={"x": 675, "y": 1390},
    )


def _build_recharge_card_check_node() -> dict:
    """独立输出 E17，确保普通内容也有 E17/PASS 结果。"""
    rules = [
        _std_rule_row(
            input_value='"pass"',
            reason_code="E17",
            distinguish_result="PASS",
            audit_content="检查发票内容是否包含充值卡、预付卡或预存类项目",
            audit_type="general-rules",
            message='""',
            policies_index='""',
            suggestion='""',
        ),
        _std_rule_row(
            input_value='"prohibited_item"',
            reason_code="E17",
            distinguish_result="PASS",
            audit_content="检查发票内容是否包含充值卡、预付卡或预存类项目",
            audit_type="general-rules",
            message='""',
            policies_index='""',
            suggestion='""',
        ),
        _std_rule_row(
            input_value='"recharge_card"',
            reason_code="E17",
            distinguish_result="REJECT",
            audit_content="检查发票内容是否包含充值卡、预付卡或预存类项目",
            audit_type="general-rules",
            message='"发票号【"+(invoiceNo??"")+"】✗ 发票内容【"+(goodsName??"")+"】包含充值卡/预付卡/预存等公司禁止报销项"',
            policies_index='"《锐捷网络员工费用管理与报销制度》\\n4.2.2.2 禁止报销：预付卡销售、充值卡、成品油(卡)"',
            suggestion='"【删除发票】删除本票据，并提供非充值内容的发票"',
        ),
    ]
    return _make_decision_table(
        node_id="ent-recharge-card-check",
        name="充值卡检查",
        input_field="contentCheckResult",
        input_name="充值卡检查结果",
        rules=rules,
        output_path="recharge_card_result",
        position={"x": 900, "y": 1390},
    )


# ---------------------------------------------------------------------------
# Phase 4: 虚开发票预警节点（W31，LLM+规则混合）
# ---------------------------------------------------------------------------

FRAUD_PREPROCESS_EXPRESSIONS = [
    {
        "id": _new_uuid(),
        # 系统规则1：费用项目含"营销活动类" && 部门为EBG/TBU销售 && 发票内容含"餐饮费"
        # && 提单日期-开票日期≤2 && 税率0%/1%/3% && 金额≥900
        "key": "isHighRiskByRule",
        "value": (
            'contains((serviceData.auditInfo.expenseItemName ?? ""), "营销活动") '
            'and (contains((serviceData.auditInfo.deptName ?? ""), "EBG") or contains((serviceData.auditInfo.deptName ?? ""), "TBU")) '
            'and contains((goodsName ?? ""), "餐饮") '
            'and number(serviceData.auditInfo.submitTime ?? "") - number(invoiceDate ?? "") <= 2 '
            'and (invoiceTaxRate == "0%" or invoiceTaxRate == "1%" or invoiceTaxRate == "3%") '
            'and number(invoiceAmount ?? 0) >= 900'
        ),
    },
    {
        "id": _new_uuid(),
        # 企查查规则2：企业注册日期与发票开具日期相差在6个月以内
        "key": "isRecentlyRegistered",
        "value": (
            '(d(serviceData.salerCompanyInfo.establishDate ?? "").isValid() '
            'and d(invoiceDate ?? "").isValid()) '
            '? (d(invoiceDate).isSameOrAfter(d(serviceData.salerCompanyInfo.establishDate)) '
            'and d(invoiceDate).isSameOrBefore('
            'd(serviceData.salerCompanyInfo.establishDate).add(6, "month"))) '
            ': false'
        ),
    },
    {
        "id": _new_uuid(),
        # 是否需要 LLM 检查注册地址异常
        "key": "needLlmAddressCheck",
        "value": '$.isHighRiskByRule or $.isRecentlyRegistered',
    },
]


def _build_fraud_preprocess_node() -> dict:
    """虚开发票预警预处理 expressionNode。"""
    return {
        "type": "expressionNode",
        "content": {
            "expressions": FRAUD_PREPROCESS_EXPRESSIONS,
            "passThrough": True,
            "inputField": None,
            "outputPath": None,
            "executionMode": "single",
        },
        "id": "ent-fraud-preprocess",
        "name": "虚开发票预警预处理",
        "position": {"x": -650, "y": 1600},
    }


FRAUD_ADDRESS_PROMPT_SOURCE = r"""export const handler = async (input) => {
  const context = input.context || {};
  const needCheck = input.needLlmAddressCheck ?? context.needLlmAddressCheck ?? false;

  // 如果不需要 LLM 检查，直接返回通过
  if (!needCheck) {
    return {
      prompt: '无需检查，直接返回通过',
      context: input.context || {},
      skipLlm: true
    };
  }

  const salerName = input.salerName ?? context.salerName ?? '';
  const salerAddress = input.salerCompanyInfo?.address ?? context.salerCompanyInfo?.address ?? '';
  const allSalerAddresses = input.allSalerAddresses ?? context.allSalerAddresses ?? [];

  const optimizedPrompt = `# Role
你是一位企业税务风险审计专家，专门识别虚开发票的高风险特征。

# Task
请根据以下销货方企业注册地址信息，判断是否存在虚开发票风险。

# 人工规则（需 LLM 泛化判断）
1. 多家开票企业注册地点相似程度达90%以上（可能是同一地址的不同表述）
2. 企业注册地点为居民楼、小区、农村自建房等非商业经营场所

# 待审查数据
- 当前销货方名称：${salerName}
- 当前销货方注册地址：${salerAddress}
- 本核销单所有销货方注册地址列表：${JSON.stringify(allSalerAddresses)}

# 判定规则
- 如果当前地址与列表中其他地址相似度≥90%（如仅门牌号不同、表述略有差异），返回 passed=false
- 如果注册地址明显为居民楼、小区、农村等非商业场所，返回 passed=false
- 否则返回 passed=true
- 存在歧义时默认 passed=true

# 输出格式
你必须【仅仅且直接】返回一个标准的 JSON 对象：
{
  "passed": 判定结果（布尔类型 true 或 false）,
  "riskReason": 风险原因说明（字符串，passed=false 时填写具体风险点）
}`;

  return {
    prompt: optimizedPrompt,
    context: input.context || {}
  };
};
"""


def _build_fraud_address_prompt_node() -> dict:
    """虚开发票地址检查prompt functionNode。"""
    return {
        "type": "functionNode",
        "content": {"source": FRAUD_ADDRESS_PROMPT_SOURCE},
        "id": "ent-fraud-address-prompt",
        "name": "虚开发票地址检查prompt",
        "position": {"x": -365, "y": 1600},
    }


def _build_fraud_address_llm_node() -> dict:
    """虚开发票地址检查调用llm functionNode（复用通用 LLM 调用代码）。

    LLM 网关地址必须从 context.llmGatewayUrl 读取（由 orchestrator 注入，
    统一来自 .env 的 NODE_GATEWAY_URL）；未注入时抛错，不再回退到硬编码地址。
    """
    source = (
        "import http from 'http';\r\n"
        "\r\n"
        "export const handler = async (input) => {\r\n"
        "  try {\r\n"
        "    // 如果上游标记 skipLlm，直接返回通过\r\n"
        "    if (input.skipLlm === true) {\r\n"
        "      return {\r\n"
        "        llm_status: 'success',\r\n"
        "        llm_result: { passed: true, riskReason: '无需检查' },\r\n"
        "        raw_content: null,\r\n"
        "        error_message: null\r\n"
        "      };\r\n"
        "    }\r\n"
        "    const context = input && input.context ? input.context : {};\r\n"
        "    if (!context.llmGatewayUrl) {\r\n"
        "      throw new Error('llmGatewayUrl not injected in context; configure NODE_GATEWAY_URL in .env');\r\n"
        "    }\r\n"
        "    const LOCAL_LLM_URL = context.llmGatewayUrl;\r\n"
        "    const prompt = input.prompt || context.prompt || (input.prev && input.prev.prompt) || '';\r\n"
        "\r\n"
        "    if (!prompt) {\r\n"
        "      throw new Error('missing prompt from previous component output');\r\n"
        "    }\r\n"
        "\r\n"
        "    const payload = {\r\n"
        "      prompt\r\n"
        "    };\r\n"
        "\r\n"
        "    if (input.systemPrompt || context.systemPrompt) {\r\n"
        "      payload.systemPrompt = input.systemPrompt || context.systemPrompt;\r\n"
        "    }\r\n"
        "\r\n"
        "    if (typeof input.temperature === 'number' || typeof context.temperature === 'number') {\r\n"
        "      payload.temperature = typeof input.temperature === 'number' ? input.temperature : context.temperature;\r\n"
        "    }\r\n"
        "\r\n"
        "    if (input.context && input.context.runId) { payload.runId = input.context.runId; }\r\n"
        "    if (input.context && input.context.receiptCode) { payload.receiptCode = input.context.receiptCode; }\r\n"
        "    if (input.context && input.context.invoiceKey) { payload.invoiceKey = input.context.invoiceKey; }\r\n"
        "    const response = await http.post(LOCAL_LLM_URL, payload);\r\n"
        "\r\n"
        "    if (!response || response.status < 200 || response.status >= 300) {\r\n"
        "      throw new Error('local llm service failed at ' + LOCAL_LLM_URL + ': ' + (response && response.status ? response.status : 'unknown'));\r\n"
        "    }\r\n"
        "\r\n"
        "    const result = typeof response.data === 'string' ? JSON.parse(response.data) : (response.data || {});\r\n"
        "\r\n"
        "    return {\r\n"
        "      llm_status: result.llmStatus || 'error',\r\n"
        "      llm_result: result.llmResult || null,\r\n"
        "      raw_content: result.rawContent || null,\r\n"
        "      error_message: result.errorMessage || null\r\n"
        "    };\r\n"
        "  } catch (error) {\r\n"
        "    return {\r\n"
        "      llm_status: 'error',\r\n"
        "      llm_result: null,\r\n"
        "      raw_content: null,\r\n"
        "      error_message: error && error.message ? error.message : String(error)\r\n"
        "    };\r\n"
        "  }\r\n"
        "};\r\n"
    )
    return {
        "type": "functionNode",
        "content": {"source": source},
        "id": "ent-fraud-address-llm",
        "name": "虚开发票地址检查调用llm",
        "position": {"x": -100, "y": 1600},
    }


def _build_fraud_postprocess_node() -> dict:
    """虚开发票预警后处理 expressionNode。"""
    return {
        "type": "expressionNode",
        "content": {
            "expressions": [
                {
                    "id": _new_uuid(),
                    "key": "llmAddressPassed",
                    "value": '(llm_status ?? "error") == "success" ? ((llm_result ?? {}).passed == true) : true',
                },
                {
                    "id": _new_uuid(),
                    # 综合三项标志：任一为 true 则高风险
                    "key": "isHighRiskInvoice",
                    "value": '(($.isHighRiskByRule ?? false) or ($.isRecentlyRegistered ?? false) or ((llm_status ?? "error") == "success" ? ((llm_result ?? {}).passed == false) : false)) ? "true" : "false"',
                },
            ],
            "passThrough": True,
            "inputField": None,
            "outputPath": None,
            "executionMode": "single",
        },
        "id": "ent-fraud-postprocess",
        "name": "虚开发票预警后处理",
        "position": {"x": 200, "y": 1600},
    }


def _build_fraud_check_node() -> dict:
    """规则5：虚开发票预警检查 W31（弱控 WARNING，转财务审核）。"""
    node_id = "ent-fraud-check"
    rules = [
        _std_rule_row(
            input_value='"false"',
            reason_code="W31",
            distinguish_result="PASS",
            audit_content="检查使用的发票销货方是否为高风险发票（虚开发票预警）",
            audit_type="general-rules",
            message='""',
            policies_index='""',
            suggestion='""',
        ),
        _std_rule_row(
            input_value='"true"',
            reason_code="W31",
            distinguish_result="WARNING",
            audit_content="检查使用的发票销货方是否为高风险发票（虚开发票预警）",
            audit_type="general-rules",
            message='"发票号【"+(invoiceNo??"")+"】✗ 销货方【"+(salerName??"")+"】符合高风险发票特征，存在虚开发票风险，✓ 需排除注册时间短、注册地址异常、开票税率异常的高风险销货方发票"',
            policies_index='"《锐捷网络员工费用管理与报销制度》\\n6. 监督管理（虚开发票加征15%企业所得税款及滞纳金，扣减预算，按《纪律管理办法2026版》处理）"',
            suggestion='"【业务确认】请确认发票业务真实性，补充提供业务合同、消费明细、付款凭证等佐证材料；如为真实合规票据，请在单据备注栏说明情况，财务将进行人工重点复核"',
        ),
    ]
    return _make_decision_table(
        node_id=node_id,
        name="虚开发票预警检查",
        input_field="isHighRiskInvoice",
        input_name="是否高风险发票",
        rules=rules,
        output_path="fraud_check_result",
        position={"x": 675, "y": 1600},
    )


# ---------------------------------------------------------------------------
# 主构建逻辑
# ---------------------------------------------------------------------------

def build_entertainment_graph() -> dict:
    """构建业务招待费稽核工作流 graph。"""
    # 旧版通讯费源图在部分部署包中未随仓库分发，使用同结构的最新源图兜底。
    source_graph = SRC_GRAPH
    if not source_graph.exists():
        fallback_graph = REPO_ROOT / "graph-latest-0727-1900.json"
        if not fallback_graph.exists():
            raise FileNotFoundError(
                f"source graph not found: {SRC_GRAPH}; fallback also missing: {fallback_graph}"
            )
        source_graph = fallback_graph
    with open(source_graph, "r", encoding="utf-8") as f:
        src = json.load(f)

    nodes = src["nodes"]
    edges = src["edges"]

    # --- Phase 1: 删除通讯费特有节点 ---
    nodes = [n for n in nodes if n["id"] not in NODE_IDS_TO_DELETE]
    # 删除涉及已删除节点的边
    deleted_set = NODE_IDS_TO_DELETE
    edges = [
        e for e in edges
        if e["sourceId"] not in deleted_set and e["targetId"] not in deleted_set
    ]

    # --- Phase 1: 改造数据校验预处理 expressionNode ---
    for node in nodes:
        if node["id"] == "c67dcb33-2750-4a43-8af7-8346612c04a9":
            node["content"]["expressions"] = ENTERTAINMENT_PREPROCESS_EXPRESSIONS
            break

    # --- Phase 1: 改造票据类型检查 E35 的 message/suggestion/policiesIndex ---
    # 业务招待费禁止：增值税专用发票、增值税电子专用发票、电子发票（增值税专用发票）、海关专用缴款书
    for node in nodes:
        if node["id"] == "invoice_type_check":
            rules = node["content"]["rules"]
            # False 规则（不通过）的 message 和 suggestion
            for rule in rules:
                if rule.get(INPUT_FIELD_ID) == "False":
                    rule["509fd9ba-3996-4e4a-9021-df6513ed6807"] = (
                        '"票据\\""+(invoiceNo??"")+"\\",票据种类为\\""+(invoiceType??"")+\\"\\",'
                        '不属于业务招待费报销允许的票种范围，不允许使用增值税专用发票、'
                        '增值税电子专用发票、电子发票（增值税专用发票）、海关专用缴款书报销"'
                    )
                    rule["a1b2c3d4-0000-0000-0000-regulation0"] = (
                        '"《锐捷网络员工费用管理与报销制度》\\n5.2票据使用规范"'
                    )
                    rule["a1b2c3d4-0000-0000-0000-suggestion0"] = (
                        '"【删除发票】删除本票据，重新上传业务招待费允许范围内的合规发票"'
                    )
            break

    # --- Phase 2: 新增业务招待费特有规则节点 ---
    nodes.append(_build_company_header_check_node())   # E01
    nodes.append(_build_tax_number_check_node())      # E02
    nodes.append(_build_self_expense_check_node())      # E15
    nodes.append(_build_gift_count_check_node())        # W33
    nodes.append(_build_invoice_number_check_node())    # W34

    # --- Phase 3: LLM 内容合规节点改造 ---
    # 删除旧的充值卡检查prompt、调用llm、后处理、充值卡检查节点
    old_llm_chain_ids = {
        "33a6a837-0964-4c10-b451-e277138820aa",  # 充值卡检查prompt
        "109af3d5-6dec-4cd0-a09e-de43b026c491",  # 调用llm (充值卡)
        "ee403929-4b10-40ed-98a5-1b7cef33387f",  # 发票内容检查后处理
        "b49e3359-766d-4ff3-9115-423eb06cebe7",  # 发票充值卡检查
    }
    nodes = [n for n in nodes if n["id"] not in old_llm_chain_ids]
    edges = [
        e for e in edges
        if e["sourceId"] not in old_llm_chain_ids and e["targetId"] not in old_llm_chain_ids
    ]

    # 新增招待费内容合规 LLM 链
    nodes.append(_build_content_compliance_prompt_node())       # prompt
    nodes.append(_build_content_compliance_llm_node())          # 调用llm
    nodes.append(_build_content_compliance_postprocess_node())  # 后处理
    nodes.append(_build_content_compliance_check_node())         # 决策表
    nodes.append(_build_recharge_card_check_node())              # E17 独立决策表

    # --- Phase 4: 虚开发票预警节点 ---
    nodes.append(_build_fraud_preprocess_node())
    nodes.append(_build_fraud_address_prompt_node())
    nodes.append(_build_fraud_address_llm_node())
    nodes.append(_build_fraud_postprocess_node())
    nodes.append(_build_fraud_check_node())

    # --- Phase 5: 重建边连接 ---
    REQUEST_ID = "9948bfb0-d9fb-416d-b9a2-b22a875094f0"
    RESPONSE_ID = "e109e75a-d107-4fd0-a8b3-e3dae7fad15b"
    DATA_PREPROCESS_ID = "c67dcb33-2750-4a43-8af7-8346612c04a9"
    INVOICE_PREPROCESS_ID = "f10e41a3-a2b4-479f-8fdb-4d3da38d777d"

    # 新增/恢复节点 ID
    COMPANY_HEADER_CHECK = "d3046965-dbaf-41cd-ba93-0d957fe67ec8"
    TAX_NUMBER_CHECK = "d13f7062-96e4-4d74-a552-dfcc60d98ff4"
    SELF_EXPENSE_CHECK = "ent-self-expense-check"
    GIFT_COUNT_CHECK = "ent-gift-count-check"
    INVOICE_NUMBER_CHECK = "ent-invoice-number-check"
    CONTENT_PROMPT = "ent-content-compliance-prompt"
    CONTENT_LLM = "ent-content-compliance-llm"
    CONTENT_POSTPROCESS = "ent-content-compliance-postprocess"
    CONTENT_CHECK = "ent-content-compliance-check"
    RECHARGE_CARD_CHECK = "ent-recharge-card-check"
    FRAUD_PREPROCESS = "ent-fraud-preprocess"
    FRAUD_PROMPT = "ent-fraud-address-prompt"
    FRAUD_LLM = "ent-fraud-address-llm"
    FRAUD_POSTPROCESS = "ent-fraud-postprocess"
    FRAUD_CHECK = "ent-fraud-check"

    # 保留的通用节点 ID（来自通讯费）
    INVOICE_TYPE_CHECK = "invoice_type_check"
    AMOUNT_CHECK = "dcc29290-4d5e-4eb9-8e77-eee31820529a"
    BLACKLIST_CHECK = "0fdb068f-749a-4c67-a89f-54adb83084d6"
    WRITEOFF_CHECK = "2ea2f963-44fc-4130-9632-af048b76d0b1"
    YEAR_CHECK = "a4cf2236-f43d-44a3-86ef-b90fd2ce0d33"
    INVOICE_PROBLEM_CHECK = "3e42eda0-70e0-4df2-8abc-6f2dbe4f2535"

    def _edge(src: str, tgt: str) -> dict:
        return {"id": _new_uuid(), "sourceId": src, "targetId": tgt, "type": "edge"}

    # 构建全新的边集合（完全重建，确保连通性正确）
    new_edges = []

    # 分支1: request → 数据校验预处理 → 7个决策表 → response
    new_edges.append(_edge(REQUEST_ID, DATA_PREPROCESS_ID))
    for check_id in [
        COMPANY_HEADER_CHECK,
        TAX_NUMBER_CHECK,
        INVOICE_TYPE_CHECK,
        AMOUNT_CHECK,
        BLACKLIST_CHECK,
        WRITEOFF_CHECK,
        YEAR_CHECK,
        SELF_EXPENSE_CHECK,
        GIFT_COUNT_CHECK,
        INVOICE_NUMBER_CHECK,
    ]:
        new_edges.append(_edge(DATA_PREPROCESS_ID, check_id))
        new_edges.append(_edge(check_id, RESPONSE_ID))

    # 分支2: request → 发票检查预处理 → 检查发票问题 → response
    new_edges.append(_edge(REQUEST_ID, INVOICE_PREPROCESS_ID))
    new_edges.append(_edge(INVOICE_PREPROCESS_ID, INVOICE_PROBLEM_CHECK))
    new_edges.append(_edge(INVOICE_PROBLEM_CHECK, RESPONSE_ID))

    # 分支3: request → 招待费内容合规prompt → 调用llm → 后处理 → 招待费内容合规检查 → response。
    # 发票内容 goodsName 已在数据准备阶段生成，流程图不再保留拼接预处理节点。
    new_edges.append(_edge(REQUEST_ID, CONTENT_PROMPT))
    new_edges.append(_edge(CONTENT_PROMPT, CONTENT_LLM))
    new_edges.append(_edge(CONTENT_LLM, CONTENT_POSTPROCESS))
    # request 也需要连到后处理（提供原始数据如 invoiceNo, goodsName）
    new_edges.append(_edge(REQUEST_ID, CONTENT_POSTPROCESS))
    new_edges.append(_edge(CONTENT_POSTPROCESS, CONTENT_CHECK))
    # 决策表直接读取 request 中的数据准备结果 goodsName。
    new_edges.append(_edge(REQUEST_ID, CONTENT_CHECK))
    new_edges.append(_edge(CONTENT_CHECK, RESPONSE_ID))
    new_edges.append(_edge(CONTENT_POSTPROCESS, RECHARGE_CARD_CHECK))
    new_edges.append(_edge(REQUEST_ID, RECHARGE_CARD_CHECK))
    new_edges.append(_edge(RECHARGE_CARD_CHECK, RESPONSE_ID))

    # 分支4: request → 虚开发票预警预处理 → 地址检查prompt → 调用llm → 后处理 → 预警检查 → response
    new_edges.append(_edge(REQUEST_ID, FRAUD_PREPROCESS))
    new_edges.append(_edge(FRAUD_PREPROCESS, FRAUD_PROMPT))
    new_edges.append(_edge(FRAUD_PROMPT, FRAUD_LLM))
    new_edges.append(_edge(FRAUD_LLM, FRAUD_POSTPROCESS))
    # request 也需要连到后处理（提供原始数据如 salerName, isHighRiskByRule 等）
    new_edges.append(_edge(REQUEST_ID, FRAUD_POSTPROCESS))
    new_edges.append(_edge(FRAUD_POSTPROCESS, FRAUD_CHECK))
    new_edges.append(_edge(FRAUD_CHECK, RESPONSE_ID))

    edges = new_edges

    graph = {
        "contentType": "application/vnd.gorules.decision",
        "nodes": nodes,
        "edges": edges,
    }
    return graph


def main() -> None:
    graph = build_entertainment_graph()
    with open(DST_GRAPH, "w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False, indent=2)
    print(f"✓ 已生成业务招待费稽核工作流: {DST_GRAPH}")
    print(f"  节点数: {len(graph['nodes'])}")
    print(f"  边数: {len(graph['edges'])}")

    # 统计节点类型
    type_counts: dict[str, int] = {}
    for n in graph["nodes"]:
        t = n["type"]
        type_counts[t] = type_counts.get(t, 0) + 1
    print("  节点类型分布:")
    for t, c in type_counts.items():
        print(f"    {t}: {c}")


if __name__ == "__main__":
    main()
