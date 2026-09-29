# FRITZ!Box answering-machine number assignment

Verified against the manufacturer's FRITZ!OS 8.25 firmware for 5690 Pro:
https://download.avm.de/fritzbox/fritzbox-5690-pro/deutschland/fritz.os/FRITZ.Box_5690_Pro-08.25.image

The vendor's `fon_devices/edit_tam.lua` handles `apply` through the WebGUI.
`lua/fon_devices_html.lua` maps the real values from `num_<slot>` checkboxes
into the shared TAM MSN table and the selected TAM's MSN bitmap.
`num_selection=sel_nums` selects specific numbers. This is a write path,
although the documented TR-064 TAM service has no corresponding number setter.

The app sends line numbers and selected TAM indexes to HA. HA obtains a WebGUI
session through DeviceConfig:X_AVM-DE_CreateUrlSID using the existing FRITZ!
credentials. The `NewX_AVM-DE_UrlSID` reply may be the bare query assignment
`sid=<16 hex digits>`; it is not necessarily a complete URL. Only the SID is
extracted and the configured router remains the request destination. Missing,
zero or ambiguous session IDs stop the write without claiming a permissions cause.
It reads the actual edit form, retains its successful controls,
selects matching phone-number values, and submits through `data.lua` with
`page=edit_tam`. No index is substituted for a telephone number.

The native save handler also writes unrelated recording, remote-access, mail
and timer settings. Therefore the complete existing form is preserved and
`timer:settings/TamTimerXML<index>` is read through authenticated `query.lua`.
Its existing schedule items are submitted in the native timer field format.
Missing/unrecognized forms, numbers or timer data stop the write. A pending
physical confirmation is polled through the native `twofactor.lua` session;
no operation is resubmitted as confirmed without the router confirming it.

Each saved TAM is independently checked using TR-064 GetInfo. HTTP 200 alone
is not success. Empty (all numbers), extra numbers, disabled TAMs or a mismatch
fail verification. HA stores the app's mapping only after all writes verify.
Partial writes are idempotent: a retry skips already matching TAMs.

Existing TAMs use the edit path above. For a fresh installation, selection -2
means “create for this line”. HA walks the native `assis/assi_tam_intern.lua`
wizard through `AssiTamInternEinrichten`, `AssiTamInternIncoming` and
`AssiTamInternSummary`, preserving successful controls on every page. It selects
the real number using its labelled `NewFnc_*` checkbox, requires the summary's
specific-number mode and corresponding number identifier, then submits
`Submit_Save` through `data.lua`. Router-advertised 2FA uses the same confirmed
flow. Before writing, the selected slot must have Display=0; after writing,
Display=1, enabled status and exact numbers are independently checked. New TAMs
record after 30 seconds, up to 180 seconds. Existing USB settings are retained.
The returned actual indexes replace -2 in the app and persisted HA assignments.
Retries reuse matching visible CallWebhook-named TAMs instead of duplicating them.
Contract tests use synthetic forms; live creation still needs device testing.


## Confirmation choices

The wizard displays router-advertised confirmation methods for each operation.
For TR-064 SIP changes, X_AVM-DE_Auth SetConfig supplies `button` and/or
`dtmf;*1…`. The full TR-064 dial string is displayed unchanged. TR-064 has no
Authenticator-code submission action; the UI explains this when applicable.
Reference: https://fritz.support/resources/TR-064_Authentication.pdf

For WebGUI TAM saves, the native `twofactor.js` / `twofactor.lua` contract offers
`button,dtmf,googleauth;<phone suffix>`. The WebGUI telephone code is `*1` plus
the supplied suffix. Authenticator is available only when both the pending
operation and `tfa_googleauth_info` advertise it for the current user. Six-digit
codes go to `tfa_googleauth`; even a successful submission does not complete
setup until `tfa_active` confirms `done` and `active`. Wrong codes can be retried.

HA exposes a short-lived confirmation ID and choices to the initiating admin.
Submissions require that owner and ID; codes stay in memory and are consumed
once. Cancellation/expiry clears pending data and cancels this WebGUI operation.
The wizard uses the same choice dialog for SIP and TAM requests. Bootstrap
installation remains automatic; manual repository/store emergency links were
removed from the setup UI.
