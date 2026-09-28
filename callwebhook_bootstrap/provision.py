"""Write Asterisk configuration through Supervisor's real addon-config mount."""
import json
import os
from pathlib import Path
import re


def provision(request_path, result_path, addon_root=Path('/addon_configs')):
    request_path, result_path = Path(request_path), Path(result_path)
    request = json.loads(request_path.read_text())
    job_id = request.get('job_id')
    result = {'job_id': job_id, 'ok': False}
    try:
        addon = request.get('addon', '')
        if not re.fullmatch(r'[a-z0-9]+_asterisk', addon):
            raise ValueError('Ungültiger Asterisk-Slug')
        # Never manufacture a shadow /addon_configs tree in the wrong container.
        if not addon_root.is_dir() or not (addon_root / addon).is_dir():
            raise ValueError('Asterisk-Konfiguration ist nicht eingebunden')
        target = addon_root / addon / 'asterisk' / 'custom'
        target.mkdir(parents=True, exist_ok=True)
        if not target.resolve().is_relative_to(addon_root.resolve()):
            raise ValueError('Unzulässiger Konfigurationspfad')
        contents = {'pjsip.conf': request.get('pjsip'), 'extensions.conf': request.get('extensions')}
        if any(not isinstance(value, str) or not value.strip() for value in contents.values()):
            raise ValueError('Asterisk-Konfiguration fehlt')
        if '[callwebhook-ios]' not in contents['pjsip.conf'] or '[from-callwebhook-ios]' not in contents['extensions.conf']:
            raise ValueError('CallWebhook-Konfiguration unvollständig')
        backups = {}
        try:
            for name, value in contents.items():
                path = target / name
                if path.is_symlink():
                    raise ValueError('Konfigurationsdatei darf kein Symlink sein')
                backups[path] = path.read_bytes() if path.exists() else None
                if backups[path] is not None:
                    backup = path.with_suffix('.conf.bak')
                    backup.write_bytes(backups[path])
                    backup.chmod(0o600)
                temporary = path.with_suffix('.conf.tmp')
                temporary.write_text(value.rstrip() + '\n')
                temporary.chmod(0o600)
                temporary.replace(path)
                if path.read_text() != value.rstrip() + '\n':
                    raise ValueError('Konfiguration konnte nicht verifiziert werden')
        except Exception:
            for path, previous in backups.items():
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(previous)
                    path.chmod(0o600)
            raise
        result.update(ok=True, files=[str(target / name) for name in contents], config_verified=True)
    except Exception as error:
        result['error'] = str(error)
    finally:
        request_path.unlink(missing_ok=True)
        temporary = result_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(result))
        temporary.chmod(0o600)
        temporary.replace(result_path)
    return result


if __name__ == '__main__':
    os.umask(0o077)
    provision('/homeassistant/callwebhook/provision-request.json', '/homeassistant/callwebhook/provision-result.json')
