#!/usr/bin/env python3
"""Produce reviewable config copies. Never writes to HA or runs Asterisk."""
import argparse
import getpass
import os
from pathlib import Path
import re

MARKER = '; CallWebhook macOS line 3'


def prepare(pjsip, extensions, password):
    if not re.fullmatch(r'[A-Za-z0-9_-]{24,128}', password):
        raise ValueError('Use a separate 24–128 character random Mac password (letters/digits/_/-).')
    if MARKER in pjsip or MARKER in extensions or '[callwebhook-mac]' in pjsip:
        raise ValueError('Mac config already exists. Start from the unmodified backup.')
    # Require the current repository-generated configuration; never guess at another setup.
    if '[fritz3-endpoint]' not in pjsip or '[transport-udp]' not in pjsip:
        raise ValueError('Expected existing FRITZ line 3 endpoint and UDP transport.')
    old = ' same => n,Dial(${PJSIP_DIAL_CONTACTS(callwebhook-ios)},60,b(callwebhook-push-header^s^1(${CW_ID}^${CW_LINE})))'
    if extensions.count(old) != 1 or '[callwebhook-push-header]' not in extensions:
        raise ValueError('Expected the current push dialplan exactly once; generate it with iPhone setup first.')
    # Dial both registered contacts in one Dial(). Asterisk cancels the other leg on answer.
    # Empty contacts must never produce an invalid leading/trailing ampersand.
    replacement = ''' same => n,Set(CW_TARGETS=${PJSIP_DIAL_CONTACTS(callwebhook-ios)})
 same => n,Set(CW_MAC=)
 same => n,ExecIf($["${CW_LINE}"="3"]?Set(CW_MAC=${PJSIP_DIAL_CONTACTS(callwebhook-mac)}))
 same => n,ExecIf($["${CW_TARGETS}"!="" & "${CW_MAC}"!=""]?Set(CW_TARGETS=${CW_TARGETS}&${CW_MAC}):Set(CW_TARGETS=${CW_TARGETS}${CW_MAC}))
 same => n,GotoIf($["${CW_TARGETS}"=""]?done)
 same => n,Dial(${CW_TARGETS},60,Q(ANSWERED_ELSEWHERE)b(callwebhook-push-header^s^1(${CW_ID}^${CW_LINE})))'''
    pjsip += f'''
{MARKER}
[callwebhook-mac-auth]
type=auth
auth_type=userpass
username=callwebhook-mac
password={password}

[callwebhook-mac]
type=aor
max_contacts=1
remove_existing=yes

[callwebhook-mac]
type=endpoint
transport=transport-udp
context=from-callwebhook-mac
disallow=all
allow=alaw,ulaw
auth=callwebhook-mac-auth
aors=callwebhook-mac
identify_by=username,auth_username
direct_media=no
force_rport=yes
rewrite_contact=yes
rtp_symmetric=yes
'''
    extensions = extensions.replace(old, replacement) + f'''
{MARKER}
[from-callwebhook-mac]
; No include of iPhone/mobile contexts. Every permitted destination uses fritz3.
exten => _X.,1,Dial(PJSIP/${{EXTEN}}@fritz3-endpoint,60)
 same => n,Hangup()
exten => _**X.,1,Dial(PJSIP/${{EXTEN}}@fritz3-endpoint,60)
 same => n,Hangup()
'''
    return pjsip, extensions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pjsip', type=Path, required=True)
    parser.add_argument('--extensions', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='New directory; never a live Asterisk directory')
    args = parser.parse_args()
    password = getpass.getpass('Separate Mac SIP password: ')
    pjsip, extensions = prepare(args.pjsip.read_text(), args.extensions.read_text(), password)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, content in [('pjsip.conf', pjsip), ('extensions.conf', extensions)]:
        descriptor = os.open(args.output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as file:
            file.write(content)
    print('Prepared copies only. Review the diff before installing; keep originals for rollback.')


if __name__ == '__main__':
    main()
