#!/usr/bin/env python3
"""
核销单联调脚本：在线跑通 process_receipt 完整链路

用法:
    python integration_test.py <receipt_code>
    python integration_test.py rjw260327000006
"""

import json
import os
import sys
from pathlib import Path

# 先加载 .env 环境变量
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

# 把项目根目录加到路径
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from expense_audit_orchestrator.audit_client import (
    DEFAULT_AUDIT_SERVICE_URL,
    fetch_audit_info,
    fetch_audit_invoice_file_info,
    fetch_audit_invoice_files,
    fetch_company_blacklist,
    fetch_company_list,
    fetch_expense_invoice_types,
    fetch_invoice_info,
)
from expense_audit_orchestrator.core import (
    ReceiptDataPreparer,
    call_ocr_service,
    get_invoice_file_from_server,
)
from expense_audit_orchestrator.kingdee_ocr import create_kingdee_ocr_provider_from_env


# ============ 配置 ============
# 线上核销单服务地址
ONLINE_AUDIT_SERVICE_URL = "https://service-uate-gw.ruijie.com.cn"

# OCR 服务配置（从环境变量读取）
# 需要设置: KINGDEE_OCR_URL, KINGDEE_OCR_APP_KEY, KINGDEE_OCR_APP_SECRET 等


def test_fetch_audit_info(receipt_code: str, service_url: str) -> dict:
    """步骤1: 获取核销单基本信息"""
    print(f"\n{'='*60}")
    print(f"步骤1: 获取核销单基本信息")
    print(f"{'='*60}")
    try:
        audit_info = fetch_audit_info(receipt_code, service_url=service_url, timeout=10.0)
        print(f"✅ 获取成功，返回字段: {list(audit_info.keys())}")
        print(f"   instanceCode: {audit_info.get('instanceCode')}")
        print(f"   eiCode: {audit_info.get('eiCode')}")
        return audit_info
    except Exception as e:
        print(f"❌ 失败: {e}")
        raise


def test_fetch_audit_invoice_files(instance_code: str, service_url: str) -> list:
    """步骤2: 获取核销单关联的发票文件列表"""
    print(f"\n{'='*60}")
    print(f"步骤2: 获取发票文件列表 (aType=0)")
    print(f"{'='*60}")
    try:
        files = fetch_audit_invoice_files(instance_code, a_type=0, service_url=service_url, timeout=10.0)
        print(f"✅ 获取成功，共 {len(files)} 个文件")
        for i, f in enumerate(files):
            print(f"   [{i+1}] fid={f.get('fid')}, fileName={f.get('fileName')}, fileType={f.get('fileType')}")
        return files
    except Exception as e:
        print(f"❌ 失败: {e}")
        raise


def test_fetch_audit_invoice_file_info(fid: str, service_url: str) -> dict:
    """步骤3: 获取单个文件详情"""
    print(f"\n{'='*60}")
    print(f"步骤3: 获取文件详情 (fid={fid})")
    print(f"{'='*60}")
    try:
        info = fetch_audit_invoice_file_info(fid, service_url=service_url, timeout=10.0)
        # 返回可能是 list 或 dict
        if isinstance(info, list) and len(info) > 0:
            info = info[0]
        print(f"✅ 获取成功")
        print(f"   返回字段: {list(info.keys())}")

        # 检查关键字段
        has_base64 = bool(info.get("fileBase64") or info.get("base64"))
        has_url = bool(info.get("fileUrl") or info.get("filePath") or info.get("url"))
        print(f"   有 fileBase64: {has_base64}")
        print(f"   有 fileUrl: {has_url}")

        if has_base64:
            b64 = info.get("fileBase64") or info.get("base64") or ""
            print(f"   base64 长度: {len(b64)} 字符")
        if has_url:
            url = info.get("fileUrl") or info.get("filePath") or info.get("url") or ""
            print(f"   fileUrl: {url[:100]}..." if len(str(url)) > 100 else f"   fileUrl: {url}")

        return info
    except Exception as e:
        print(f"❌ 失败: {e}")
        raise


def test_ocr(file_path: str, file_name: str | None = None, audit_info: dict | None = None, company_list: list | None = None) -> dict:
    """步骤4: 调用金蝶 OCR"""
    print(f"\n{'='*60}")
    print(f"步骤4: 调用金蝶 OCR")
    print(f"{'='*60}")
    print(f"   file_path: {file_path[:80]}..." if len(file_path) > 80 else f"   file_path: {file_path}")
    print(f"   file_name: {file_name}")

    try:
        ocr_provider = create_kingdee_ocr_provider_from_env()
        kwargs: dict[str, Any] = {"file_name": file_name}
        if audit_info is not None:
            kwargs["audit_info"] = audit_info
        if company_list is not None:
            kwargs["company_list"] = company_list
        ocr_data = ocr_provider(file_path, **kwargs)
        print(f"✅ OCR 成功")
        print(f"   返回字段: {list(ocr_data.keys())}")
        if "chequeNo" in ocr_data:
            print(f"   chequeNo: {ocr_data.get('chequeNo')}")
        return ocr_data
    except Exception as e:
        print(f"❌ OCR 失败: {e}")
        raise


def _safe_fetch(func, *args, fallback=None, **kwargs):
    """安全调用接口，失败时返回 fallback 值"""
    try:
        return func(*args, **kwargs)
    except Exception as e:
        print(f"   ⚠️ {func.__name__} 调用失败: {e}，使用 fallback")
        return fallback if fallback is not None else []


def test_prepare_receipt_context(receipt_code: str, service_url: str) -> dict:
    """步骤5: 完整数据准备"""
    print(f"\n{'='*60}")
    print(f"步骤5: 完整数据准备 (ReceiptDataPreparer)")
    print(f"{'='*60}")

    ocr_provider = create_kingdee_ocr_provider_from_env()

    preparer = ReceiptDataPreparer(
        audit_info_provider=lambda code: fetch_audit_info(code, service_url=service_url, timeout=10.0),
        company_blacklist_provider=lambda: _safe_fetch(fetch_company_blacklist, service_url=service_url, timeout=10.0, fallback=[]),
        company_list_provider=lambda: _safe_fetch(fetch_company_list, service_url=service_url, timeout=10.0, fallback=[]),
        expense_invoice_types_provider=lambda ei: _safe_fetch(fetch_expense_invoice_types, ei, service_url=service_url, timeout=10.0, fallback=[]),
        invoice_info_provider=lambda *args: fetch_invoice_info(*args, service_url=service_url, timeout=10.0),
        audit_invoice_files_provider=lambda ic, at: fetch_audit_invoice_files(ic, at, service_url=service_url, timeout=10.0),
        audit_invoice_file_info_provider=lambda fid: fetch_audit_invoice_file_info(fid, service_url=service_url, timeout=10.0),
        ocr_provider=ocr_provider,
    )

    try:
        context = preparer.prepare_receipt_context(receipt_code)
        print(f"✅ 数据准备成功")
        print(f"   receiptCode: {context.get('receiptCode')}")
        print(f"   invoiceFiles 数量: {len(context.get('invoiceFiles', []))}")
        for i, f in enumerate(context.get('invoiceFiles', [])):
            print(f"   [{i+1}] invoiceKey={f.get('invoiceKey')}, filePath={f.get('filePath', '')[:60]}...")
        return context
    except Exception as e:
        print(f"❌ 失败: {e}")
        raise


def test_full_pipeline(receipt_code: str, service_url: str, output_dir: Path | None = None) -> dict:
    """步骤6: 完整流程（数据准备 + OCR + 规则输入构建）"""
    print(f"\n{'='*60}")
    print(f"步骤6: 完整流程测试")
    print(f"{'='*60}")

    ocr_provider = create_kingdee_ocr_provider_from_env()

    preparer = ReceiptDataPreparer(
        audit_info_provider=lambda code: fetch_audit_info(code, service_url=service_url, timeout=10.0),
        company_blacklist_provider=lambda: _safe_fetch(fetch_company_blacklist, service_url=service_url, timeout=10.0, fallback=[]),
        company_list_provider=lambda: _safe_fetch(fetch_company_list, service_url=service_url, timeout=10.0, fallback=[]),
        expense_invoice_types_provider=lambda ei: _safe_fetch(fetch_expense_invoice_types, ei, service_url=service_url, timeout=10.0, fallback=[]),
        invoice_info_provider=lambda *args: fetch_invoice_info(*args, service_url=service_url, timeout=10.0),
        audit_invoice_files_provider=lambda ic, at: fetch_audit_invoice_files(ic, at, service_url=service_url, timeout=10.0),
        audit_invoice_file_info_provider=lambda fid: fetch_audit_invoice_file_info(fid, service_url=service_url, timeout=10.0),
        ocr_provider=ocr_provider,
    )

    # 确定输出目录
    if output_dir is None:
        output_dir = Path(__file__).resolve().parent / "output" / receipt_code
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"📁 输出目录: {output_dir}")

    try:
        # 先准备上下文
        context = preparer.prepare_receipt_context(receipt_code)
        invoice_files = context.get("invoiceFiles", [])

        if not invoice_files:
            print("⚠️ 没有发票文件，流程结束")
            return {}

        # 逐张处理每张发票
        results = []
        for i, invoice_file in enumerate(invoice_files):
            print(f"\n--- 处理第 {i+1}/{len(invoice_files)} 张发票 ---")
            try:
                prepared = preparer.prepare_invoice_input(
                    receipt_code,
                    invoice_file,
                    context,
                    include_current_invoice_metadata=True,
                )

                # 保存 prepared_input 到 JSON 文件
                invoice_key = invoice_file.get("invoiceKey", f"invoice_{i+1}")
                output_file = output_dir / f"{invoice_key}_prepared_input.json"
                with open(output_file, "w", encoding="utf-8") as f:
                    json.dump(prepared, f, ensure_ascii=False, indent=2)
                print(f"   📝 已保存: {output_file}")

                results.append({
                    "index": i + 1,
                    "invoiceKey": invoice_key,
                    "status": "success",
                    "output_file": str(output_file),
                    "ocr_keys": list(prepared.get("ocrData", {}).keys()) if "ocrData" in prepared else list(prepared.keys())[:10],
                })
                print(f"✅ 第 {i+1} 张发票处理成功")
            except Exception as e:
                results.append({
                    "index": i + 1,
                    "invoiceKey": invoice_file.get("invoiceKey"),
                    "status": "error",
                    "error": str(e),
                })
                print(f"❌ 第 {i+1} 张发票处理失败: {e}")

        print(f"\n{'='*60}")
        print(f"处理汇总: {len([r for r in results if r['status'] == 'success'])}/{len(results)} 成功")
        print(f"{'='*60}")
        for r in results:
            status_icon = "✅" if r["status"] == "success" else "❌"
            print(f"   {status_icon} [{r['index']}] {r['invoiceKey']}: {r['status']}")
            if r["status"] == "success":
                print(f"      📄 {r['output_file']}")

        return {"results": results, "context": context}
    except Exception as e:
        print(f"❌ 流程失败: {e}")
        raise


def main():
    if len(sys.argv) < 2:
        receipt_code = "rjw260327000006"
        print(f"未提供核销单号，使用默认: {receipt_code}")
    else:
        receipt_code = sys.argv[1]

    print(f"\n{'#'*60}")
    print(f"# 核销单联调测试")
    print(f"# 核销单号: {receipt_code}")
    print(f"# 服务地址: {ONLINE_AUDIT_SERVICE_URL}")
    print(f"#{'#'*59}")

    # 检查环境变量
    print(f"\n环境变量检查:")
    env_vars = ["KINGDEE_OCR_URL", "KINGDEE_OCR_APP_KEY", "KINGDEE_OCR_APP_SECRET"]
    for var in env_vars:
        val = os.getenv(var)
        status = "✅ 已设置" if val else "❌ 未设置"
        print(f"   {var}: {status}")

    try:
        # 步骤1: 获取核销单信息
        audit_info = test_fetch_audit_info(receipt_code, ONLINE_AUDIT_SERVICE_URL)
        instance_code = audit_info.get("instanceCode") or receipt_code

        # 步骤2: 获取发票文件列表
        invoice_files = test_fetch_audit_invoice_files(instance_code, ONLINE_AUDIT_SERVICE_URL)

        if not invoice_files:
            print("\n⚠️ 该核销单没有关联的发票文件，流程结束")
            return

        # 步骤3: 获取第一个文件的详情（测试文件链路）
        first_fid = invoice_files[0].get("fid")
        if first_fid:
            file_info = test_fetch_audit_invoice_file_info(first_fid, ONLINE_AUDIT_SERVICE_URL)

            # 测试 OCR（单张）- 尝试获取 company_list，如果失败则跳过单张 OCR 测试
            try:
                from expense_audit_orchestrator.core import _resolve_invoice_file_path, _resolve_invoice_file_name
                file_path = _resolve_invoice_file_path(file_info)
                file_name = _resolve_invoice_file_name({"auditInvoiceFile": invoice_files[0], "auditInvoiceFileInfo": file_info})
                # 尝试获取 company_list 用于 OCR
                try:
                    company_list = fetch_company_list(service_url=ONLINE_AUDIT_SERVICE_URL, timeout=10.0)
                except Exception as e:
                    print(f"   ⚠️ 获取 company_list 失败（可能接口不存在），跳过单张 OCR 测试: {e}")
                    company_list = []
                if company_list:
                    test_ocr(file_path, file_name, audit_info=audit_info, company_list=company_list)
                else:
                    print(f"   ⚠️ 未获取到 company_list，跳过单张 OCR 测试，继续完整流程")
            except Exception as e:
                print(f"   ⚠️ 单张 OCR 测试失败，继续完整流程: {e}")

        # 步骤5: 完整数据准备
        context = test_prepare_receipt_context(receipt_code, ONLINE_AUDIT_SERVICE_URL)

        # 步骤6: 完整流程
        result = test_full_pipeline(receipt_code, ONLINE_AUDIT_SERVICE_URL)

        print(f"\n{'#'*60}")
        print(f"# 联调完成")
        print(f"#{'#'*59}")

    except Exception as e:
        print(f"\n{'#'*60}")
        print(f"# 联调失败: {e}")
        print(f"#{'#'*59}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
