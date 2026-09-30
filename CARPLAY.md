# CarPlay preparation — version 2.0 (2)

The approved telephone, SIP, push and audio implementation remains the baseline.
This change adds a CarPlay scene with contacts, recent local calls, explicit line
selection and call status/hangup. It reuses DialerModel; it does not change SIP,
RTP, CallKit audio ownership or Asterisk provisioning. Disconnecting CarPlay
never terminates the call. Contacts are read only with existing permission;
CarPlay cannot be used to complete setup or grant microphone access.

Explicit INStartCallIntent/INStartAudioCallIntent continuation now starts a
resolved call instead of merely filling the iPhone keypad. Multiple recipients,
names without a resolved telephone handle, video calls and carrier codes are
not silently dialed. Emergency numbers retain DialerModel's cellular routing.
The in-app Siri handler asks iOS to continue the call in the app. It must still
be verified on-device, including cold launch, locked phone and CarPlay-only launch.

## Apple prerequisite

Request Communication for de.reno.CallWebhook.U98PKCA4W7, team U98PKCA4W7.
After approval enable CarPlay Communication on that App ID, enable Siri, and
regenerate development and distribution provisioning profiles. Replace their
existing GitHub signing secrets. The build copies only capabilities actually
granted in the selected profile; it never forges an entitlement. Without the
CarPlay grant the compiled scene does not make the app appear on the car display.
Siri similarly needs a profile permitting the Siri entitlement.

## Acceptance tests after approval

- Connect CarPlay with the iPhone app closed; check contacts and recent calls.
- Select a contact/number, choose each configured line, confirm remote caller ID.
- Start a call through Siri with a resolved contact; check exactly one call.
- Test ambiguous contacts and missing permissions without unintended dialing.
- Test locked-phone outgoing initiation and incoming answer/decline.
- Verify system call UI, car buttons, microphone, speaker, audio interruption,
  Siri interaction and hangup. Current outgoing calls still use the existing
  DialerModel audio path rather than CXStartCallAction: do not claim full CarPlay
  CallKit compliance until these behaviors are verified and, if required, a
  separately reviewed outgoing CallKit adapter is implemented.
- Unplug CarPlay during a call; verify the call survives.
- Confirm emergency routing only through safe simulation; do not place test
  calls to emergency services.

No CarPlay device test or Apple approval is implied by a successful CI build.
