# Fresh-install phone setup

- The last HA step creates a persistent `input_boolean.iphone_call_active` through HA's authenticated helper WebSocket API, or reuses the existing entity. The registry's actual entity ID is saved; no temporary REST state or hard-coded webhook is used. Administrator access is required for creation.
- CallMonitor uses the configured HA instance, Keychain token, token refresh and input_boolean services. ON/OFF writes are serialized. Existing helpers are not reset during discovery.
- Lines 1 and 2 each store mobile number, provider, SIM UUID and confirmation. Line 3 is landline only. Providers include current and legacy retail names; custom entries cover providers absent from the catalog. Brand selection does not imply tariff support or identify a SIM.
- Unconditional forwarding uses `**21*<international destination>#`. An optional area code completes FRITZ numbers supplied without one. Invalid/injected numbers and mobile/service destinations are rejected. The originating SIM supplies the source mobile number; it is not encoded in MMI.
- The user explicitly selects the SIM, reviews source/destination/code and starts the action. A successful API return is not a network confirmation. Changing any assignment invalidates the user's activation confirmation. Prepaid restrictions and charges depend on the tariff.
- Apple exposes no public calling/dialing default-status API in the inspected SDK. Startup therefore opens the official Default Apps settings URL and asks the user to verify both roles. SIM availability is shown separately, never as proof of default status.
- Carrier codes bypass SIP. If a default-calling intent comes back to the app, a separate carrier-code sheet offers Apple's explicit-action `telephony:` fallback. iOS/carrier MMI rejection offers copying the code. Actual network activation requires device testing.

Sources checked 2026-09-28:
- https://developer.apple.com/documentation/livecommunicationkit/preparing-your-app-to-be-the-default-dialer-app
- https://developer.apple.com/documentation/callkit/preparing-your-app-to-be-the-default-calling-app
- https://developer.apple.com/documentation/uikit/uiapplication/category
- https://developer.apple.com/documentation/uikit/uiapplication/opendefaultapplicationssettingsurlstring
- https://www.telekom.de/hilfe/mobilfunk/telefonie-nachrichten/anrufweiterleitung
- https://www.vodafone.de/hilfe/mobiles-telefonieren-surfen.html
- https://www.o2online.de/ratgeber/hacks-tipps/rufumleitung-iphone/
- https://www.o2online.de/content/dam/o2/documents/preislisten/o2-prepaid-preisliste-mobilfunk.pdf
- https://github.com/home-assistant/core/blob/dev/homeassistant/components/input_boolean/__init__.py
