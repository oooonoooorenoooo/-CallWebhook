"""Publish only the explicitly approved, already signed build 18 artifact.

No build, signing-profile change, app-store submission, new tester or external
TestFlight group is created. Re-runs reuse an existing Apple build.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from ensure_push_profile import jwt_token

REPO = "oooonoooorenoooo/-CallWebhook"
SOURCE_RUN = 42088271868
SOURCE_SHA = "0f633bc71243df5f7adc5100c2ae8c1bd85fb2dc"
ARTIFACT_ID = 13444497446
ARTIFACT_SHA256 = "36048a57f5b5be644b3ecd85e0369401a688135f95a30ef65cb8dc7fe4796769"
BUNDLE = "de.reno.CallWebhook.U98PKCA4W7"
VERSION = "2.0"
BUILD = "18"
API = "https://api.appstoreconnect.apple.com"
ROOT = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "callwebhook-testflight-18"
RESULT = ROOT / "result.json"


def record(**fields):
    ROOT.mkdir(parents=True, exist_ok=True)
    data = json.loads(RESULT.read_text()) if RESULT.exists() else {
        "version": VERSION, "build": BUILD, "source_commit": SOURCE_SHA,
        "source_run": SOURCE_RUN, "artifact_id": ARTIFACT_ID,
    }
    data.update(fields)
    RESULT.write_text(json.dumps(data, indent=2) + "\n")
    print(json.dumps(fields), flush=True)


def gh(path):
    return json.loads(subprocess.check_output(["gh", "api", f"repos/{REPO}/{path}"]))


def verify():
    run = gh(f"actions/runs/{SOURCE_RUN}")
    if (run.get("head_sha") != SOURCE_SHA or run.get("conclusion") != "success"
            or run.get("head_branch") != "codex/macos-line3"
            or run.get("path") != ".github/workflows/build-ios.yml"):
        raise RuntimeError("The approved signed source build does not match or was not successful")
    artifact = gh(f"actions/artifacts/{ARTIFACT_ID}")
    if (artifact.get("name") != "CallWebhook-TestFlight" or artifact.get("expired")
            or artifact.get("workflow_run", {}).get("id") != SOURCE_RUN
            or artifact.get("workflow_run", {}).get("head_sha") != SOURCE_SHA
            or artifact.get("digest") != "sha256:" + ARTIFACT_SHA256):
        raise RuntimeError("The approved App Store artifact identity does not match")
    raw = subprocess.check_output(["gh", "api", f"repos/{REPO}/actions/artifacts/{ARTIFACT_ID}/zip"])
    if hashlib.sha256(raw).hexdigest() != ARTIFACT_SHA256:
        raise RuntimeError("Artifact download checksum mismatch")
    ROOT.mkdir(parents=True, exist_ok=True)
    ipa = ROOT / "CallWebhook-TestFlight.ipa"
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if archive.namelist() != ["CallWebhook-TestFlight.ipa"]:
            raise RuntimeError("Unexpected files in the approved artifact")
        ipa.write_bytes(archive.read("CallWebhook-TestFlight.ipa"))
    with zipfile.ZipFile(ipa) as archive:
        info = plistlib.loads(archive.read("Payload/CallWebhook.app/Info.plist"))
    if (info.get("CFBundleIdentifier") != BUNDLE
            or info.get("CFBundleShortVersionString") != VERSION
            or str(info.get("CFBundleVersion")) != BUILD
            or info.get("CallWebhookPushEnvironment") != "production"):
        raise RuntimeError("IPA bundle/version/build/push environment is not the approved distribution build")
    unpacked = ROOT / "verified"
    subprocess.run(["ditto", "-x", "-k", str(ipa), str(unpacked)], check=True)
    app = unpacked / "Payload/CallWebhook.app"
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    entitlements = plistlib.loads(subprocess.check_output(
        ["codesign", "-d", "--entitlements", ":-", str(app)], stderr=subprocess.DEVNULL))
    if (entitlements.get("aps-environment") != "production"
            or entitlements.get("get-task-allow") is True
            or entitlements.get("beta-reports-active") is not True
            or entitlements.get("com.apple.developer.calling-app") is not True
            or entitlements.get("com.apple.developer.dialing-app") is not True):
        raise RuntimeError("IPA does not retain distribution, push and calling entitlements")
    record(artifact_verified=True, ipa_sha256=hashlib.sha256(ipa.read_bytes()).hexdigest())


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api(path, payload=None, method="GET"):
    url = path if path.startswith(API + "/") else API + path
    if urllib.parse.urlsplit(url).netloc != "api.appstoreconnect.apple.com":
        raise RuntimeError("Unexpected Apple API origin")
    request = urllib.request.Request(url,
        data=None if payload is None else json.dumps(payload).encode(), method=method,
        headers={"Authorization": "Bearer " + jwt_token(), "Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=45) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        try:
            errors = json.load(error).get("errors", [])
            detail = "; ".join(str(e.get("code", "")) + ": " + str(e.get("detail", e.get("title", ""))) for e in errors)
        except Exception:
            detail = "No structured error details"
        raise RuntimeError(f"Apple API HTTP {error.code}: {detail[:800]}") from None


def all_data(path):
    items = []
    for _ in range(50):
        page = api(path)
        items.extend(page.get("data", []))
        path = page.get("links", {}).get("next")
        if not path:
            return items
    raise RuntimeError("Unexpected Apple API pagination limit")


def app_id():
    apps = api("/v1/apps?" + urllib.parse.urlencode({"filter[bundleId]": BUNDLE}))["data"]
    apps = [app for app in apps if app["attributes"].get("bundleId", "").lower() == BUNDLE.lower()]
    if len(apps) != 1:
        raise RuntimeError("Could not uniquely match the existing CallWebhook App Store app")
    return apps[0]["id"]


def find_build(app):
    query = urllib.parse.urlencode({"filter[app]": app, "filter[version]": BUILD,
        "filter[preReleaseVersion.version]": VERSION, "include": "preReleaseVersion", "limit": "200"})
    page = api("/v1/builds?" + query)
    versions = {x["id"]: x["attributes"] for x in page.get("included", []) if x["type"] == "preReleaseVersions"}
    builds = [x for x in page.get("data", []) if x["attributes"].get("version") == BUILD
        and versions.get(x["relationships"]["preReleaseVersion"]["data"]["id"], {}).get("version") == VERSION]
    if len(builds) > 1:
        raise RuntimeError("Build identity is ambiguous")
    return builds[0] if builds else None


def preflight():
    app = app_id()
    build = find_build(app)
    record(app_id=app, already_uploaded=build is not None)
    with open(os.environ["GITHUB_OUTPUT"], "a") as out:
        out.write("upload_needed=" + str(build is None).lower() + "\n")


def complete():
    app = app_id()
    build = None
    for _ in range(48):
        build = find_build(app)
        state = build["attributes"].get("processingState") if build else "NOT_VISIBLE_YET"
        record(apple_processing_state=state, apple_build_id=build["id"] if build else None)
        if state == "VALID":
            break
        if state in ("FAILED", "INVALID"):
            raise RuntimeError("Apple rejected build processing: " + state)
        time.sleep(15)
    else:
        raise RuntimeError("Upload completed, but Apple processing has not been confirmed as VALID")
    if build["attributes"].get("expired"):
        raise RuntimeError("The existing Apple build is expired; a new build is required")
    ident = build["id"]
    record(upload_confirmed=True, apple_processing_state="VALID", apple_build_id=ident)
    groups = all_data("/v1/betaGroups?" + urllib.parse.urlencode({"filter[app]": app, "limit": "200"}))
    internal = [g for g in groups if g["attributes"].get("isInternalGroup") is True]
    record(existing_internal_group_count=len(internal))
    # Apple permits only one relationship filter. Intersect these IDs with the
    # separately verified app's internal groups rather than combining filters.
    linked = all_data("/v1/betaGroups?" + urllib.parse.urlencode({"filter[builds]": ident, "limit": "200"}))
    linked_ids = {g["id"] for g in linked}
    usable = [g for g in internal if g["id"] in linked_ids or g["attributes"].get("hasAccessToAllBuilds") is True]
    if not usable and len(internal) == 1:
        # Retain the existing internal audience; do not invite anyone or touch external groups.
        api(f"/v1/builds/{ident}/relationships/betaGroups",
            {"data": [{"type": "betaGroups", "id": internal[0]["id"]}]}, method="POST")
        linked = all_data("/v1/betaGroups?" + urllib.parse.urlencode({"filter[builds]": ident, "limit": "200"}))
        linked_ids = {g["id"] for g in linked}
        usable = [g for g in internal if g["id"] in linked_ids]
    detail = api(f"/v1/builds/{ident}/buildBetaDetail")["data"]["attributes"]
    state = detail.get("internalBuildState")
    record(upload_confirmed=True, apple_processing_state="VALID", internal_build_state=state,
        internal_groups_available=len(usable), existing_internal_group_count=len(internal),
        external_groups_changed=False, app_store_submission=False)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
        summary.write(f"## CallWebhook {VERSION} ({BUILD})\n\nApple build: `{ident}`\n\n"
            f"Processing: **VALID**; internal testing: **{state}**; available internal groups: **{len(usable)}**.\n\n"
            "Original signed artifact verified; no app-store submission or external tester change.\n\n"
            "This is build 18 with the existing parallel-call work and CI build fixes. "
            "The reported iPhone ringing regression is NOT confirmed fixed; automatic mailbox updates "
            "and transcription are NOT included.\n")
    if not usable or state not in ("IN_BETA_TESTING", "READY_FOR_BETA_TESTING"):
        raise RuntimeError("Apple accepted the build, but internal tester availability still needs attention")
    record(testflight_available=True)


if __name__ == "__main__":
    try:
        {"verify": verify, "preflight": preflight, "complete": complete}[sys.argv[1]]()
    except Exception as error:
        record(error=str(error))
        print("::error::" + str(error), file=sys.stderr)
        sys.exit(1)
