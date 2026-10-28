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
For the operator's existing Home Assistant OS / Raspberry Pi, install the
[`CallWebhook Push-Dienst` add-on](callwebhook_push_relay/DOCS.md). The HA component
provides a narrow relay API over the existing Nabu Casa HTTPS connection; app
users do not install this operator add-on.

The final assistant step automatically registers push and provisions the Asterisk
incoming route. Existing completed installations also attempt this on app start.
No Apple key file or Apple developer account is required from app users. When the
service is not yet available, the assistant reports it rather than pretending
incoming background calls are ready. Extras retains only the wizard restart entry,
not separate push/key-import/repair menus.

The FRITZ step asks for one, two or three CallWebhook LAN phones before provisioning.
On an empty router, each active line defaults to creating its own answering machine
when continuing from the phone-line step. Existing answering machines can still
be selected. Creation uses the router wizard and verifies the real number.

HA backend API 16 is required: CallWebhook Bootstrap installs it and restarts HA.
It adds one inbound-only PJSIP endpoint matched to the configured FRITZ!Box host,
so incoming calls also work when the router omits the registration's `line`
parameter. All three outbound registrations and iPhone authentication are preserved.
Existing routes without this migration are marked pending and reprovisioned by
push setup after the backend update. A real external call is still required to
verify ringing and audio on the device.
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

The final wizard step can provision the operator relay on the existing HA Pi: verified repository discovery, installation, one-time APNs import, automatic startup and public reachability/device enrollment checks. Existing operator credentials are reused. Other users use the published relay URL and never install the operator service.

Operator iPhone enrollment uses the authenticated connection to its own HA after
verifying the local/public relay identity and health; unrelated installations keep
using the public HTTPS service. Enrollment/TLS failures are retained in the final
function test instead of being replaced by a stored configuration flag. Error
messages distinguish HA relay enrollment from Apple App Attest.

TAM assignments are now written through FRITZ!OS's authenticated `edit_tam`
WebGUI handler and independently read back through TR-064. The real form provides
number field names/values and all existing options; the stored timer is preserved.
The assistant handles the physical FRITZ!Box confirmation when requested and
reports a failed write or mismatched readback instead of accepting all numbers.
No firmware, raw router configuration or existing recordings are replaced.

Der Einrichtungsassistent startet den Bootstrap nach erfolgreicher HA-Autorisierung automatisch, wenn das Backend fehlt oder veraltet ist. Nach dem HA-Neustart setzt er Asterisk fort; ein Wiederholungsbutton erscheint nur bei Fehlern. Anruf-Push unterscheidet jetzt Sendefehler, fehlende iPhone-Bestätigung und bestätigte Bereitschaft. Die Hangup-Route endet explizit, damit sie keine neuen Push-Anrufe auslöst.

## Vollständige HA-Deinstallation

Im App-Store des vorhandenen Repositorys **CallWebhook vollständig entfernen** installieren und starten. Das eigenständige Werkzeug entfernt CallWebhook, Bootstrap, Asterisk und den lokalen Push-Dienst samt Konfiguration und Standard-Anrufstatus-Helfer, startet HA wieder und deinstalliert sich selbst. Details und Löschumfang: [Anleitung](callwebhook_cleanup/DOCS.md).

## FRITZ!Box credentials in Home Assistant

The setup assistant writes the FRITZ!Box credentials entered in its first step
to `/config/secrets.yaml` after the HA component is ready, and refreshes them
before mailbox assignment. Only `fritz_callwebhook_user` and
`fritz_callwebhook_password` are replaced; other entries are preserved.
The authenticated, admin-only `POST /api/callwebhook/setup/fritz-credentials`
endpoint accepts `username` and `password`, writes atomically with mode 0600,
and returns no credentials. The backend reads these values on demand, so no
HA restart is needed after saving. This requires backend API 18 and an app
build containing the credential synchronization.

## Continuous setup assistant

Setup remains on one scrolling page with a progress bar. It verifies FRITZ!Box
credentials, signs in to Home Assistant, then asks for one, two or three
CallWebhook LAN phones. Confirming the count starts device discovery and missing
SIP/TAM provisioning, preserving the FRITZ!Box second-factor dialog.
Each active line's number and answering machine must be explicitly confirmed.
Changing either selection invalidates that confirmation. After confirmations
and mobile forwarding are complete, Bootstrap installs/updates automatically;
HA readiness is checked before credentials and number assignments are saved,
Asterisk is provisioned, and the call helper and incoming push are configured.
Successful discoveries use matching green status rows. Technical slot/service
listings and separate Asterisk fallback controls are no longer shown. A failed
automatic setup can be retried without recreating already verified devices.


## Parallele Festnetzanrufe auf iPhone und Mac

Ab iPhone-Build 16 verarbeitet `SIPService` die Anrufzustände über direkte Linphone-Callbacks bei explizit deaktivierter automatischer Iteration; die bestehende Main-Runloop iteriert den Core weiterhin alle 20 ms. Dadurch wird das Rufende auch dann verarbeitet, wenn `currentCall` nach einem SIP-CANCEL bereits leer ist. Der eingehende CallKit-Ruf wird bei `End`/`Error` anhand seiner UUID sofort beendet. Seine endgültige lokale Verlaufsklassifizierung wird erst bei `Released` gespeichert. Signalisierung über `AcceptedElsewhere`, SIP Reason 200 oder Q.850 cause 26 wird als „Auf anderem Gerät angenommen“ an CallKit und den lokalen Verlauf weitergegeben. Ein bloßer CANCEL/487 oder normale Rufbeendigung bleibt ohne Annahmenachweis verpasst. Lokales Ablehnen und bereits verbundene Gespräche behalten ihren Status. Der getrennte ausgehende Mobilfunkanruf, etwa bei einem Selbsttest von SIM 1 auf die Festnetznummer, wird nicht verändert.

Die iPhone-Anpassung wurde nach ausdrücklicher Aufhebung der ursprünglichen Vorgabe „iPhone unverändert“ ergänzt. Der Mac-Telefoniecode und die Live-Asterisk-Konfiguration werden dadurch nicht verändert. Frühere falsche Verlaufseinträge werden ohne zuverlässigen Nachweis nicht nachträglich umklassifiziert.

Validierung der Korrektur:

- iOS-Gerätebuild mit Xcode 27.1 und Linphone iOS SDK 5.4.128 erfolgreich; Entwicklungssignatur umfasst Push, Default Calling App und Default Dialer App.
- Sechs lokale SIP-Tests kompilieren den echten iPhone-`SIPService` mit dem macOS-Linphone-SDK und Plattform-Doubles für CallKit, Mobilfunk und Schlüsselbund. Sie prüfen Q.850/26 und SIP/200 als anderswo angenommen; CANCEL ohne Reason, Q.850/16 und SIP/487 als verpasst; lokales Ablehnen als abgelehnt. Die Tests prüfen auch, dass die Anrufanzeige zum sofortigen Beenden aufgefordert wird, bevor der Verlauf endgültig gespeichert wird. Das ist kein echter CallKit-/iPhone-Klingeltest.
- Zusätzliche Swift-Tests prüfen Verlauf, Badge, lokale Beendigung und bereits verbundene/ausgehende Gespräche; vorhandene Verlauf-/Badge-Tests und 68 Backend-Tests bestehen.

```sh
swiftc CallWebhook/LocalCallHistory.swift CallWebhook/MissedCallState.swift \
  CallWebhook/SIPCallTermination.swift tests/SIPCallTerminationTests.swift -o /tmp/sip-end-tests
/tmp/sip-end-tests
python3 macOS/bootstrap_sdk.py
xcodegen generate --spec tests/SIPServiceRegression/project.yml
xcodebuild -project tests/SIPServiceRegression/SIPServiceRegression.xcodeproj \
  -scheme SIPServiceRegression -configuration Debug -destination 'platform=macOS' \
  -derivedDataPath work/ring-build CODE_SIGNING_ALLOWED=NO build
python3 tests/SIPServiceRegression/run.py
```

Beim lokalen Build war das Linphone-GitLab nicht erreichbar. Deshalb wurde ausschließlich für diesen Build ein temporäres XcodeGen-Projekt mit dem unveränderten offiziellen SwiftPM-Paket von `https://download.linphone.org/releases/ios/spm/linphone-sdk-swift-ios-5.4.128/` verwendet. SwiftPM prüft dessen Binär-Prüfsummen; Quellenprüfsummen liegen lokal unter `work/ios-sdk/source-sha256.json`. Die reguläre Paketquelle in `project.yml` bleibt unverändert.

Build 16 wurde auf dem verbundenen iPhone als Update derselben Bundle-ID erfolgreich installiert. Noch am echten Anschluss zu bestätigen: iPhone klingelt nach Annahme am Mac nicht weiter; „Auf anderem Gerät angenommen“ erscheint im iPhone-Verlauf und verursacht keinen Verpasst-Badge; echte Nichtannahme bleibt verpasst. Dies sowohl vom selben iPhone als auch von einem anderen Telefon und mit gesperrtem iPhone prüfen. Diagnoseeinträge `incoming_attached`, `terminal_reason` und `callkit_ended` unter `de.reno.CallWebhook` / `IncomingCall` enthalten Ruf-UUIDs und Statuscodes, keine Rufnummern oder Zugangsdaten.
