"""Transport-level guards for the private multi-event artifact API."""

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from core.dependencies import get_multi_event_artifact_job_service
from main import app


TRACK1_KEY = "track1-synthetic-key-" + "k" * 24
ARTIFACT_JOBS = "/api/v2/multi-event/jobs"


class UnusedArtifactService:
    def __getattr__(self, _name):
        raise AssertionError("invalid or unauthorized requests must not reach the job service")


class MultiEventArtifactRouteTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"TRACK1_INTERNAL_SHARED_SECRET": TRACK1_KEY})
        self.env.start()
        app.dependency_overrides[get_multi_event_artifact_job_service] = lambda: UnusedArtifactService()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        self.env.stop()

    def test_artifact_job_path_requires_internal_auth_before_body_validation(self):
        response = self.client.post(
            ARTIFACT_JOBS,
            content="{",
            headers={"content-type": "application/json"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("INVALID_MULTI_EVENT_ARTIFACT_REQUEST", response.text)

    def test_artifact_manifest_validation_does_not_echo_payload_values(self):
        sentinel = "PSEUDONYM_ARTIFACT_INPUT_SENTINEL"
        response = self.client.post(
            ARTIFACT_JOBS,
            json={"manifest": {"accountId": sentinel, "objectKey": sentinel}},
            headers={"X-TD-Track1-Key": TRACK1_KEY},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json(),
            {"detail": [{"code": "INVALID_MULTI_EVENT_ARTIFACT_REQUEST"}]},
        )
        self.assertNotIn(sentinel, response.text)


if __name__ == "__main__":
    unittest.main()
