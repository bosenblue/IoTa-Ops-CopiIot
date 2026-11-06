import os
import tempfile
import unittest
from pathlib import Path

TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["IOTA_DB_PATH"] = str(Path(TEMP_DIR.name) / "test.db")
os.environ["IOTA_UPLOAD_DIR"] = str(Path(TEMP_DIR.name) / "runbooks")

from fastapi.testclient import TestClient

import server


class OpsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(server.app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        TEMP_DIR.cleanup()

    def test_seed_data_is_available(self):
        response = self.client.get("/api/devices")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 5)

    def test_alert_update_is_persisted_and_audited(self):
        response = self.client.patch("/api/alerts/AL-3108", json={"state": "处理中"})
        self.assertEqual(response.status_code, 200)
        alerts = self.client.get("/api/alerts", params={"state": "处理中"}).json()
        self.assertIn("AL-3108", [alert["id"] for alert in alerts])
        audit = self.client.get("/api/audit").json()
        self.assertTrue(any("AL-3108" in item["title"] for item in audit))

    def test_uploaded_runbook_is_retrieved_by_diagnosis(self):
        content = "GW-204 网关共享链路异常排查：先检查网关网络和供电，再确认设备心跳是否恢复。"
        upload = self.client.post("/api/documents", files={"file": ("gateway.md", content.encode("utf-8"), "text/markdown")})
        self.assertEqual(upload.status_code, 201)
        result = self.client.post("/api/diagnose", json={"question": "分析 GW-204 网关离线原因"})
        self.assertEqual(result.status_code, 200)
        self.assertIn("gateway", " ".join(source["title"] for source in result.json()["sources"]).lower())
        self.assertTrue(result.json()["sources"])

    def test_invalid_upload_is_rejected(self):
        response = self.client.post("/api/documents", files={"file": ("notes.pdf", b"not a text file", "application/pdf")})
        self.assertEqual(response.status_code, 415)


if __name__ == "__main__":
    unittest.main()
