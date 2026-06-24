#!/usr/bin/env python3
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


HOST = "0.0.0.0"
PORT = 8080


def build_audit_info(instance_code: str) -> dict:
    """Build mock audit record data by instance code."""
    return {
        "instanceCode": instance_code,
        "eiCode": "FEE-PROJ-1001",
        "eiName": "差旅费",
        "submitTime": "2026-06-08 10:20:30",
        "verifiStaffNo": "A10293",
        "verifiUserId": "u_98321",
        "verifiUserCompanyName": "锐捷网络股份有限公司",
        "applyAmount": 1234.56,
    }


def build_company_blacklist() -> list[dict]:
    """Build mock company blacklist entries."""
    return [
        {
            "id": 10001,
            "code": "COMPANY_BLACKLIST",
            "value": "福建示例供应商有限公司",
            "createTime": "2026-06-01 09:00:00",
            "modifyTime": "2026-06-08 09:00:00",
        }
    ]


def build_company_list() -> list[dict]:
    """Build mock finance company entries."""
    return [
        {
            "cCode": "RJCW01",
            "cName": "锐捷网络股份有限公司",
            "cNameEn": "Ruijie Networks Co., Ltd.",
            "cShortName": "锐捷网络",
            "companyTax": "913500007549617646"

        },
        {
            "cCode": "RJCW02",
            "cName": "福建星网智慧科技有限公司",
            "cNameEn": "Fujian Star-Net Smart Technology Co., Ltd.",
            "cShortName": "星网智慧",
            "companyTax": "913500"

        },
    ]


def build_expense_invoice_types(ei_code: str) -> list[dict]:
    """Build mock allowed invoice types for an expense item."""
    allowed_types = {
        "FEE-PROJ-1001": [
            {
                "id": 20001,
                "eiCode": "FEE-PROJ-1001",
                "invoiceType": "26",
                "manufacturerBillCode": "ELEC_NORMAL",
                "modifyTime": "2026-06-08 10:00:00",
            },
            {
                "id": 20002,
                "eiCode": "FEE-PROJ-1001",
                "invoiceType": "2",
                "manufacturerBillCode": "VAT_NORMAL",
                "modifyTime": "2026-06-08 10:00:00",
            },
        ],
    }
    return allowed_types.get(
        ei_code,
        [
            {
                "id": 20999,
                "eiCode": ei_code,
                "invoiceType": "26",
                "manufacturerBillCode": "ELEC_NORMAL",
                "modifyTime": "2026-06-08 10:00:00",
            }
        ],
    )


def build_invoice_info(
    cheque_no: str,
    instance_code: str,
    accounting_code: str | None = None,
) -> list[dict]:
    """Build mock invoice occupation records."""
    records = [
        {
            "aiiid": 30001,
            "miInstanceCode": instance_code,
            "miApplyUserId": "u_10086",
            "miApplyUserName": "王丽",
            "createTime": "2026-06-08 11:20:00",
        }
    ]
    if cheque_no == "123123":
        records.append(
            {
                "aiiid": 30002,
                "miInstanceCode": instance_code,
                "miApplyUserId": "u_10010",
                "miApplyUserName": "陈峰",
                "createTime": "2026-06-08 11:45:00",
            }
        )

    if accounting_code:
        return records

    return records[:1]


def build_audit_invoice_files(instance_code: str, a_type: int = 0) -> list[dict]:
    """Build mock audit invoice file records."""
    records = [
        {
            "afiid": "AFID-001",
            "miInstanceCode": instance_code,
            "fid": "FID-001",
            "createTime": "2026-06-08 11:20:00",
            "type": 0,
            "fileName": f"{instance_code}-origin.pdf",
            "aiid": "AIID-001",
        },
        {
            "afiid": "AFID-002",
            "miInstanceCode": instance_code,
            "fid": "FID-002",
            "createTime": "2026-06-08 11:25:00",
            "type": 1,
            "fileName": f"{instance_code}-clip.png",
            "aiid": "AIID-001",
        },
    ]
    return [record for record in records if record["type"] == a_type]


def build_audit_invoice_file_info(fid: str) -> list[dict]:
    """Build mock audit invoice file detail records."""
    return [
        {
            "fileUrl": f"https://files.example/{fid}.pdf",
            "fileBase64": f"BASE64-{fid}",
            "fid": fid,
        }
    ]


class AuditRequestHandler(BaseHTTPRequestHandler):
    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, status_code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{now}] {self.address_string()} - {format % args}")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/health":
            self._send_json(200, {"ok": True, "service": "expense-audit-mock"})
            return

        if path == "/audit/companyblacklist":
            self._send_json(
                200,
                {
                    "code": 0,
                    "message": "success",
                    "data": build_company_blacklist(),
                },
            )
            return

        if path in {"/audit/companylist", "/audit/company-list"}:
            ei_code = query.get("eiCode", [""])[0].strip()
            if ei_code:
                self._send_json(
                    200,
                    {
                        "code": 0,
                        "message": "success",
                        "data": build_expense_invoice_types(ei_code),
                    },
                )
                return

            self._send_json(
                200,
                {
                    "code": 0,
                    "message": "success",
                    "data": build_company_list(),
                },
            )
            return

        if path == "/audit/invoiceInfo":
            cheque_no = query.get("chequeNo", [""])[0].strip()
            instance_code = query.get("instanceCode", [""])[0].strip()
            accounting_code = query.get("accountingCode", [""])[0].strip() or None

            if not cheque_no or not instance_code:
                self._send_json(
                    400,
                    {
                        "code": 400,
                        "message": "chequeNo 和 instanceCode 不能为空",
                    },
                )
                return

            self._send_json(
                200,
                {
                    "code": 0,
                    "message": "success",
                    "data": build_invoice_info(cheque_no, instance_code, accounting_code),
                },
            )
            return

        if path == "/audit/invoice-file":
            instance_code = query.get("instanceCode", [""])[0].strip()
            raw_a_type = query.get("aType", ["0"])[0].strip() or "0"

            if not instance_code:
                self._send_json(
                    400,
                    {
                        "code": 400,
                        "message": "instanceCode 不能为空",
                    },
                )
                return

            try:
                a_type = int(raw_a_type)
            except ValueError:
                self._send_json(
                    400,
                    {
                        "code": 400,
                        "message": "aType 必须是整数",
                    },
                )
                return

            self._send_json(
                200,
                {
                    "code": 0,
                    "message": "success",
                    "data": build_audit_invoice_files(instance_code, a_type),
                },
            )
            return

        invoice_file_info_prefix = "/audit/invoice-file-info/"
        if path.startswith(invoice_file_info_prefix):
            fid = path[len(invoice_file_info_prefix):].strip()
            if not fid:
                self._send_json(
                    400,
                    {
                        "code": 400,
                        "message": "fid 不能为空",
                    },
                )
                return

            self._send_json(
                200,
                {
                    "code": 0,
                    "message": "success",
                    "data": build_audit_invoice_file_info(fid),
                },
            )
            return

        companylist_prefixes = ["/audit/companylist/", "/audit/company-list/"]
        for companylist_prefix in companylist_prefixes:
            if not path.startswith(companylist_prefix):
                continue
            ei_code = path[len(companylist_prefix):].strip()
            if not ei_code:
                self._send_json(
                    400,
                    {
                        "code": 400,
                        "message": "eiCode 不能为空",
                    },
                )
                return

            self._send_json(
                200,
                {
                    "code": 0,
                    "message": "success",
                    "data": build_expense_invoice_types(ei_code),
                },
            )
            return

        prefix = "/audit/info/"
        if path.startswith(prefix):
            instance_code = path[len(prefix):].strip()
            if not instance_code:
                self._send_json(
                    400,
                    {
                        "code": 400,
                        "message": "instanceCode 不能为空",
                    },
                )
                return

            self._send_json(
                200,
                {
                    "code": 0,
                    "message": "success",
                    "data": build_audit_info(instance_code),
                },
            )
            return

        self._send_json(
            404,
            {
                "code": 404,
                "message": "not found",
            },
        )


def run_server() -> None:
    server = ThreadingHTTPServer((HOST, PORT), AuditRequestHandler)
    print(f"Mock audit service is running at http://{HOST}:{PORT}")
    print("Try: GET /audit/info/{instanceCode}")
    server.serve_forever()


if __name__ == "__main__":
    run_server()