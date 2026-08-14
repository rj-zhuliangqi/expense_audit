import unittest
from unittest.mock import patch

from expense_audit_orchestrator.profiles.travel.client import TravelApiClient
from expense_audit_orchestrator.profiles.travel.data import (
    NOT_READY_MESSAGE,
    normalize_travel_data,
    travel_invoice_enricher,
    travel_receipt_enricher,
)


class TravelApiClientTests(unittest.TestCase):
    def test_fetch_all_calls_all_travel_endpoints(self) -> None:
        calls = []

        def request(path, description, timeout):
            calls.append(path)
            return []

        result = TravelApiClient(request_list=request).fetch_all("INS/001")
        self.assertEqual(len(calls), 10)
        self.assertTrue(all("INS%2F001" in path for path in calls))
        self.assertTrue(all(value["status"] == "READY" for value in result["sourceStatus"].values()))

    def test_other_expenses_short_method_alias_uses_expected_endpoint(self) -> None:
        calls = []

        def request(path, description, timeout):
            calls.append(path)
            return []

        TravelApiClient(request_list=request).fetch_other_expenses("INS-1")
        self.assertEqual(calls, ["/api/audit-service/audit/travel-other-expenses/INS-1"])

    def test_one_endpoint_failure_is_fail_open(self) -> None:
        def request(path, description, timeout):
            if path.endswith("/train-tickets/INS-1"):
                raise RuntimeError("connection refused")
            return [{"id": "ok"}]

        result = TravelApiClient(request_list=request).fetch_all("INS-1")
        self.assertEqual(result["trainTickets"], [])
        self.assertEqual(result["sourceStatus"]["trainTickets"]["status"], "NOT_READY")
        self.assertIn("按通过处理", result["sourceStatus"]["trainTickets"]["message"])
        self.assertEqual(result["journeys"], [{"id": "ok"}])


class TravelDataTests(unittest.TestCase):
    def test_normalize_travel_data_maps_orders_and_amounts(self) -> None:
        raw = {
            "journeys": [
                {
                    "tjmJourneyid": "J1",
                    "tjmStartTime": "2026-08-01 08:00:00",
                    "tjmArrivalTime": "2026-08-03 22:30:00",
                    "tjmStartPlace": "福州",
                    "tjmArrivalPlace": "北京",
                    "tjmSubsidyDays": 2,
                    "tjmLoaclTrafficCost": 80,
                    "tjmLoaclTrafficStandard": 50,
                    "tjmAirportreturncost": 150,
                }
            ],
            "airTickets": [{"flightNo": "F1", "departureTime": "2026-08-01 08:30:00", "amount": 1000}],
            "trainTickets": [],
            "hotels": [],
            "cityTransports": [{"amount": 60, "date": "2026-08-02"}],
            "drivingCars": [{"amount": 100, "miles": 100, "standard": 1, "rate": 1}],
            "travelSubsidies": [{"days": 2, "total": 200, "standard": 100, "rate": 1}],
            "otherTransports": [],
            "otherExpenses": [{"typecode": "2001", "amount": 30, "date": "2026-08-01"}],
            "sourceStatus": {"journeys": {"status": "READY", "message": ""}},
        }
        result = normalize_travel_data(
            raw,
            instance_code="INS-1",
            audit_info={"verifiUserName": "张三", "postTypeCode": "7", "postType": "销售"},
        )
        self.assertEqual(result["employeeRole"], "销售")
        self.assertEqual(result["verifiUserName"], "张三")
        self.assertEqual(result["travelSegments"][0]["journeyId"], "J1")
        self.assertEqual(result["baggageInfo"][0]["typeCode"], "2001")
        self.assertEqual(result["subsidyInfo"]["applyAmount"], 200)
        self.assertEqual(result["stationVehicleStandard"]["eligibleOrderCount"], 1)

    def test_missing_instance_returns_non_blocking_placeholder(self) -> None:
        result = travel_receipt_enricher("R", {})
        self.assertEqual(result["instanceCode"], "")
        self.assertTrue(result["messages"])
        self.assertIn(NOT_READY_MESSAGE, result["messages"])

    def test_receipt_enricher_keeps_partial_results_when_client_fails(self) -> None:
        class FailingClient:
            def fetch_all(self, instance_code):
                raise RuntimeError("service unavailable")

        result = travel_receipt_enricher(
            "R",
            {"auditInfo": {"instanceCode": "INS-1"}},
            client=FailingClient(),
        )
        self.assertEqual(result["instanceCode"], "INS-1")
        self.assertTrue(result["messages"])

    def test_invoice_enricher_merges_ocr_tax_and_train_state(self) -> None:
        service_data = {
            "travelAudit": {
                "trainOrders": [
                    {"source": "monthly", "ticketNumber": "T-1", "trainNo": "G1", "date": "2026-08-01"}
                ],
                "taxInfo": {},
                "primaryInvoice": True,
            },
            "auditInfo": {"formInputTax": "2.5"},
        }
        result = travel_invoice_enricher(
            "R",
            "/tmp/invoice.pdf",
            {
                "invoiceType": "RJ-010",
                "invoiceNo": "T-1",
                "trainNo": "G1",
                "invoiceDate": "2026-08-01",
                "effectiveTaxAmount": "2.5",
            },
            service_data,
        )
        self.assertEqual(result["taxInfo"], {"invoiceDeductibleTax": 2.5, "formInputTax": 2.5})
        self.assertTrue(result["selfBoughtMonthlyTrain"])
        self.assertEqual(result["trainOrders"][0]["ticketNumber"], "T-1")

    def test_invoice_enricher_does_not_create_scene_or_tax_reject_data(self) -> None:
        result = travel_invoice_enricher("R", "f", {"goodsName": "未知内容"}, {"travelAudit": {}})
        self.assertIsNone(result["invoiceScene"])
        self.assertNotIn("formInputTax", result["taxInfo"])


if __name__ == "__main__":
    unittest.main()

class TravelServiceUrlBindingTests(unittest.TestCase):
    @patch("expense_audit_orchestrator.bootstrap.create_graph_runtime_client")
    @patch("expense_audit_orchestrator.bootstrap.create_kingdee_ocr_provider_from_env")
    @patch("expense_audit_orchestrator.profiles.travel.client.audit_client._fetch_service_data")
    def test_bootstrap_binds_explicit_service_url_to_travel_client(
        self,
        fetch_service_data,
        create_ocr_provider,
        create_runtime_client,
    ) -> None:
        from expense_audit_orchestrator.bootstrap import create_receipt_audit_service

        create_ocr_provider.return_value = lambda *args, **kwargs: {}
        create_runtime_client.return_value = object()
        fetch_service_data.return_value = []

        service = create_receipt_audit_service(
            profile="travel",
            audit_service_url="http://mock-service",
        )
        enricher = service._data_preparer.receipt_enrichers["travelAudit"]
        enricher("R", {"auditInfo": {"instanceCode": "INS-1"}})

        self.assertEqual(fetch_service_data.call_count, 10)
        self.assertTrue(
            all(call.kwargs["service_url"] == "http://mock-service" for call in fetch_service_data.call_args_list)
        )
