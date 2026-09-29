# -CallWebhook
    CallWebhook

## Incoming calls and VoIP push

Incoming FRITZ!Box/easybell calls reach Asterisk, which invokes an authenticated,
per-installation HA hook before dialing the iPhone. HA sends a PushKit notification
through APNs. The app immediately reports CallKit, refreshes SIP registration and
acknowledges readiness. Asterisk then dials the new contact; its SIP header and
push payload share one call UUID. Declines, duplicate pushes and expired calls are
handled without presenting a second call. No setup-progress pushes are used.

The shared push relay is implemented under [`push_relay`](push_relay/README.md).
The app uses Apple App Attest to enroll automatically; each HA installation receives
only its own device-scoped credential. **The operator must deploy the relay and
set `CALLWEBHOOK_PUSH_RELAY_URL` before automatic background push can work.**
No deployed URL or working live APNs service is included in the repository.

The final assistant step automatically registers push and provisions the Asterisk
incoming route. Existing completed installations also attempt this on app start.
No Apple key file or Apple developer account is required from app users. When the
service is not yet available, the assistant reports it rather than pretending
incoming background calls are ready. Extras retains only the wizard restart entry,
not separate push/key-import/repair menus.

HA backend API 8 is required: CallWebhook Bootstrap installs it and restarts HA.
In relay mode `/config/callwebhook/voip.json` is created automatically with mode
0600 and contains the installation credential, not the Apple key. Existing direct
APNs credentials remain supported for private installations.

Test with a real incoming call while the iPhone is locked. The iPhone still needs
SIP/RTP access to Asterisk (home network or VPN). APNs acceptance alone is not a
delivery test. There is currently one iPhone per HA/Asterisk installation.

CI checks the signed `aps-environment` against the app configuration: development
for the direct IPA, production for TestFlight. When the old profile lacks push,
CI uses the existing App Store Connect API key to enable Push Notifications and
create/reuse a profile with the same certificates and devices, preserving the
calling/dialing entitlements. No existing profiles or certificates are revoked.
The API key needs Certificates, Identifiers & Profiles permission; an unavailable
permission is reported as a signing failure rather than silently removing push.
