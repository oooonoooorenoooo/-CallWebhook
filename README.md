# -CallWebhook
    CallWebhook

## Incoming calls and VoIP push

Incoming FRITZ!Box/easybell calls reach Asterisk, which invokes an authenticated,
per-installation HA hook before dialing the iPhone. HA sends a PushKit notification
through APNs. The app immediately reports CallKit, refreshes SIP registration and
acknowledges readiness. Asterisk then dials the new contact; its SIP header and
push payload share one call UUID. Declines, duplicate pushes and expired calls are
handled without presenting a second call. No setup-progress pushes are used.

One-time setup in **Extras → Anrufe im Hintergrund / VoIP-Push**:

1. Install/update the HA backend with the provided Bootstrap button and wait for
   HA to finish restarting. The existing Bootstrap downloads the updated backend
   and manifest; no wizard reset is needed.
2. If an APNs key is already saved on HA, use **Vorhandene Push-Einrichtung wieder aktivieren**. Token renewal preserves the existing key. Otherwise select an existing valid Apple **APNs** `.p8` key, its Key ID and Team ID. App Store Connect API
   keys cannot send APNs notifications. The APNs key stays on HA in
   `/config/callwebhook/voip.json` (permissions 0600), never in the app bundle/git.
3. Save and activate. This registers the iPhone and provisions the incoming
   Asterisk push route using the existing mailbox assignments and SIP accounts.
4. Verify a real incoming call with the iPhone locked. SIP/RTP still need access
   to Asterisk (home network or VPN). APNs acceptance alone is not a delivery test.

CI checks the signed `aps-environment` against the app configuration: development
for the direct IPA, production for TestFlight. When the old profile lacks push,
CI uses the existing App Store Connect API key to enable Push Notifications and
create/reuse a profile with the same certificates and devices, preserving the
calling/dialing entitlements. No existing profiles or certificates are revoked.
The API key needs Certificates, Identifiers & Profiles permission; an unavailable
permission is reported as a signing failure rather than silently removing push.
