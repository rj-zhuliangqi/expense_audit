#!/usr/bin/env python3

import argparse
import json
import os
import sys
from collections.abc import Sequence

from expense_audit_orchestrator import audit_client


DEFAULT_REAL_AUDIT_SERVICE_URL = "https://service-uate-gw.ruijie.com.cn/api/audit-service"


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="调用真实核销单服务，查询稽核发票文件列表")
    parser.add_argument("instance_code", help="核销单号 instanceCode")
    parser.add_argument(
        "--service-url",
        default=os.getenv("AUDIT_SERVICE_URL", DEFAULT_REAL_AUDIT_SERVICE_URL),
        help="核销单服务地址；未传时优先读取 AUDIT_SERVICE_URL，否则使用默认真实网关地址",
    )
    parser.add_argument("--a-type", type=int, default=0, help="发票文件类型，默认 0")
    parser.add_argument("--timeout", type=float, default=20.0, help="请求超时时间，默认 20 秒")
    parser.add_argument(
        "--with-file-info",
        action="store_true",
        help="继续按 fid 查询文件详情，并附加到输出 JSON 中",
    )
    parser.add_argument("--indent", type=int, default=2, help="输出 JSON 缩进，默认 2")
    return parser


def _collect_file_info_by_fid(
    invoice_files: list[dict[str, object]],
    *,
    service_url: str,
    timeout: float,
) -> dict[str, list[dict[str, object]]]:
    file_info_by_fid: dict[str, list[dict[str, object]]] = {}
    seen_fids: set[str] = set()

    for invoice_file in invoice_files:
        fid = str(invoice_file.get("fid") or "").strip()
        if not fid or fid in seen_fids:
            continue

        seen_fids.add(fid)
        file_info_by_fid[fid] = audit_client.fetch_audit_invoice_file_info(
            fid,
            service_url=service_url,
            timeout=timeout,
        )

    return file_info_by_fid


def main_cli(argv: Sequence[str] | None = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)

    try:
        invoice_files = audit_client.fetch_audit_invoice_files(
            args.instance_code,
            a_type=args.a_type,
            service_url=args.service_url,
            timeout=args.timeout,
        )
        payload: dict[str, object] = {
            "instanceCode": args.instance_code,
            "serviceUrl": args.service_url,
            "aType": args.a_type,
            "invoiceFiles": invoice_files,
        }
        if args.with_file_info:
            payload["invoiceFileInfo"] = _collect_file_info_by_fid(
                invoice_files,
                service_url=args.service_url,
                timeout=args.timeout,
            )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(payload, ensure_ascii=False, indent=args.indent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())