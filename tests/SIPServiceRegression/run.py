"""Exercise the real iPhone SIPService over loopback using the macOS SDK transport."""
import importlib.util
from pathlib import Path

root = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('sip_peer', root / 'macOS/tests/SIPRegression/run.py')
peer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(peer)
peer.APP = root / 'work/ring-build/Build/Products/Debug/SIPServiceRegression.app/Contents/MacOS/SIPServiceRegression'
for reason, expected in [
    ('Q.850;cause=26;text="Answered elsewhere"', 'answered_elsewhere'),
    ('SIP;cause=200;text="Call completed elsewhere"', 'answered_elsewhere'),
    ('', 'missed'),
    ('Q.850;cause=16;text="Normal clearing"', 'missed'),
    ('SIP;cause=487;text="Request terminated"', 'missed'),
]:
    peer.run(reason, expected, requires_view=False)
peer.run('', 'declined', decline=True, requires_view=False)
print('Six iPhone SIPService loopback regression cases passed')
