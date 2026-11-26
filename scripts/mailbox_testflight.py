"""Verify and complete the current CallWebhook mailbox TestFlight release.

Stages: prepare -> archive/export in CI -> verify_ipa -> upload in CI -> complete.
The helper never downloads an old artifact, creates testers/groups, changes
profiles, submits an App Store version, or enrolls an external audience.
"""
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

try:
    from .ensure_push_profile import jwt_token
except ImportError:
    from ensure_push_profile import jwt_token


BUNDLE = "de.reno.CallWebhook.U98PKCA4W7"
VERSION = "2.0"
API = "https://api.appstoreconnect.apple.com"
ROOT = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "callwebhook-mailbox-testflight"
RESULT = ROOT / "result.json"
POLL_SECONDS = 15
PROCESSING_TIMEOUT = 12 * 60
_deadline = None


class ReleaseError(RuntimeError):
    pass


class AppleHTTPError(ReleaseError):
    def __init__(self, status, detail):
        self.status = status
        super().__init__(f"Apple API HTTP {status}: {detail}")


def safe_message(message):
    text = str(message)
    for name in ("ASC_PRIVATE_KEY", "ASC_KEY_ID", "ASC_ISSUER_ID"):
        value = os.environ.get(name)
        if value:
            text = text.replace(value, "[redacted]")
    text = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[redacted JWT]", text)
    return text[:1200]


def source_commit():
    value = os.environ.get("GITHUB_SHA", "")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        raise ReleaseError("GITHUB_SHA must identify the current checked-out source commit")
    return value.lower()


def expected_build():
    value = os.environ.get("BUILD_NUMBER", "")
    if not re.fullmatch(r"[1-9][0-9]*", value) or int(value) < 20:
        raise ReleaseError("BUILD_NUMBER must be the prepared integer build number, at least 20")
    return value


def resource_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9-]+", value):
        raise ReleaseError("Apple returned an invalid resource identifier")
    return value


def write_result(data):
    ROOT.mkdir(parents=True, exist_ok=True)
    temporary = RESULT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(RESULT)


def record(**fields):
    data = json.loads(RESULT.read_text()) if RESULT.exists() else {}
    data.update(fields)
    write_result(data)
    print(json.dumps(fields), flush=True)


def release_receipt(require_ipa=False):
    if not RESULT.is_file():
        raise ReleaseError("Release preparation receipt is missing; run prepare first")
    data = json.loads(RESULT.read_text())
    if (data.get("prepared") is not True or data.get("source_commit") != source_commit()
            or data.get("bundle_id") != BUNDLE or data.get("version") != VERSION
            or data.get("build") != expected_build()):
        raise ReleaseError("Release receipt does not match the current source, app, version and build")
    resource_id(data.get("app_id"))
    if require_ipa and (data.get("ipa_verified") is not True or data.get("profile_verified") is not True
            or not re.fullmatch(r"[0-9a-f]{64}", data.get("ipa_sha256", ""))):
        raise ReleaseError("The current release IPA has not passed signature and entitlement verification")
    return data


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api_url(path):
    if not isinstance(path, str) or any(ord(c) < 32 for c in path) or "\\" in path:
        raise ReleaseError("Invalid Apple API URL")
    url = API + path if path.startswith("/v1/") else path
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "api.appstoreconnect.apple.com"
            or parsed.username or parsed.password or parsed.fragment
            or not parsed.path.startswith("/v1/")):
        raise ReleaseError("Refusing an Apple API URL outside the exact HTTPS API origin")
    decoded = urllib.parse.unquote(parsed.path)
    if ".." in PurePosixPath(decoded).parts or "\\" in decoded:
        raise ReleaseError("Invalid Apple API resource path")
    return url


def remaining_time():
    if _deadline is None:
        return 45
    remaining = _deadline - time.monotonic()
    if remaining <= 0:
        raise ReleaseError("Apple TestFlight verification exceeded the 12-minute deadline")
    return min(45, remaining)


def api(path, payload=None, method="GET"):
    # Validate BEFORE minting a token, including every pagination/related link.
    url = api_url(path)
    route = urllib.parse.urlsplit(url).path
    if method != "GET" and not (method == "POST" and re.fullmatch(
            r"/v1/builds/[A-Za-z0-9-]+/relationships/betaGroups", route)):
        raise ReleaseError("This release helper permits only reads and an existing beta-group linkage")
    if method == "GET" and payload is not None:
        raise ReleaseError("Unexpected payload on an Apple API read")
    try:
        token = jwt_token()
    except Exception:
        raise ReleaseError("Could not create Apple authentication with the configured CI key") from None
    request = urllib.request.Request(url,
        data=None if payload is None else json.dumps(payload).encode(), method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=remaining_time()) as response:
            api_url(response.geturl())
            raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise ReleaseError("Apple API response exceeded the expected size")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            raise AppleHTTPError(error.code, "Redirect refused; credentials remain on the approved API origin") from None
        try:
            body = json.loads(error.read(16_384))
            details = "; ".join(str(e.get("code", "")) + ": " + str(e.get("detail", e.get("title", "")))
                                for e in body.get("errors", [])[:4])
        except Exception:
            details = "No structured error details"
        raise AppleHTTPError(error.code, safe_message(details or "No structured error details")) from None
    except urllib.error.URLError:
        raise ReleaseError("The approved Apple API could not be reached") from None


def all_pages(path):
    seen = set()
    initial_path = urllib.parse.urlsplit(api_url(path)).path
    for _ in range(1000):
        url = api_url(path)
        if url in seen or urllib.parse.urlsplit(url).path != initial_path:
            raise ReleaseError("Invalid or cyclic Apple API pagination")
        seen.add(url)
        page = api(url)
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            raise ReleaseError("Apple API list response has an unexpected shape")
        yield page
        path = page.get("links", {}).get("next")
        if not path:
            return
    raise ReleaseError("Apple API pagination limit reached; refusing an incomplete build inventory")


def all_data(path):
    return [item for page in all_pages(path) for item in page["data"]]


def app_id():
    apps = all_data("/v1/apps?" + urllib.parse.urlencode({
        "filter[bundleId]": BUNDLE, "fields[apps]": "bundleId", "limit": "200"}))
    # Apple may normalize identifier casing; no name/prefix matching is allowed.
    matches = [app for app in apps if app.get("type") == "apps"
               and app.get("attributes", {}).get("bundleId", "").lower() == BUNDLE.lower()]
    if len(matches) != 1:
        raise ReleaseError("The existing CallWebhook App Store app could not be uniquely matched by bundle ID")
    return resource_id(matches[0]["id"])


def relationship_id(item, name):
    relation = item.get("relationships", {}).get(name, {}).get("data")
    if not isinstance(relation, dict):
        raise ReleaseError("Apple omitted the requested " + name + " identity")
    return resource_id(relation.get("id"))


def list_builds(app, build_number=None):
    resource_id(app)
    query = {"filter[app]": app, "filter[preReleaseVersion.version]": VERSION,
             "include": "preReleaseVersion,app", "limit": "200"}
    if build_number is not None:
        query["filter[version]"] = str(build_number)
    builds, versions = [], {}
    for page in all_pages("/v1/builds?" + urllib.parse.urlencode(query)):
        builds.extend(page["data"])
        for item in page.get("included", []):
            if item.get("type") == "preReleaseVersions":
                versions[resource_id(item["id"])] = item.get("attributes", {})
    verified = {}
    for build in builds:
        ident = resource_id(build.get("id"))
        if build.get("type") != "builds" or relationship_id(build, "app") != app:
            raise ReleaseError("Apple returned a build belonging to a different app")
        version = versions.get(relationship_id(build, "preReleaseVersion"))
        if not version or version.get("version") != VERSION:
            raise ReleaseError("Apple build does not match the requested marketing version 2.0")
        if build_number is not None and build.get("attributes", {}).get("version") != str(build_number):
            raise ReleaseError("Apple returned a different build number than the release receipt")
        if ident in verified and verified[ident] != build:
            raise ReleaseError("Conflicting build identity across Apple API pages")
        verified[ident] = build
    return list(verified.values())


def choose_build_number(builds):
    # Also step above dotted numeric versions (23.1 -> 24), so an integer build
    # cannot compare below an already uploaded numeric version on this train.
    values = [str(item.get("attributes", {}).get("version", "")) for item in builds]
    leading = [int(value.split(".")[0]) for value in values
               if re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,2}", value)]
    return max(20, max(leading, default=19) + 1)


def prepare():
    sha = source_commit()
    app = app_id()
    builds = list_builds(app)
    build = str(choose_build_number(builds))
    data = {"bundle_id": BUNDLE, "version": VERSION, "build": build,
            "app_id": app, "source_commit": sha, "prepared": True,
            "existing_build_count": len(builds), "prepared_at": int(time.time()),
            "external_groups_changed": False, "app_store_submission": False}
    write_result(data)
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"build_number={build}\napp_id={app}\n")
    print(json.dumps(data), flush=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_ipa_info(info, build):
    if (info.get("CFBundleIdentifier") != BUNDLE
            or info.get("CFBundleShortVersionString") != VERSION
            or str(info.get("CFBundleVersion")) != build
            or info.get("CallWebhookPushEnvironment") != "production"):
        raise ReleaseError("IPA bundle, marketing version, build or production push setting does not match this release")


def validate_entitlements(entitlements, expected_team):
    team = entitlements.get("com.apple.developer.team-identifier", "")
    application = entitlements.get("application-identifier", "")
    application_prefix, separator, application_bundle = application.partition(".") if isinstance(application, str) else ("", "", "")
    if (not isinstance(team, str) or not re.fullmatch(r"[A-Z0-9]{10}", team)
            or team != expected_team
            or application_prefix != team or separator != "."
            or application_bundle.lower() != BUNDLE.lower()
            or entitlements.get("aps-environment") != "production"
            or entitlements.get("get-task-allow") is True
            or entitlements.get("beta-reports-active") is not True
            or entitlements.get("com.apple.developer.calling-app") is not True
            or entitlements.get("com.apple.developer.dialing-app") is not True
            or entitlements.get("com.apple.developer.carplay-communication") is not True):
        raise ReleaseError("IPA does not retain the expected distribution, team/app identity, push, calling, dialing and CarPlay entitlements")


def validate_profile(profile, expected_team):
    expiry = profile.get("ExpirationDate")
    if not isinstance(expiry, datetime):
        raise ReleaseError("The exported provisioning profile has no valid expiration date")
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if (expiry <= datetime.now(timezone.utc) or "ProvisionedDevices" in profile
            or profile.get("ProvisionsAllDevices") is True
            or expected_team not in profile.get("TeamIdentifier", [])):
        raise ReleaseError("The exported provisioning profile is expired or is not the expected App Store team profile")
    validate_entitlements(profile.get("Entitlements", {}), expected_team)
    return expiry.isoformat()


def verify_ipa():
    receipt = release_receipt()
    team = os.environ.get("APPLE_TEAM_ID", "")
    if not re.fullmatch(r"[A-Z0-9]{10}", team):
        raise ReleaseError("APPLE_TEAM_ID must identify the approved distribution signing team")
    ipa = Path(os.environ["TESTFLIGHT_IPA"]).resolve(strict=True)
    if not ipa.is_file():
        raise ReleaseError("TESTFLIGHT_IPA must identify the locally exported IPA")
    digest = sha256_file(ipa)
    with zipfile.ZipFile(ipa) as archive:
        for member in archive.infolist():
            path = PurePosixPath(member.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in member.filename:
                raise ReleaseError("Unsafe path in IPA archive")
            if stat.S_ISLNK(member.external_attr >> 16):
                target = archive.read(member).decode("utf-8")
                resolved = (ROOT / path.parent / target).resolve()
                if target.startswith("/") or not resolved.is_relative_to(ROOT.resolve()):
                    raise ReleaseError("Unsafe symbolic link in IPA archive")
        plists = [name for name in archive.namelist()
                  if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", name)]
        if len(plists) != 1:
            raise ReleaseError("Expected exactly one top-level iPhone app in the IPA")
        validate_ipa_info(plistlib.loads(archive.read(plists[0])), receipt["build"])
        app_relative = PurePosixPath(plists[0]).parent
    ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="verified-", dir=ROOT) as temporary:
        try:
            subprocess.run(["ditto", "-x", "-k", str(ipa), temporary],
                           check=True, capture_output=True)
            app = Path(temporary) / app_relative
            subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)],
                           check=True, capture_output=True)
            entitlements = plistlib.loads(subprocess.check_output(
                ["codesign", "-d", "--entitlements", ":-", str(app)], stderr=subprocess.DEVNULL))
            profile = plistlib.loads(subprocess.check_output(
                ["security", "cms", "-D", "-i", str(app / "embedded.mobileprovision")], stderr=subprocess.DEVNULL))
        except (subprocess.CalledProcessError, OSError, plistlib.InvalidFileException):
            raise ReleaseError("Local IPA extraction, code signature or entitlement decoding failed") from None
        validate_entitlements(entitlements, team)
        expiry = validate_profile(profile, team)
    if sha256_file(ipa) != digest:
        raise ReleaseError("The exported IPA changed during verification")
    record(ipa_verified=True, ipa_sha256=digest, ipa_path=str(ipa),
           profile_verified=True, profile_expires_at=expiry)


def find_build(app, build_number):
    builds = list_builds(app, build_number)
    if len(builds) > 1:
        raise ReleaseError("Apple returned more than one exact app/version/build match")
    return builds[0] if builds else None


def pause_polling():
    time.sleep(min(POLL_SECONDS, remaining_time()))


def internal_groups(app):
    groups = all_data("/v1/betaGroups?" + urllib.parse.urlencode({
        "filter[app]": app, "limit": "200"}))
    return [group for group in groups if group.get("type") == "betaGroups"
            and group.get("attributes", {}).get("isInternalGroup") is True]


def usable_groups(internal, build):
    # Apple accepts a single relationship filter: query app and builds
    # separately, then intersect IDs. Do not combine both filters in one call.
    linked = all_data("/v1/betaGroups?" + urllib.parse.urlencode({
        "filter[builds]": build, "limit": "200"}))
    linked_ids = {resource_id(group.get("id")) for group in linked}
    return [group for group in internal if resource_id(group.get("id")) in linked_ids
            or group.get("attributes", {}).get("hasAccessToAllBuilds") is True]


def summary(data):
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return
    with open(target, "a") as output:
        output.write(f"## CallWebhook {VERSION} ({data['build']})\n\n"
            f"Source: `{data['source_commit']}`\n\n"
            f"Verified IPA SHA-256: `{data.get('ipa_sha256', 'unverified')}`\n\n"
            f"Apple build: `{data.get('apple_build_id', 'not confirmed')}`\n\n"
            f"Processing: **{data.get('apple_processing_state', 'not confirmed')}**; "
            f"internal testing: **{data.get('internal_build_state', 'not confirmed')}**; "
            f"available internal groups: **{data.get('internal_groups_available', 0)}**.\n\n"
            "This release was built from the source commit above and includes landline mailbox "
            "transcription and automatic refresh. Device transcription quality still requires a real recording test. "
            "No App Store submission, new tester, new group or external-group change was made.\n")


def complete():
    global _deadline
    _deadline = time.monotonic() + PROCESSING_TIMEOUT
    try:
        receipt = release_receipt(require_ipa=True)
        # The verification job receives this receipt as an artifact from the
        # successful upload job. It deliberately does not rebuild/download an IPA.
        app = app_id()
        if app != receipt["app_id"]:
            raise ReleaseError("Apple app identity changed after release preparation")
        last_state = None
        while True:
            build = find_build(app, receipt["build"])
            state = build.get("attributes", {}).get("processingState") if build else "NOT_VISIBLE_YET"
            if state != last_state:
                record(apple_processing_state=state, apple_build_id=build["id"] if build else None)
                last_state = state
            if state in ("FAILED", "INVALID"):
                raise ReleaseError("Apple rejected build processing: " + state)
            if state == "VALID":
                if build.get("attributes", {}).get("expired") is True:
                    raise ReleaseError("The matching Apple build is expired")
                break
            pause_polling()
        ident = resource_id(build["id"])
        record(upload_confirmed=True, apple_build_id=ident, apple_processing_state="VALID")
        internal = internal_groups(app)
        record(existing_internal_group_count=len(internal))
        usable = usable_groups(internal, ident)
        if not usable:
            if len(internal) != 1:
                raise ReleaseError("No uniquely identifiable existing internal group can receive this build")
            group_id = resource_id(internal[0]["id"])
            # Re-read type and ownership immediately before this sole mutation.
            current = api(f"/v1/betaGroups/{group_id}?include=app")["data"]
            if (current.get("type") != "betaGroups" or current.get("id") != group_id
                    or current.get("attributes", {}).get("isInternalGroup") is not True
                    or relationship_id(current, "app") != app):
                raise ReleaseError("The existing beta group is no longer internal to the exact CallWebhook app")
            api(f"/v1/builds/{ident}/relationships/betaGroups",
                {"data": [{"type": "betaGroups", "id": group_id}]}, method="POST")
            record(linked_existing_internal_group=group_id)
        while True:
            usable = usable_groups(internal, ident)
            detail = api(f"/v1/builds/{ident}/buildBetaDetail")["data"]["attributes"]
            state = detail.get("internalBuildState")
            record(internal_build_state=state, internal_groups_available=len(usable))
            if usable and state in ("IN_BETA_TESTING", "READY_FOR_BETA_TESTING"):
                record(testflight_available=True, external_groups_changed=False, app_store_submission=False)
                summary(json.loads(RESULT.read_text()))
                return
            if state in ("EXPIRED", "MISSING_EXPORT_COMPLIANCE", "INVALID", "FAILED"):
                raise ReleaseError("Apple internal TestFlight state requires attention: " + state)
            pause_polling()
    finally:
        _deadline = None


def main():
    stages = {"prepare": prepare, "verify_ipa": verify_ipa, "complete": complete}
    if len(sys.argv) != 2 or sys.argv[1] not in stages:
        raise ReleaseError("Usage: mailbox_testflight.py prepare|verify_ipa|complete")
    stages[sys.argv[1]]()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        message = safe_message(error)
        try:
            record(error=message, testflight_available=False)
        except Exception:
            pass
        print("::error::" + message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"), file=sys.stderr)
        sys.exit(1)
