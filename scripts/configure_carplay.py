"""Copy optional capabilities only from Apple's actual signing profile."""
import plistlib
import sys
from pathlib import Path

OPTIONAL = ("com.apple.developer.carplay-communication", "com.apple.developer.siri")

def configure(profile, entitlements):
    result = dict(entitlements)
    granted = profile.get("Entitlements", {})
    for key in OPTIONAL:
        result.pop(key, None)
        if granted.get(key) is True:
            result[key] = True
    return result

if __name__ == "__main__":
    profile_path, output_path = map(Path, sys.argv[1:])
    profile = plistlib.loads(profile_path.read_bytes())
    entitlements = plistlib.loads(output_path.read_bytes())
    result = configure(profile, entitlements)
    output_path.write_bytes(plistlib.dumps(result))
    for key in OPTIONAL:
        print(f"{key}: {'enabled by Apple profile' if result.get(key) else 'pending profile approval'}")
