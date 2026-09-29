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
credentials. It reads the actual edit form, retains its successful controls,
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

This path currently edits TAMs already visible in the FRITZ!Box. It does not
create hidden empty TAM slots by briefly enabling catch-all routing. Firmware
contract tests use synthetic forms; a live 5690 Pro save still needs device testing.
