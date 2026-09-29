"""Keep existing certificates/devices; add APNs and create/reuse a matching profile.

Uses the existing App Store Connect CI key. Never revokes/deletes profiles or keys.
"""
import base64
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

BUNDLE = "de.reno.CallWebhook.U98PKCA4W7"
API = "https://api.appstoreconnect.apple.com"


def decode_profile(content):
    with tempfile.NamedTemporaryFile() as file:
        file.write(content)
        file.flush()
        return plistlib.loads(subprocess.check_output(["security", "cms", "-D", "-i", file.name], stderr=subprocess.DEVNULL))


def valid(profile, environment):
    ent = profile.get("Entitlements", {})
    return (ent.get("aps-environment") == environment
            and ent.get("application-identifier", "").lower().endswith("." + BUNDLE.lower())
            and ent.get("com.apple.developer.calling-app") is True
            and ent.get("com.apple.developer.dialing-app") is True)


def jwt_token():
    def b64(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=")
    header = b64(json.dumps({"alg": "ES256", "kid": os.environ["ASC_KEY_ID"], "typ": "JWT"}).encode())
    now = int(time.time())
    payload = b64(json.dumps({"iss": os.environ["ASC_ISSUER_ID"], "iat": now, "exp": now + 600, "aud": "appstoreconnect-v1"}).encode())
    signing = header + b"." + payload
    with tempfile.NamedTemporaryFile(mode="w") as file:
        file.write(os.environ["ASC_PRIVATE_KEY"])
        file.flush()
        der = subprocess.check_output(["openssl", "dgst", "-sha256", "-sign", file.name], input=signing, stderr=subprocess.DEVNULL)
    # P-256 DER signature: SEQUENCE(INTEGER r, INTEGER s); each is at most 33 bytes.
    if der[0] != 0x30 or der[2] != 0x02:
        raise RuntimeError("Invalid signing key")
    length = der[3]
    r = int.from_bytes(der[4:4+length], "big")
    offset = 4 + length
    if der[offset] != 0x02:
        raise RuntimeError("Invalid signing key")
    s = int.from_bytes(der[offset+2:offset+2+der[offset+1]], "big")
    return (signing + b"." + b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))).decode()


def main(path, environment):
    target = Path(path)
    original = decode_profile(target.read_bytes())
    if valid(original, environment):
        print("Existing signing profile includes APNs.")
        return
    token = jwt_token()

    def api(path, payload=None):
        url = path if path.startswith(API + "/") else API + path
        request = Request(url, data=None if payload is None else json.dumps(payload).encode(),
                          headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            # Do not print request headers or authentication material.
            raise RuntimeError(f"Apple signing API HTTP {error.code}. API key needs Certificates, Identifiers & Profiles access.") from None

    def all_data(path):
        result = []
        while path:
            page = api(path)
            result.extend(page["data"])
            path = page.get("links", {}).get("next")
        return result

    bundles = [item for item in api("/v1/bundleIds?filter[identifier]=" + BUNDLE)["data"]
               if item["attributes"]["identifier"].lower() == BUNDLE.lower()
               and item["attributes"].get("platform") in ("IOS", "UNIVERSAL")]
    if len(bundles) != 1:
        # Apple can normalize identifier case; never create a different app ID.
        available = all_data("/v1/bundleIds?limit=200")
        bundles = [item for item in available if item["attributes"]["identifier"].lower() == BUNDLE.lower()
                   and item["attributes"].get("platform") in ("IOS", "UNIVERSAL")]
        if len(bundles) != 1:
            # Profiles provide an authoritative relationship even when a filtered
            # identifier lookup omits an Xcode-created identifier.
            matches = [item for item in all_data("/v1/profiles?limit=200")
                       if item["attributes"].get("uuid") == original["UUID"]]
            if len(matches) == 1:
                related = api(f"/v1/profiles/{matches[0]['id']}/bundleId")["data"]
                if related["attributes"]["identifier"].lower() == BUNDLE.lower():
                    bundles = [related]
        if len(bundles) != 1:
            print("Apple API visible bundle count:", len(available))
            print("CallWebhook identifiers visible to this key:", [item["attributes"]["identifier"] for item in available if "callwebhook" in item["attributes"]["identifier"].lower()])
    if len(bundles) != 1:
        raise RuntimeError("CallWebhook bundle ID could not be uniquely matched to this profile. Profile identifier: " + original.get("Entitlements", {}).get("application-identifier", "unknown"))
    bundle_id = bundles[0]["id"]
    capabilities = all_data(f"/v1/bundleIds/{bundle_id}/bundleIdCapabilities?limit=200")
    if not any(item["attributes"]["capabilityType"] == "PUSH_NOTIFICATIONS" for item in capabilities):
        api("/v1/bundleIdCapabilities", {"data": {
            "type": "bundleIdCapabilities", "attributes": {"capabilityType": "PUSH_NOTIFICATIONS"},
            "relationships": {"bundleId": {"data": {"type": "bundleIds", "id": bundle_id}}}}})
    profiles = all_data(f"/v1/bundleIds/{bundle_id}/profiles?limit=200")
    source = next((item for item in profiles if item["attributes"].get("uuid") == original["UUID"]), None)
    if source is None:
        raise RuntimeError("Original signing profile not found in Apple account; update provisioning profile secret")
    certificates = all_data(f"/v1/profiles/{source['id']}/certificates?limit=200")
    devices = all_data(f"/v1/profiles/{source['id']}/devices?limit=200")
    source_certs = {item["id"] for item in certificates}
    source_devices = {item["id"] for item in devices}
    name = "CallWebhook-VoIP-" + environment
    for item in profiles:
        attributes = item["attributes"]
        if attributes["name"] != name or attributes["profileState"] != "ACTIVE":
            continue
        content = base64.b64decode(attributes["profileContent"])
        if not valid(decode_profile(content), environment):
            continue
        certs = {entry["id"] for entry in all_data(f"/v1/profiles/{item['id']}/certificates?limit=200")}
        devs = {entry["id"] for entry in all_data(f"/v1/profiles/{item['id']}/devices?limit=200")}
        if certs == source_certs and devs == source_devices:
            target.write_bytes(content)
            print("Reusing APNs profile with original certificates and devices.")
            return
    relationships = {
        "bundleId": {"data": {"type": "bundleIds", "id": bundle_id}},
        "certificates": {"data": [{"type": "certificates", "id": item["id"]} for item in certificates]},
    }
    if devices:
        relationships["devices"] = {"data": [{"type": "devices", "id": item["id"]} for item in devices]}
    created = api("/v1/profiles", {"data": {"type": "profiles", "attributes": {
        "name": name, "profileType": source["attributes"]["profileType"]}, "relationships": relationships}})["data"]
    content = base64.b64decode(created["attributes"]["profileContent"])
    if not valid(decode_profile(content), environment):
        raise RuntimeError("New Apple profile does not include APNs and both existing calling entitlements")
    target.write_bytes(content)
    print("APNs profile created; existing profiles and certificates preserved.")


if __name__ == "__main__":
    try:
        main(sys.argv[1], sys.argv[2])
    except Exception as error:
        print("::error::Push signing setup failed: " + str(error), file=sys.stderr)
        sys.exit(1)
