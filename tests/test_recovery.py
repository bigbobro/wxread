import importlib.util
import io
import json
import os
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import requests
import yaml

import progress


ROOT = Path(__file__).resolve().parents[1]


def response(payload=None, status=200, cookie=False):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload or {}).encode()
    if cookie:
        result.cookies.set("wr_skey", "synthetic-cookie")
    return result


class ReadingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name) / "progress.json"
        self.environment = patch.dict(os.environ, {
            "WXREAD_STATE_FILE": str(self.state),
            "WXREAD_DATE": progress.beijing_day(),
            "WXREAD_DEFER_NOTIFICATION": "1",
            "WXREAD_MAX_RUNTIME_SECONDS": "7200",
            "GITHUB_STEP_SUMMARY": str(Path(self.directory.name) / "summary.md"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def module(self, replies=(), target=2, renewal=True):
        config = ModuleType("config")
        for key, value in {
            "data": {"s": "placeholder"}, "headers": {},
            "cookies": {"wr_skey": "synthetic-cookie"}, "READ_NUM": target,
            "PUSH_METHOD": "test", "book": ["synthetic-book"],
            "chapter": ["synthetic-chapter"],
        }.items():
            setattr(config, key, value)
        notifier = ModuleType("push")
        notifier.push = Mock(return_value=True)
        spec = importlib.util.spec_from_file_location("tested_main", ROOT / "main.py")
        module = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", {"config": config, "push": notifier}):
            spec.loader.exec_module(module)
        remaining = iter(replies)
        module.read_calls = []
        module.renewals = []

        def post(url, **kwargs):
            if url == module.RENEW_URL:
                module.renewals.append(url)
                return response(cookie=renewal)
            if url == module.FIX_SYNCKEY_URL:
                return response()
            self.assertEqual(url, module.READ_URL)
            module.read_calls.append(json.loads(kwargs["data"]))
            value = next(remaining, {"succ": 1, "synckey": "synthetic"})
            if isinstance(value, Exception):
                raise value
            return value if isinstance(value, requests.Response) else response(value)

        for context in (patch.object(module.requests, "post", side_effect=post),
                        patch.object(module.time, "sleep")):
            context.start()
            self.addCleanup(context.stop)
        return module

    def seed(self, count, day=None):
        progress.write_json(self.state, {
            "schema": 1, "date": day or progress.beijing_day(), "completed": count,
        })

    def count(self):
        return json.loads(self.state.read_text())["completed"]

    def test_temporary_network_failure_retries_and_finishes(self):
        module = self.module([requests.ConnectionError("synthetic")])
        self.assertEqual(module.main(), 0)
        self.assertEqual((len(module.read_calls), self.count()), (3, 2))
        module.push.assert_not_called()  # Reading and notification are separate steps.

    def test_http_503_retries_but_400_stops(self):
        module = self.module([response(status=503)])
        self.assertEqual(module.main(), 0)
        self.assertEqual(len(module.read_calls), 3)
        self.seed(0)
        module = self.module([response(status=400)])
        self.assertEqual(module.main(), 1)
        self.assertEqual((len(module.read_calls), self.count()), (1, 0))

    def test_partial_failure_resumes_then_third_run_skips(self):
        module = self.module([
            {"succ": 1, "synckey": "synthetic"},
            *[requests.Timeout("synthetic") for _ in range(4)],
        ])
        self.assertEqual(module.main(), 1)
        self.assertEqual(self.count(), 1)
        module = self.module()
        self.assertEqual(module.main(), 0)
        self.assertEqual((len(module.read_calls), self.count()), (1, 2))
        self.assertEqual(module.read_calls[0]["rt"], 30)
        module = self.module()
        self.assertEqual(module.main(), 0)
        self.assertEqual(module.read_calls, [])
        self.assertEqual(module.renewals, [])

    def test_yesterdays_success_does_not_skip_today(self):
        self.seed(2, "2000-01-01")
        module = self.module()
        self.assertEqual(module.main(), 0)
        self.assertEqual(len(module.read_calls), 2)
        self.assertEqual(json.loads(self.state.read_text())["date"], progress.beijing_day())

    def test_corrupt_state_is_not_silently_reset(self):
        self.state.write_text("invalid json")
        module = self.module()
        self.assertEqual(module.main(), 1)
        self.assertEqual(module.read_calls, [])
        self.assertEqual(self.state.read_text(), "invalid json")

    def test_cookie_refresh_loop_is_bounded(self):
        module = self.module([{"succ": 0}] * 4)
        self.assertEqual(module.main(), 1)
        self.assertEqual((len(module.read_calls), len(module.renewals)), (4, 4))
        self.assertEqual(self.count(), 0)

    def test_false_success_flag_does_not_advance(self):
        module = self.module([{"succ": 0, "synckey": "synthetic"}] * 4)
        self.assertEqual(module.main(), 1)
        self.assertEqual(self.count(), 0)

    def test_synckey_repair_is_bounded(self):
        module = self.module([{"succ": 1}] * 4)
        self.assertEqual(module.main(), 1)
        self.assertEqual(len(module.read_calls), 4)
        self.assertEqual(self.count(), 0)

    def test_startup_renewal_failure_preserves_existing_cookie(self):
        module = self.module(renewal=False)
        self.assertEqual(module.main(), 0)
        self.assertEqual(self.count(), 2)

    def test_notification_failure_does_not_restart_reading(self):
        module = self.module()
        self.assertEqual(module.main(), 0)
        module.push.return_value = False
        self.assertEqual(module.notify_result(), 1)
        self.assertEqual(self.count(), 2)
        module = self.module()
        self.assertEqual(module.main(), 0)
        self.assertEqual(module.notify_result(), 0)
        module.push.assert_not_called()
        self.assertEqual(module.read_calls, [])

    def test_day_rollover_stops_before_network(self):
        self.seed(1)
        module = self.module()
        with patch.object(module, "beijing_day", return_value="2099-01-01"):
            self.assertEqual(module.main(), 1)
        self.assertEqual((module.read_calls, self.count()), ([], 1))

    def test_deadline_stops_before_network(self):
        self.seed(1)
        module = self.module()
        with patch.dict(os.environ, {"WXREAD_MAX_RUNTIME_SECONDS": "0"}):
            self.assertEqual(module.main(), 1)
        self.assertEqual((module.read_calls, self.count()), ([], 1))


class ProgressRestoreTests(unittest.TestCase):
    day = "2026-09-27"
    repository = "example/wxread"

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "progress.json"

    def artifact(self, identity):
        return {
            "id": identity, "name": f"wxread-progress-{self.day}", "expired": False,
            "workflow_run": {"id": identity, "head_branch": "main",
                             "head_repository_id": 1, "repository_id": 1},
        }

    def archive(self, count, **extra):
        data = {"schema": 1, "date": self.day, "completed": count, **extra}
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("progress.json", json.dumps(data))
        return buffer.getvalue()

    def fake_api(self, artifacts, counts):
        def api(endpoint):
            if "/actions/artifacts?" in endpoint:
                return json.dumps({"artifacts": artifacts}).encode()
            identity = int(endpoint.split("/")[-2 if endpoint.endswith("/zip") else -1])
            if endpoint.endswith("/zip"):
                return self.archive(counts[identity])
            return json.dumps({
                "path": ".github/workflows/deploy.yml", "event": "schedule",
                "head_branch": "main", "head_repository": {"full_name": self.repository},
            }).encode()
        return api

    def test_restores_maximum_count_even_when_latest_is_lower(self):
        artifacts = [self.artifact(1), self.artifact(2)]
        with patch.object(progress, "github_api", side_effect=self.fake_api(artifacts, {1: 100, 2: 60})):
            value = progress.restore_progress(self.path, self.day, self.repository)
        self.assertEqual(value.completed, 100)
        self.assertEqual(set(json.loads(self.path.read_text())), {"schema", "date", "completed"})

    def test_no_artifact_initializes_new_day(self):
        with patch.object(progress, "github_api", return_value=b'{"artifacts": []}'):
            value = progress.restore_progress(self.path, self.day, self.repository)
        self.assertEqual(value.completed, 0)

    def test_artifact_from_fork_is_ignored(self):
        artifact = self.artifact(1)
        artifact["workflow_run"]["head_repository_id"] = 2
        with patch.object(progress, "github_api", side_effect=self.fake_api([artifact], {1: 100})) as api:
            value = progress.restore_progress(self.path, self.day, self.repository)
        self.assertEqual(value.completed, 0)
        self.assertEqual(api.call_count, 1)

    def test_artifact_from_another_workflow_is_ignored(self):
        api = self.fake_api([self.artifact(1)], {1: 100})
        def wrong_workflow(endpoint):
            value = api(endpoint)
            if "/actions/runs/" in endpoint:
                run = json.loads(value)
                run["path"] = ".github/workflows/unrelated.yml"
                return json.dumps(run).encode()
            return value
        with patch.object(progress, "github_api", side_effect=wrong_workflow):
            value = progress.restore_progress(self.path, self.day, self.repository)
        self.assertEqual(value.completed, 0)

    def test_read_error_preserves_existing_file(self):
        self.path.write_text("existing-state")
        with patch.object(progress, "github_api", side_effect=RuntimeError("unavailable")):
            with self.assertRaises(RuntimeError):
                progress.restore_progress(self.path, self.day, self.repository)
        self.assertEqual(self.path.read_text(), "existing-state")

    def test_rejects_extra_fields_in_checkpoint(self):
        api = self.fake_api([self.artifact(1)], {1: 10})
        with patch.object(progress, "github_api", side_effect=lambda endpoint:
                          self.archive(10, unexpected="synthetic") if endpoint.endswith("/zip") else api(endpoint)):
            with self.assertRaises(ValueError):
                progress.restore_progress(self.path, self.day, self.repository)
        self.assertFalse(self.path.exists())

    def test_pagination_reaches_later_artifact(self):
        api = self.fake_api([self.artifact(2)], {2: 50})
        expired = {**self.artifact(1), "expired": True}
        with patch.object(progress, "github_api", side_effect=lambda endpoint:
                          json.dumps({"artifacts": [expired] * 100}).encode()
                          if endpoint.endswith("&page=1") else api(endpoint)):
            value = progress.restore_progress(self.path, self.day, self.repository)
        self.assertEqual(value.completed, 50)

    def test_schema_rejects_invalid_counts(self):
        for count in (True, -1, "2", 2881):
            with self.subTest(count=count), self.assertRaises(ValueError):
                progress.validate_progress({"schema": 1, "date": self.day, "completed": count}, self.day)


class WorkflowTests(unittest.TestCase):
    def test_schedule_and_checkpoint_order(self):
        workflow = yaml.load((ROOT / ".github/workflows/deploy.yml").read_text(), Loader=yaml.BaseLoader)
        times = []
        for entry in workflow["on"]["schedule"]:
            minute, hour, *daily = entry["cron"].split()
            self.assertEqual(daily, ["*", "*", "*"])
            when = datetime(2026, 9, 27, int(hour), int(minute), tzinfo=timezone.utc)
            times.append(when.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%H:%M"))
        self.assertEqual(times, ["12:07", "16:17", "18:27"])
        self.assertEqual(workflow["concurrency"]["cancel-in-progress"], "false")
        steps = workflow["jobs"]["deploy"]["steps"]
        names = [step["name"] for step in steps]
        self.assertLess(names.index("Save today's reading progress"), names.index("Notify reading result"))
        save = steps[names.index("Save today's reading progress")]
        self.assertIn("always()", save["if"])
        self.assertEqual(save["with"]["path"], "run-state/progress.json")


class NotificationTests(unittest.TestCase):
    def setUp(self):
        config = ModuleType("config")
        for name in ("PUSHPLUS_TOKEN", "SERVERCHAN_SPT", "TELEGRAM_BOT_TOKEN",
                     "TELEGRAM_CHAT_ID", "WXPUSHER_SPT"):
            setattr(config, name, "synthetic-placeholder")
        spec = importlib.util.spec_from_file_location("tested_push", ROOT / "push.py")
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", {"config": config}):
            spec.loader.exec_module(self.module)

    def test_business_error_is_not_success_despite_http_200(self):
        for method in ("pushplus", "wxpusher", "telegram", "serverchan"):
            with self.subTest(method=method), \
                    patch.object(self.module.requests, "post", return_value=response({"code": 999, "ok": False})), \
                    patch.object(self.module.requests, "get", return_value=response({"code": 999})), \
                    patch.object(self.module.time, "sleep"):
                self.assertFalse(self.module.push("synthetic message", method, is_success=False))

    def test_success_responses_are_accepted(self):
        for method, payload in (("pushplus", {"code": 200}), ("wxpusher", {"code": 1000}),
                                ("serverchan", {"code": 0}), ("telegram", {"ok": True})):
            with self.subTest(method=method), \
                    patch.object(self.module.requests, "post", return_value=response(payload)), \
                    patch.object(self.module.requests, "get", return_value=response(payload)):
                self.assertTrue(self.module.push("synthetic message", method))

    def test_notification_retry_budget_is_bounded(self):
        with patch.object(self.module.requests, "post", side_effect=requests.Timeout("synthetic")) as post, \
                patch.object(self.module.time, "sleep") as sleep:
            self.assertFalse(self.module.push("synthetic message", "pushplus"))
        self.assertEqual(post.call_count, 5)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 15, 30, 60])

    def test_wxpusher_preserves_run_link_as_one_path_component(self):
        content = "补跑失败\nhttps://github.com/example/wxread/actions/runs/1"
        with patch.object(self.module.requests, "get", return_value=response({"code": 1000})) as get:
            self.assertTrue(self.module.push(content, "wxpusher", is_success=False))
        encoded = get.call_args.args[0].rsplit("/", 1)[-1]
        self.assertEqual(unquote(encoded), content)


if __name__ == "__main__":
    unittest.main()
