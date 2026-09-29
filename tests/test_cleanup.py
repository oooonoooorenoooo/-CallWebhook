"""Run the destructive workflow only against temporary files and a fake Supervisor."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import yaml

SOURCE = Path(__file__).parents[1] / "callwebhook_cleanup/cleanup.py"
SPEC = importlib.util.spec_from_file_location("cleanup", SOURCE)
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = {"repositories": [
            {"slug": "abc", "source": cleanup.CALLWEBHOOK_REPO},
            {"slug": "def", "source": cleanup.ASTERISK_REPO},
            {"slug": "other", "source": "https://example.com/other"}]}
        self.installed = {"abc_callwebhook_bootstrap", "abc_callwebhook_push_relay",
                          "abc_callwebhook_cleanup", "def_asterisk", "other_asterisk", "other_test"}
        self.calls = []
        self.write("configuration.yaml", "# keep\ndefault_config:\ncallwebhook:\ninput_boolean:\n  iphone_call_active:\n    name: Call\n  keep:\n    name: Keep\nautomation: !include automations.yaml\n")
        self.write("secrets.yaml", "# other credentials\nfritz_callwebhook_user: u\nfritz_callwebhook_password: p\nother: keep\n")
        self.write("callwebhook/voip.json", '{"key":"test"}')
        self.write("callwebhook/archive/message.wav", "audio")
        self.write("custom_components/callwebhook/__init__.py", "# app")
        self.write("custom_components/other/__init__.py", "# keep")
        self.write(".storage/input_boolean", json.dumps({"version": 1, "data": {"items": [
            {"id": "iphone_call_active", "name": "iphone_call_active"}, {"id": "keep"}]}}))
        self.write(".storage/core.entity_registry", json.dumps({"version": 1, "data": {"entities": [
            {"platform": "input_boolean", "unique_id": "iphone_call_active", "entity_id": "input_boolean.renamed"},
            {"platform": "light", "unique_id": "iphone_call_active", "entity_id": "light.keep"}], "deleted_entities": []}}))
        self.write(".storage/core.restore_state", json.dumps({"version": 1, "data": [
            {"state": {"entity_id": "input_boolean.renamed"}}, {"state": {"entity_id": "light.keep"}}]}))

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)

    def api(self, method, path, data=None):
        self.calls.append((method, path, data))
        if path == "/addons/self/info":
            return {"slug": "abc_callwebhook_cleanup"}
        if path == "/store":
            return self.store
        if path == "/addons":
            return {"addons": [{"slug": slug} for slug in self.installed]}
        if path.endswith("/info"):
            return {"state": "started"}
        if path.endswith("/uninstall"):
            self.assertEqual(data, {"remove_config": True})
            self.installed.remove(path.split("/")[2])
        return {}

    def test_complete_cleanup_preserves_neighbors_and_restarts_before_self_removal(self):
        cleanup.uninstall(self.root, self.api)
        self.assertEqual(self.installed, {"other_asterisk", "other_test"})
        self.assertFalse((self.root / "callwebhook").exists())
        self.assertFalse((self.root / "custom_components/callwebhook").exists())
        self.assertTrue((self.root / "custom_components/other/__init__.py").exists())
        config = (self.root / "configuration.yaml").read_text()
        self.assertIn("automation: !include automations.yaml", config)
        self.assertIn("  keep:\n    name: Keep", config)
        self.assertNotIn("callwebhook:", config)
        self.assertNotIn("iphone_call_active", config)
        self.assertEqual((self.root / "secrets.yaml").read_text(), "# other credentials\nother: keep\n")
        registry = json.loads((self.root / ".storage/core.entity_registry").read_text())
        self.assertEqual([e["entity_id"] for e in registry["data"]["entities"]], ["light.keep"])
        self.assertEqual(self.calls[-2][1], "/core/start")
        self.assertEqual(self.calls[-1][1], "/addons/abc_callwebhook_cleanup/uninstall")
        # Repeating the file cleanup does nothing and cannot grow its scope.
        self.assertEqual(cleanup.file_plan(self.root)[0], {})

    def test_failure_restarts_ha_and_keeps_tool_for_retry(self):
        def failed(method, path, data=None):
            if path == "/addons/def_asterisk/uninstall":
                raise RuntimeError("busy")
            return self.api(method, path, data)
        with self.assertRaises(RuntimeError):
            cleanup.uninstall(self.root, failed)
        self.assertEqual(self.calls[-1][1], "/core/start")
        self.assertIn("abc_callwebhook_cleanup", self.installed)
        self.assertTrue((self.root / "callwebhook/voip.json").exists())

    def test_symlink_rejected_before_any_stop_or_deletion(self):
        (self.root / "secrets.yaml").unlink()
        (self.root / "secrets.yaml").symlink_to(self.root / "configuration.yaml")
        with self.assertRaises(ValueError):
            cleanup.uninstall(self.root, self.api)
        self.assertFalse(any(method == "POST" for method, _, _ in self.calls))

    def test_invalid_yaml_and_cross_entry_anchor_rejected(self):
        for text in ("callwebhook: [\n", "callwebhook: &shared {}\nother: *shared\n"):
            with self.assertRaises(yaml.YAMLError):
                cleanup.remove_yaml_keys(text, [("callwebhook",)])

    def test_exact_repositories_required(self):
        self.store["repositories"] = [self.store["repositories"][-1]]
        with self.assertRaises(RuntimeError):
            cleanup.uninstall(self.root, self.api)
        self.assertFalse(any(method == "POST" for method, _, _ in self.calls))

    def test_last_yaml_helper_removes_empty_parent_and_preserves_next_section(self):
        text = "input_boolean:\n  iphone_call_active:\n    name: Call\nautomation: !include automations.yaml\n"
        result = cleanup.remove_yaml_keys(text, [("input_boolean", "iphone_call_active")])
        self.assertEqual(result, "automation: !include automations.yaml\n")
