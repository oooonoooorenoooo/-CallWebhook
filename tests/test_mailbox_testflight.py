"""Offline release guards for the current CallWebhook mailbox TestFlight build."""
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest import mock
import urllib.parse
import urllib.request
import zipfile

from scripts import mailbox_testflight as release


class MailboxTestFlightTests(unittest.TestCase):
    APP = "callwebhook-app"
    BUILD = "20"
    SHA = "a" * 40
    TEAM = "U98PKCA4W7"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.result = self.root / "result.json"
        self.ipa = self.root / "CallWebhook.ipa"
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(release, "ROOT", self.root))
        self.stack.enter_context(mock.patch.object(release, "RESULT", self.result))
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "GITHUB_SHA": self.SHA, "BUILD_NUMBER": self.BUILD,
            "APPLE_TEAM_ID": self.TEAM,
            "TESTFLIGHT_IPA": str(self.ipa),
            "GITHUB_OUTPUT": str(self.root / "output"),
            "GITHUB_STEP_SUMMARY": str(self.root / "summary"),
        }))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def build(self, number=BUILD, version="2.0", app=APP, ident="current-build"):
        return {
            "type": "builds", "id": ident,
            "attributes": {"version": number, "processingState": "VALID", "expired": False},
            "relationships": {
                "app": {"data": {"type": "apps", "id": app}},
                "preReleaseVersion": {"data": {"type": "preReleaseVersions", "id": "prerelease-" + version.replace(".", "-")}},
            },
        }

    def build_page(self, *builds, version="2.0", app=APP, next_url=None):
        return {"data": list(builds), "included": [
            {"type": "apps", "id": app, "attributes": {"bundleId": release.BUNDLE}},
            {"type": "preReleaseVersions", "id": "prerelease-" + version.replace(".", "-"),
             "attributes": {"version": version, "platform": "IOS"}},
        ], "links": {"next": next_url}}

    def write_receipt(self, **changes):
        receipt = {
            "bundle_id": release.BUNDLE, "version": release.VERSION,
            "build": self.BUILD, "source_commit": self.SHA,
            "app_id": self.APP, "prepared": True,
        }
        receipt.update(changes)
        self.result.write_text(json.dumps(receipt))
        return receipt

    def info(self):
        return {
            "CFBundleIdentifier": release.BUNDLE,
            "CFBundleShortVersionString": release.VERSION,
            "CFBundleVersion": self.BUILD,
            "CallWebhookPushEnvironment": "production",
        }

    def entitlements(self):
        return {
            "aps-environment": "production", "get-task-allow": False,
            "beta-reports-active": True,
            "com.apple.developer.calling-app": True,
            "com.apple.developer.dialing-app": True,
            "com.apple.developer.carplay-communication": True,
            "com.apple.developer.team-identifier": self.TEAM,
            "application-identifier": self.TEAM + "." + release.BUNDLE,
        }

    def write_ipa(self, info=None):
        with zipfile.ZipFile(self.ipa, "w") as archive:
            archive.writestr("Payload/CallWebhook.app/Info.plist", plistlib.dumps(info or self.info()))
            archive.writestr("Payload/CallWebhook.app/embedded.mobileprovision", b"mock CMS profile")

    def test_build_number_has_floor_and_advances_past_existing_builds(self):
        self.assertEqual(release.choose_build_number([]), 20)
        self.assertEqual(release.choose_build_number([self.build("18")]), 20)
        self.assertEqual(release.choose_build_number([self.build("20"), self.build("87")]), 88)
        self.assertEqual(release.choose_build_number([self.build("87.4.2")]), 88)

    def test_build_listing_follows_pagination_before_number_selection(self):
        next_url = release.API + "/v1/builds?cursor=next"
        pages = [self.build_page(self.build("18"), next_url=next_url),
                 self.build_page(self.build("66", ident="later-page"))]
        with mock.patch.object(release, "api", side_effect=pages) as api:
            builds = release.list_builds(self.APP)
        self.assertEqual(release.choose_build_number(builds), 67)
        self.assertEqual(api.call_count, 2)
        self.assertEqual(api.call_args_list[1].args[0], next_url)

    def test_build_listing_never_accepts_another_app_or_marketing_version(self):
        cases = [self.build_page(self.build(version="3.0"), version="3.0"),
                 self.build_page(self.build(app="foreign-app"), app="foreign-app")]
        for page in cases:
            with self.subTest(page=page), mock.patch.object(release, "api", return_value=page):
                try:
                    builds = release.list_builds(self.APP)
                except RuntimeError:
                    continue
                self.assertEqual(builds, [], "A different app/version must not be selected for release")

    def test_foreign_api_origins_are_rejected_before_creating_a_token(self):
        unsafe = ["https://foreign.invalid/v1/builds", "//foreign.invalid/v1/builds",
                  "http://api.appstoreconnect.apple.com/v1/builds",
                  "https://api.appstoreconnect.apple.com.evil.invalid/v1/builds",
                  "https://user@api.appstoreconnect.apple.com/v1/builds"]
        for url in unsafe:
            with self.subTest(url=url), mock.patch.object(release, "jwt_token") as token, \
                    mock.patch.object(release.urllib.request, "build_opener") as opener:
                with self.assertRaises((RuntimeError, ValueError)):
                    release.api(url)
                token.assert_not_called()
                opener.assert_not_called()

    def test_api_does_not_forward_authorization_through_redirects(self):
        request = urllib.request.Request(release.API + "/v1/apps",
            headers={"Authorization": "Bearer test-only"})
        self.assertIsNone(release.NoRedirect().redirect_request(request, None, 302, "Found", {},
            "https://foreign.invalid/redirected"))

    def test_stale_release_receipts_fail_before_ipa_or_apple_access(self):
        self.write_ipa()
        for change in ({"source_commit": "b" * 40}, {"build": "18"}, {"version": "3.0"},
                       {"bundle_id": "foreign.app"}, {"prepared": False}):
            with self.subTest(change=change):
                self.write_receipt(**change)
                with mock.patch.object(release, "api") as api, \
                        mock.patch.object(release.subprocess, "run") as run:
                    with self.assertRaises(RuntimeError):
                        release.verify_ipa()
                    api.assert_not_called()
                    run.assert_not_called()

    def test_complete_requires_a_current_verified_ipa_receipt(self):
        self.write_ipa()
        proof = {"ipa_verified": True, "profile_verified": True, "ipa_path": str(self.ipa),
                 "ipa_sha256": hashlib.sha256(self.ipa.read_bytes()).hexdigest()}
        for change in ({"source_commit": "b" * 40}, {"build": "18"}, {"version": "3.0"},
                       {"ipa_verified": False}, {"profile_verified": False}):
            with self.subTest(change=change):
                self.write_receipt(**dict(proof, **change))
                with mock.patch.object(release, "api") as api, \
                        mock.patch.object(release, "app_id") as app_id:
                    with self.assertRaises(RuntimeError):
                        release.complete()
                    api.assert_not_called()
                    app_id.assert_not_called()

    def test_ipa_metadata_must_match_prepared_bundle_version_build_and_push(self):
        for name, wrong in {"CFBundleIdentifier": "foreign.app", "CFBundleShortVersionString": "3.0",
                            "CFBundleVersion": "18", "CallWebhookPushEnvironment": "development"}.items():
            with self.subTest(field=name):
                self.write_receipt()
                info = self.info()
                info[name] = wrong
                self.write_ipa(info)
                with mock.patch.object(release.subprocess, "run") as run, \
                        mock.patch.object(release.subprocess, "check_output") as output:
                    with self.assertRaises(RuntimeError):
                        release.verify_ipa()
                    run.assert_not_called()
                    output.assert_not_called()

    def fake_extract_or_verify(self, command, **kwargs):
        if command[0] == "ditto":
            with zipfile.ZipFile(self.ipa) as archive:
                archive.extractall(command[-1])
        elif command[0] != "codesign":
            self.fail("Unexpected release subprocess: " + repr(command))
        return subprocess.CompletedProcess(command, 0)

    def verify_with_entitlements(self, entitlements=None, profile_changes=None):
        entitlements = entitlements or self.entitlements()
        profile = {"UUID": "test-profile", "TeamIdentifier": [self.TEAM],
                   "ExpirationDate": datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=30),
                   "Entitlements": entitlements}
        profile.update(profile_changes or {})
        def output(command, **kwargs):
            if command[0] == "codesign":
                return plistlib.dumps(entitlements)
            if command[0] == "security":
                return plistlib.dumps(profile)
            self.fail("Unexpected release inspection command: " + repr(command))
        with mock.patch.object(release.subprocess, "run", side_effect=self.fake_extract_or_verify), \
                mock.patch.object(release.subprocess, "check_output", side_effect=output):
            release.verify_ipa()

    def test_verified_ipa_records_the_exact_file_checksum(self):
        self.write_receipt()
        self.write_ipa()
        self.verify_with_entitlements()
        receipt = json.loads(self.result.read_text())
        self.assertIs(receipt.get("ipa_verified"), True)
        self.assertIs(receipt.get("profile_verified"), True)
        self.assertEqual(receipt.get("ipa_sha256"), hashlib.sha256(self.ipa.read_bytes()).hexdigest())
        self.assertEqual(receipt.get("source_commit"), self.SHA)

    def test_distribution_entitlements_preserve_push_calling_and_dialer(self):
        wrong_values = {"aps-environment": "development", "get-task-allow": True,
                        "beta-reports-active": False, "com.apple.developer.calling-app": False,
                        "com.apple.developer.dialing-app": False,
                        "com.apple.developer.carplay-communication": False,
                        "application-identifier": self.TEAM + ".foreign.app"}
        for field, wrong in wrong_values.items():
            with self.subTest(field=field):
                self.write_receipt()
                self.write_ipa()
                entitlements = self.entitlements()
                entitlements[field] = wrong
                with self.assertRaises(RuntimeError):
                    self.verify_with_entitlements(entitlements)

    def test_exported_profile_must_be_current_app_store_profile_of_the_approved_team(self):
        wrong_profile_entitlements = self.entitlements()
        wrong_profile_entitlements["com.apple.developer.carplay-communication"] = False
        for changes in [
            {"ExpirationDate": datetime(2000, 1, 1)},
            {"ProvisionedDevices": ["test-device"]},
            {"ProvisionsAllDevices": True},
            {"TeamIdentifier": ["OTHERTEAM1"]},
            {"Entitlements": wrong_profile_entitlements},
        ]:
            with self.subTest(changes=changes):
                self.write_receipt()
                self.write_ipa()
                with self.assertRaises(RuntimeError):
                    self.verify_with_entitlements(profile_changes=changes)

    def test_prepare_replaces_stale_receipt_with_current_source_and_selected_build(self):
        self.write_receipt(source_commit="b" * 40, ipa_verified=True, testflight_available=True)
        with mock.patch.object(release, "app_id", return_value=self.APP), \
                mock.patch.object(release, "list_builds", return_value=[self.build("44")]):
            release.prepare()
        receipt = json.loads(self.result.read_text())
        self.assertEqual(receipt["build"], "45")
        self.assertEqual(receipt["source_commit"], self.SHA)
        self.assertEqual(receipt["version"], "2.0")
        self.assertIs(receipt["prepared"], True)
        self.assertNotIn("ipa_verified", receipt)
        self.assertNotIn("testflight_available", receipt)
        self.assertIn("build_number=45", (self.root / "output").read_text())

    def group(self, ident="internal-group", *, internal=True, app=APP, all_builds=False):
        return {"type": "betaGroups", "id": ident,
                "attributes": {"isInternalGroup": internal, "hasAccessToAllBuilds": all_builds},
                "relationships": {"app": {"data": {"type": "apps", "id": app}}}}

    def complete_with_groups(self, groups, *, reread=None, linked=None):
        self.write_receipt(ipa_verified=True, profile_verified=True, ipa_sha256="1" * 64)
        linked_ids = list(linked or [])
        writes = []

        def all_data(path):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            self.assertEqual(urllib.parse.urlsplit(path).path, "/v1/betaGroups")
            if "filter[app]" in query:
                self.assertEqual(query["filter[app]"], [self.APP])
                return copy.deepcopy(groups)
            self.assertEqual(query.get("filter[builds]"), ["current-build"])
            return [self.group(ident) for ident in linked_ids]

        def api(path, payload=None, method="GET"):
            if method == "POST":
                self.assertEqual(path, "/v1/builds/current-build/relationships/betaGroups")
                self.assertEqual(payload, {"data": [{"type": "betaGroups", "id": "internal-group"}]})
                writes.append((path, payload))
                linked_ids.append("internal-group")
                return {}
            if path == "/v1/betaGroups/internal-group?include=app":
                return {"data": copy.deepcopy(reread or self.group())}
            if path == "/v1/builds/current-build/buildBetaDetail":
                return {"data": {"attributes": {"internalBuildState": "IN_BETA_TESTING"}}}
            self.fail("Unexpected Apple request: " + repr((method, path, payload)))

        with mock.patch.object(release, "app_id", return_value=self.APP), \
                mock.patch.object(release, "find_build", return_value=self.build()), \
                mock.patch.object(release, "all_data", side_effect=all_data), \
                mock.patch.object(release, "api", side_effect=api), \
                mock.patch.object(release, "pause_polling", side_effect=AssertionError("Unexpected waiting")):
            release.complete()
        return writes

    def test_complete_links_only_the_existing_internal_group_of_this_app(self):
        writes = self.complete_with_groups([self.group(), self.group("external-group", internal=False)])
        self.assertEqual(len(writes), 1)
        receipt = json.loads(self.result.read_text())
        self.assertIs(receipt["testflight_available"], True)
        self.assertIs(receipt["external_groups_changed"], False)
        self.assertEqual(receipt["internal_build_state"], "IN_BETA_TESTING")
        self.assertEqual(receipt["linked_existing_internal_group"], "internal-group")

    def test_complete_reuses_existing_internal_access_without_group_writes(self):
        self.assertEqual(self.complete_with_groups([self.group(all_builds=True)]), [])
        self.assertEqual(self.complete_with_groups([self.group()], linked=["internal-group"]), [])

    def test_complete_refuses_external_ambiguous_or_reassigned_groups(self):
        for groups, reread in [
            ([self.group("external-group", internal=False)], None),
            ([self.group(), self.group("second-internal")], None),
            ([self.group()], self.group(app="foreign-app")),
            ([self.group()], self.group(internal=False)),
        ]:
            with self.subTest(groups=groups, reread=reread):
                with self.assertRaises(RuntimeError):
                    self.complete_with_groups(groups, reread=reread)
                receipt = json.loads(self.result.read_text())
                self.assertNotIn("linked_existing_internal_group", receipt)
                self.assertNotIn("testflight_available", receipt)


if __name__ == "__main__":
    unittest.main()

