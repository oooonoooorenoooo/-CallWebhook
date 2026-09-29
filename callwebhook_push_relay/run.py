"""Read the operator's one-time add-on options without logging any key material."""
import json
import os
from pathlib import Path
import re


def normalize_key(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("APNs-Schlüssel fehlt oder ist ungültig")
    value = value.replace("\\n", "\n").strip()
    match = re.fullmatch(r"-----BEGIN PRIVATE KEY-----\s*([A-Za-z0-9+/=\s]+)\s*-----END PRIVATE KEY-----", value)
    if not match:
        raise ValueError("Den vollständigen Inhalt der APNs-.p8-Datei eintragen")
    body = re.sub(r"\s", "", match[1])
    normalized = "-----BEGIN PRIVATE KEY-----\n" + "\n".join(body[i:i+64] for i in range(0, len(body), 64)) + "\n-----END PRIVATE KEY-----\n"
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from cryptography.hazmat.primitives.asymmetric import ec
    try:
        key = load_pem_private_key(normalized.encode(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError()
    except Exception:
        raise ValueError("Ungültiger APNs-Schlüssel: unverschlüsselte P-256-.p8-Datei erforderlich") from None
    return normalized


def configure(options, data_dir):
    values = {}
    for field in ("apns_team_id", "apns_key_id"):
        value = options.get(field, "").strip()
        if not re.fullmatch(r"[A-Z0-9]{10}", value):
            raise ValueError(f"{field}: zehnstellige Apple-ID eintragen")
        values[field.upper()] = value
    key = normalize_key(options.get("apns_private_key", ""))
    path = data_dir / "apns.p8"
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(key)
    os.chmod(path, 0o600)
    values.update(APNS_KEY_FILE=str(path), RELAY_DATABASE=str(data_dir / "relay.sqlite3"))
    return values


if __name__ == "__main__":
    os.umask(0o077)
    try:
        environment = configure(json.loads(Path("/data/options.json").read_text()), Path("/data"))
    except (ValueError, TypeError, AttributeError):
        raise SystemExit("Push-Dienst nicht gestartet: APNs-Team-ID, Key-ID und gültigen .p8-Inhalt in der Add-on-Konfiguration hinterlegen. Ein App-Store-Connect-Schlüssel genügt nicht.") from None
    os.environ.update(environment)
    os.execvp("python", ["python", "-m", "push_relay.server"])
