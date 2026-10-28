#!/usr/bin/env python3
"""Fetch pinned official SDK sources. SwiftPM verifies binary checksums in Package.swift."""
import hashlib
from pathlib import Path
import subprocess

BASE = 'https://download.linphone.org/releases/macos/sdk/spm/linphone-sdk-swift-macos-5.4.128/'
FILES = {
    'Package.swift': 'f496e3cc47de5132d2450a7417a41170e7ad93e12b9b4ec4dac103d599dac2b6',
    'Sources/linphonesw/LinphoneSdkInfos.swift': 'f07fced559a71fc4c08b880c645f2df528bdb82d0b6fa39a7427f6b01eb4b66f',
    'Sources/linphonesw/LinphoneWrapper.swift': '793d7065250172841d3b03f58ae56dcb888650cdad8123615ddd3e684c283e8d',
    'LICENSE.txt': '0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0',
}


def main():
    root = Path(__file__).parent / '.sdk/linphone'
    for name, digest in FILES.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
            continue
        data = subprocess.check_output(['curl', '-fsSL', '--max-time', '120', BASE + name])
        if hashlib.sha256(data).hexdigest() != digest:
            raise RuntimeError('Official SDK checksum mismatch: ' + name)
        target.write_bytes(data)
    dummy = root / 'Sources/linphonexcframeworks/Dummy.swift'
    dummy.parent.mkdir(parents=True, exist_ok=True)
    dummy.write_text('')
    print('Linphone macOS SDK 5.4.128 sources verified.')


if __name__ == '__main__':
    main()
