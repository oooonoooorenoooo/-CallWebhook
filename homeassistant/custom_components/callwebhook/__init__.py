from pathlib import Path
from datetime import datetime
import asyncio
import json
import hashlib
import os
import secrets
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, parse_qs, quote

import requests
from aiohttp import web, ClientSession, ClientTimeout

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import CoreState, HomeAssistant

DOMAIN = "callwebhook"
BACKEND_API_VERSION = 18
BACKEND_BOOT_ID = secrets.token_hex(16)

HOST = "192.168.178.1"
DEFAULT_TAMS = ("1", "2")
REFRESH_SECONDS = 30

BASE_DIR = Path("/config/callwebhook")
MAILBOX_FILE = BASE_DIR / "mailbox.json"
XML_FILE = BASE_DIR / "mailbox.xml"
ARCHIVE_DIR = BASE_DIR / "archive"
ARCHIVE_FILE = ARCHIVE_DIR / "archive.json"
SETUP_FILE = BASE_DIR / "setup.json"
SECRETS_FILE = Path("/config/secrets.yaml")
ASTERISK_ADDON = "b35499aa_asterisk"
requested_dir = Path("/addon_configs/b35499aa_asterisk/asterisk/custom")

_refresh_lock = asyncio.Lock()
_asterisk_setup_state = {"state": "idle", "message": "Noch nicht gestartet", "progress_step": 0, "progress_total": 7, "result": None}

async def _run_asterisk_setup(hass, payload):
    global _asterisk_setup_state
    try:
        _asterisk_setup_state = {"state": "running", "message": "Asterisk-Repository und Store werden geprüft …", "progress_step": 0, "progress_total": 7, "result": None}
        def report_progress(step, message):
            _asterisk_setup_state["progress_step"] = step
            _asterisk_setup_state["message"] = message
        deadline = time.monotonic() + 300
        while True:
            readiness = await setup_readiness(hass)
            if readiness["ready_for_asterisk"]:
                break
            report_progress(0, readiness["message"])
            if time.monotonic() >= deadline:
                raise RuntimeError(readiness["message"] + " – Bereitschaft nach 5 Minuten nicht bestätigt")
            await asyncio.sleep(0.5)
        actual_addon = await hass.async_add_executor_job(ensure_asterisk_addon, report_progress)
        _asterisk_setup_state["progress_step"] = 5
        _asterisk_setup_state["message"] = "Asterisk läuft – Konfiguration wird geschrieben …"
        actual_path = f"/addon_configs/{actual_addon}/asterisk/custom"
        pjsip = fritz_incoming_pjsip(payload.get("pjsip"))
        extensions = voip_dialplan(payload.get("extensions") or "")
        push_route_applied = f"/api/callwebhook/voip/hook/{_voip.get('hook_secret', '')}" in extensions
        files = await hass.async_add_executor_job(install_asterisk_config, pjsip, extensions, actual_addon, actual_path)
        _asterisk_setup_state["progress_step"] = 6
        configured_tams = await hass.async_add_executor_job(save_setup, payload.get("mailbox_tam_1"), payload.get("mailbox_tam_2"), payload.get("mailbox_tam_3"))
        _asterisk_setup_state["message"] = "Asterisk-Konfiguration geschrieben – Neustart läuft …"
        await hass.services.async_call("hassio", "addon_restart", {"addon": actual_addon}, blocking=True)
        _asterisk_setup_state["progress_step"] = 6
        _asterisk_setup_state["message"] = "Asterisk wird neu gestartet und abschließend geprüft …"
        await hass.async_add_executor_job(wait_for_asterisk_started, actual_addon)
        async with _voip_lock:
            value = dict(_voip, route_ready=push_route_applied and voip_configured(), incoming_route_revision=3)
            await hass.async_add_executor_job(save_voip, value)
            _voip.update(value)
        _asterisk_setup_state["progress_step"] = 7
        result = {"addon": actual_addon, "files": files, "config_verified": True, "mailbox_tams": configured_tams}
        _asterisk_setup_state = {"state": "done", "message": "Asterisk installiert, gestartet und konfiguriert", "progress_step": 7, "progress_total": 7, "result": result}
    except Exception as error:
        _asterisk_setup_state = {"state": "error", "message": str(error), "progress_step": _asterisk_setup_state.get("progress_step", 0), "progress_total": 7, "result": None}



def get_configured_tams():
    if SETUP_FILE.exists():
        try:
            data = json.loads(SETUP_FILE.read_text(encoding="utf-8"))
            if "tams" in data:
                values = data.get("tams", [])
                return tuple(str(value) for value in values if str(value).isdigit() and int(value) >= 0)
        except Exception as error:
            print(f"CallWebhook Setup konnte nicht gelesen werden: {error}")
    return DEFAULT_TAMS


def save_setup(mailbox_tam_1, mailbox_tam_2, mailbox_tam_3=None):
    values = []
    for value in (mailbox_tam_1, mailbox_tam_2, mailbox_tam_3):
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number >= 0 and number not in values:
            values.append(number)
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    temp = BASE_DIR / "setup.json.tmp"
    temp.write_text(json.dumps({"tams": values, "assignments": {f"mailbox_tam_{index}": value for index, value in enumerate((mailbox_tam_1, mailbox_tam_2, mailbox_tam_3), 1)}}, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(SETUP_FILE)
    return values


def read_secret(path, name):
    import yaml
    text = Path(path).read_text(encoding="utf-8")
    root = yaml.compose(text)
    if isinstance(root, yaml.MappingNode):
        matches = [value for key, value in root.value if key.value == name]
        if len(matches) == 1 and isinstance(matches[0], yaml.ScalarNode):
            return matches[0].value
    raise ValueError("FRITZ!Box-Zugangsdaten in Home Assistant fehlen oder sind mehrdeutig")


def save_fritz_credentials(path, username, password):
    import yaml
    import tempfile
    KEYS = ("fritz_callwebhook_user", "fritz_callwebhook_password")
    if any(not isinstance(v, str) or not v or len(v) > 1024 or "\x00" in v for v in (username, password)):
        raise ValueError("FRITZ!Box-Benutzer und Kennwort fehlen oder sind ungültig")
    path = Path(path)
    if path.is_symlink():
        raise ValueError("secrets.yaml ist ein Symlink und wird nicht überschrieben")
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = before.splitlines(keepends=True)
    spans = []
    try:
        root = yaml.compose(before)
        if root is not None and (not isinstance(root, yaml.MappingNode) or root.flow_style):
            raise ValueError("secrets.yaml muss ein YAML-Mapping in Blockform enthalten")
        for key, value in root.value if root else []:
            if key.value not in KEYS:
                continue
            if value.start_mark.index < key.start_mark.index:
                raise ValueError("Verknüpfte Zugangsdaten müssen zuerst aufgelöst werden")
            start, end = key.start_mark.line, value.end_mark.line
            if end < len(lines) and lines[end][:value.end_mark.column].strip():
                end += 1
            spans.append((start, max(start + 1, end)))
        remaining = "".join(line for i, line in enumerate(lines) if not any(a <= i < b for a, b in spans))
        if remaining and not remaining.endswith("\n"):
            remaining += "\n"
        result = remaining + "".join(key + ": " + json.dumps(value, ensure_ascii=False) + "\n"
                                     for key, value in zip(KEYS, (username, password)))
        yaml.compose(result)  # Refuse removal of an anchor used by other secrets.
    except yaml.YAMLError:
        raise ValueError("secrets.yaml konnte nicht sicher bearbeitet werden") from None
    descriptor, temporary = tempfile.mkstemp(prefix=".callwebhook-secrets-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(result)
            output.flush()
            os.fsync(output.fileno())
        if path.is_symlink() or (path.read_text(encoding="utf-8") if path.exists() else "") != before:
            raise ValueError("secrets.yaml wurde gleichzeitig geändert; erneut versuchen")
        os.replace(temporary, path)  # mkstemp creates mode 0600.
    finally:
        Path(temporary).unlink(missing_ok=True)
    if [read_secret(path, key) for key in KEYS] != [username, password]:
        raise ValueError("Gespeicherte FRITZ!Box-Zugangsdaten konnten nicht bestätigt werden")


def get_secret(name):
    return read_secret(SECRETS_FILE, name)


def get_auth():
    return requests.auth.HTTPDigestAuth(
        get_secret("fritz_callwebhook_user"),
        get_secret("fritz_callwebhook_password")
    )


def get_text(message, name):
    value = message.findtext(name)

    if value is None:
        return ""

    return value.strip()


def sort_key(item):
    try:
        return datetime.strptime(
            item.get("date", ""),
            "%d.%m.%y %H:%M"
        )
    except Exception:
        return datetime.min


def tam_control_request(action, body):
    service = (
        "urn:dslforum-org:"
        "service:X_AVM-DE_TAM:1"
    )

    control_url = (
        f"http://{HOST}:49000/"
        "upnp/control/x_tam"
    )

    soap = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope
 xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
 s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body>
<u:{action} xmlns:u="{service}">
{body}
</u:{action}>
</s:Body>
</s:Envelope>"""

    response = requests.post(
        control_url,
        data=soap.encode("utf-8"),
        headers={
            "Content-Type":
                'text/xml; charset="utf-8"',
            "SOAPAction":
                f'"{service}#{action}"',
        },
        auth=get_auth(),
        timeout=10,
    )

    response.raise_for_status()

    return response


def delete_fritz_message(tam, index):
    response = tam_control_request(
        "DeleteMessage",
        (
            f"<NewIndex>{tam}</NewIndex>"
            f"<NewMessageIndex>{index}</NewMessageIndex>"
        )
    )

    audio_file = (
        BASE_DIR /
        f"voicemail_{tam}_{index}.wav"
    )

    if audio_file.exists():
        try:
            audio_file.unlink()
        except Exception as error:
            print(
                "CallWebhook lokale WAV konnte "
                f"nicht entfernt werden: {error}"
            )

    return response.status_code


def load_archive():
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    if not ARCHIVE_FILE.exists():
        return []
    try:
        data = json.loads(ARCHIVE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as error:
        print(f"CallWebhook Archiv konnte nicht gelesen werden: {error}")
        return []


def save_archive(archive):
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    temp_file = ARCHIVE_DIR / "archive.json.tmp"
    temp_file.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_file.replace(ARCHIVE_FILE)


def archive_message(tam, index):
    if not MAILBOX_FILE.exists():
        raise RuntimeError("Mailbox-Datei nicht vorhanden")
    mailbox = json.loads(MAILBOX_FILE.read_text(encoding="utf-8"))
    message = next((item for item in mailbox if str(item.get("tam", "")) == str(tam) and str(item.get("index", "")) == str(index)), None)
    if message is None:
        raise RuntimeError("Nachricht nicht gefunden")
    source_audio = ensure_message_audio(tam, index)
    if source_audio is None:
        raise RuntimeError("Aufnahme nicht vorhanden")
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archive_audio = ARCHIVE_DIR / f"voicemail_{tam}_{index}.wav"
    if not archive_audio.exists():
        temp_audio = ARCHIVE_DIR / f"voicemail_{tam}_{index}.wav.tmp"
        temp_audio.write_bytes(source_audio.read_bytes())
        temp_audio.replace(archive_audio)
    archive = load_archive()
    archived_message = dict(message)
    archived_message["archived"] = True
    archived_message["new"] = False
    archived_message["audio"] = f"/api/callwebhook/archive/audio/{tam}/{index}"
    replaced = False
    for position, item in enumerate(archive):
        if str(item.get("tam", "")) == str(tam) and str(item.get("index", "")) == str(index):
            archive[position] = archived_message
            replaced = True
            break
    if not replaced:
        archive.append(archived_message)
    archive.sort(key=sort_key, reverse=True)
    save_archive(archive)
    return archived_message


def merge_mailbox_and_archive(mailbox):
    archive = load_archive()
    archived_ids = {(str(item.get("tam", "")), str(item.get("index", ""))) for item in archive}
    live = []
    for item in mailbox:
        key = (str(item.get("tam", "")), str(item.get("index", "")))
        if key not in archived_ids:
            live_item = dict(item)
            live_item["archived"] = False
            live.append(live_item)
    combined = archive + live
    combined.sort(key=sort_key, reverse=True)
    return combined

def fetch_fritz_mailbox_for_tam(target_tam, download_index=None):
    BASE_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    service = (
        "urn:dslforum-org:"
        "service:X_AVM-DE_TAM:1"
    )

    response = tam_control_request(
        "GetMessageList",
        f"<NewIndex>{target_tam}</NewIndex>"
    )

    soap_root = ET.fromstring(
        response.content
    )

    message_url = None

    for element in soap_root.iter():
        if element.tag.endswith("NewURL"):
            message_url = element.text
            break

    if not message_url:
        raise RuntimeError(
            "FRITZ!Box hat keine "
            "MessageList-URL geliefert"
        )

    messages_response = requests.get(
        message_url,
        auth=get_auth(),
        timeout=10,
    )

    messages_response.raise_for_status()

    temp_xml = XML_FILE.with_suffix(
        ".xml.tmp"
    )

    temp_xml.write_text(
        messages_response.text,
        encoding="utf-8"
    )

    temp_xml.replace(
        XML_FILE
    )

    message_root = ET.fromstring(
        messages_response.content
    )

    sid = parse_qs(
        urlparse(
            message_url
        ).query
    ).get(
        "sid",
        [None]
    )[0]

    messages = message_root.findall(
        ".//Message"
    )

    mailbox = []
    valid_audio_files = set()

    for message in messages:
        index = get_text(
            message,
            "Index"
        )

        tam = get_text(
            message,
            "Tam"
        )

        called = get_text(
            message,
            "Called"
        )

        date = get_text(
            message,
            "Date"
        )

        duration = get_text(
            message,
            "Duration"
        )

        name = get_text(
            message,
            "Name"
        )

        number = get_text(
            message,
            "Number"
        )

        new_value = get_text(
            message,
            "New"
        )

        path = get_text(
            message,
            "Path"
        )

        if not index.isdigit():
            continue

        if not tam:
            tam = target_tam
        if not str(tam).isdigit():
            continue

        audio_api_path = ""

        filename = (
            f"voicemail_"
            f"{tam}_"
            f"{index}.wav"
        )

        audio_file = (
            BASE_DIR /
            filename
        )

        if path:
            # The list advertises a lazy audio endpoint. Download only the
            # selected message, never every recording before returning a list.
            audio_api_path = f"/api/callwebhook/audio/{tam}/{index}"
            if audio_file.exists():
                valid_audio_files.add(
                    filename
                )

                audio_api_path = (
                    "/api/callwebhook/"
                    f"audio/{tam}/{index}"
                )

            elif str(download_index) == index:
                if not sid:
                    raise RuntimeError("Keine SID in der MessageList-URL gefunden")
                recording_path = path

                prefix = (
                    "/download.lua?path="
                )

                if recording_path.startswith(
                    prefix
                ):
                    recording_path = (
                        recording_path[
                            len(prefix):
                        ]
                    )

                audio_url = (
                    f"http://{HOST}"
                    "/cgi-bin/"
                    "luacgi_notimeout"
                    f"?sid={quote(sid)}"
                    "&script=/lua/photo.lua"
                    "&myabfile="
                    f"{quote(recording_path, safe='/')}"
                )

                try:
                    audio = requests.get(
                        audio_url,
                        timeout=20
                    )

                    audio.raise_for_status()

                    content_type = (
                        audio.headers.get(
                            "Content-Type",
                            ""
                        )
                    )

                    if (
                        "audio"
                        not in
                        content_type.lower()
                    ):
                        raise RuntimeError(
                            "FRITZ!Box lieferte "
                            "keine Audiodatei"
                        )

                    temp_audio_file = (
                        BASE_DIR /
                        f"{filename}.tmp"
                    )

                    temp_audio_file.write_bytes(
                        audio.content
                    )

                    temp_audio_file.replace(
                        audio_file
                    )

                    valid_audio_files.add(
                        filename
                    )

                    audio_api_path = (
                        "/api/callwebhook/"
                        f"audio/{tam}/{index}"
                    )

                except Exception as error:
                    print(
                        "CallWebhook Audiofehler "
                        f"{tam}/{index}: {error}"
                    )

        mailbox.append(
            {
                "index": index,
                "tam": tam,
                "called": called,
                "date": date,
                "duration": duration,
                "name": name,
                "number": number,
                "new": new_value == "1",
                "audio": audio_api_path,
            }
        )

    mailbox.sort(
        key=sort_key,
        reverse=True
    )

    return mailbox


def fetch_fritz_mailbox():
    combined = []
    for target_tam in get_configured_tams():
        combined.extend(fetch_fritz_mailbox_for_tam(target_tam))
    combined.sort(key=sort_key, reverse=True)
    valid_audio_files = {
        f"voicemail_{item['tam']}_{item['index']}.wav"
        for item in combined
        if item.get("audio", "").startswith("/api/callwebhook/audio/")
        and str(item.get("tam", "")).isdigit() and str(item.get("index", "")).isdigit()
    }
    for audio_file in BASE_DIR.glob("voicemail_*.wav"):
        if audio_file.name not in valid_audio_files:
            try:
                audio_file.unlink()
            except Exception as error:
                print(f"CallWebhook konnte {audio_file.name} nicht entfernen: {error}")
    temp_json = BASE_DIR / "mailbox.json.tmp"
    temp_json.write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_json.replace(MAILBOX_FILE)
    return combined


def ensure_message_audio(tam, index):
    """Recover an evicted recording using a freshly issued FRITZ session URL."""
    if not str(tam).isdigit() or not str(index).isdigit():
        raise ValueError("Ungültige Nachrichtenkennung")
    audio_file = BASE_DIR / f"voicemail_{tam}_{index}.wav"
    if audio_file.exists():
        return audio_file
    messages = fetch_fritz_mailbox_for_tam(str(tam), str(index))
    message = next((item for item in messages if item['index'] == str(index) and item['tam'] == str(tam)), None)
    if message is None or not message.get('audio'):
        return None
    if not audio_file.exists():
        raise RuntimeError("FRITZ!Box-Aufnahme konnte nicht geladen werden")
    return audio_file


async def async_refresh_mailbox(
    hass: HomeAssistant
):
    async with _refresh_lock:
        try:
            return await hass.async_add_executor_job(
                fetch_fritz_mailbox
            )
        except Exception as error:
            print(
                "CallWebhook Mailbox-Refresh "
                f"fehlgeschlagen: {error}"
            )
            return None


async def mailbox_refresh_loop(
    hass: HomeAssistant
):
    while True:
        await async_refresh_mailbox(
            hass
        )

        await asyncio.sleep(
            REFRESH_SECONDS
        )



def ensure_asterisk_addon(progress=None):
    def progress_update(step, message):
        if progress:
            progress(step, message)
    host = os.environ.get("SUPERVISOR", "supervisor")
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError("Home Assistant Supervisor-Token ist intern nicht verfügbar")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    repository_url = "https://github.com/TECH7Fox/asterisk-hass-addons"

    store = supervisor_request("GET", "/store")
    def repository_ids(value):
        return {item["slug"] for item in value.get("repositories", [])
            if item.get("source", "").rstrip("/").removesuffix(".git").lower() == repository_url.lower()}
    if not repository_ids(store):
        supervisor_request("POST", "/store/repositories", {"repository": repository_url}, timeout=120)
    progress_update(1, "Asterisk-Repository bestätigt – Store wird geprüft …")
    addon_slug = None
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        store = supervisor_request("GET", "/store")
        repositories = repository_ids(store)
        for item in store.get("addons", store.get("apps", [])):
            if item.get("repository") in repositories and item.get("slug") == item.get("repository", "") + "_asterisk":
                addon_slug = item["slug"]
                break
        if addon_slug:
            break
        time.sleep(0.5)
    if not addon_slug:
        raise RuntimeError("Asterisk wurde im bestätigten TECH7Fox-Repository noch nicht gefunden")
    progress_update(2, "Asterisk im Store gefunden – Installation wird geprüft …")

    store_info_url = f"http://{host}/store/addons/{addon_slug}"
    availability = requests.get(f"{store_info_url}/availability", headers=headers, timeout=30)
    if availability.status_code >= 400:
        raise RuntimeError(f"Asterisk ist auf diesem Home-Assistant-System nicht installierbar: HTTP {availability.status_code} – {availability.text}")

    store_info = requests.get(store_info_url, headers=headers, timeout=30)
    if store_info.status_code >= 400:
        raise RuntimeError(f"Asterisk-Store-Status konnte nicht gelesen werden: HTTP {store_info.status_code} – {store_info.text}")
    try:
        store_data = store_info.json().get("data", {})
    except ValueError:
        store_data = {}
    installed_version = store_data.get("installed")

    if not installed_version:
        install = requests.post(
            f"{store_info_url}/install",
            headers=headers,
            json={"background": False},
            timeout=300,
        )
        if install.status_code >= 400:
            raise RuntimeError(f"Asterisk konnte nicht installiert werden: HTTP {install.status_code} – {install.text}")

        installed_version = None
        last_store_response = None
        for _ in range(240):
            last_store_response = requests.get(store_info_url, headers=headers, timeout=30)
            if last_store_response.status_code < 300:
                try:
                    current = last_store_response.json().get("data", {})
                    installed_version = current.get("installed")
                except ValueError:
                    installed_version = None
                if installed_version:
                    break
            time.sleep(0.5)
        if not installed_version:
            detail = "" if last_store_response is None else f"HTTP {last_store_response.status_code} – {last_store_response.text}"
            raise RuntimeError(f"Supervisor hat die Asterisk-Installation nicht bestätigt (installed ist leer): {detail}")
    progress_update(3, "Asterisk installiert – Pflichtkonfiguration wird gesetzt …")

    info_url = f"http://{host}/addons/{addon_slug}/info"

    # Preserve the add-on's complete default/current option set and fill required secrets.
    installed_info = requests.get(info_url, headers=headers, timeout=30)
    if installed_info.status_code >= 400:
        raise RuntimeError(f"Asterisk-Optionen konnten nicht gelesen werden: HTTP {installed_info.status_code} – {installed_info.text}")
    try:
        info_data = installed_info.json().get("data", {})
        current_options = dict(info_data.get("options") or {})
    except (ValueError, TypeError):
        info_data = {}
        current_options = {}
    if not current_options:
        current_options = {
            "ami_password": None,
            "auto_add": True,
            "auto_add_secret": "",
            "video_support": False,
            "register_ingress_entry": True,
            "generate_ssl_cert": True,
            "certfile": "fullchain.pem",
            "keyfile": "privkey.pem",
            "additional_sounds": [],
            "mailbox": False,
            "mailbox_port": 12345,
            "mailbox_password": "",
            "mailbox_extension": "100",
            "mailbox_google_api_key": "",
            "log_level": "info",
        }
    current_options["ami_password"] = current_options.get("ami_password") or secrets.token_urlsafe(32)
    # CallWebhook provisions its own PJSIP endpoints; TECH7Fox person auto-add and
    # ingress registration are optional and would make first start depend on HA API availability.
    current_options["auto_add"] = False
    current_options["register_ingress_entry"] = False
    current_options["auto_add_secret"] = ""
    options = requests.post(
        f"http://{host}/addons/{addon_slug}/options",
        headers=headers,
        json={"options": current_options},
        timeout=60,
    )
    if options.status_code not in (200, 201):
        raise RuntimeError(f"Asterisk wurde installiert, aber die Pflichtkonfiguration konnte nicht gesetzt werden: HTTP {options.status_code} – {options.text}")
    progress_update(4, "Asterisk konfiguriert – Add-on wird gestartet …")

    if info_data.get("state") != "started":
        supervisor_request("POST", f"/addons/{addon_slug}/start", {}, timeout=120)
    for _ in range(180):
        state = requests.get(info_url, headers=headers, timeout=30)
        if state.status_code < 300:
            try:
                data = state.json().get("data", {})
                current = str(data.get("state", "")).lower()
                if current == "started":
                    progress_update(5, "Asterisk gestartet – CallWebhook-Konfiguration folgt …")
                    return addon_slug
                if current in ("error", "failed"):
                    raise RuntimeError(f"Asterisk meldet nach dem Start den Zustand {current}")
            except ValueError:
                pass
        time.sleep(0.5)
    raise RuntimeError(f"Asterisk {installed_version} ist installiert, wurde aber innerhalb von 90 Sekunden nicht gestartet")

def supervisor_request(method, endpoint, payload=None, timeout=30):
    host = os.environ.get("SUPERVISOR", "supervisor")
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError("Supervisor-Token fehlt")
    response = requests.request(method, f"http://{host}{endpoint}",
        headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=timeout)
    response.raise_for_status()
    body = response.json()
    if body.get("result") == "error":
        raise RuntimeError(body.get("message", "Supervisor-Anfrage fehlgeschlagen"))
    return body.get("data", {})


def find_push_relay():
    """Resolve only this repository's installed relay, never a request URL."""
    import re
    installed = supervisor_request("GET", "/addons", timeout=5)
    candidates = [item.get("slug", "") for item in installed.get("addons", [])
                  if re.fullmatch(r"[a-z0-9]+_callwebhook_push_relay", item.get("slug", ""))]
    if not candidates:
        return None
    store = supervisor_request("GET", "/store", timeout=5)
    repositories = {item["slug"] for item in store.get("repositories", [])
        if item.get("source", "").rstrip("/").removesuffix(".git").lower()
        == "https://github.com/oooonoooorenoooo/-callwebhook"}
    candidates = [slug for slug in candidates if slug.removesuffix("_callwebhook_push_relay") in repositories]
    if len(candidates) != 1:
        return None
    slug = candidates[0]
    info = supervisor_request("GET", f"/addons/{slug}/info", timeout=5)
    if info.get("state") != "started":
        return None
    return "http://" + slug.replace("_", "-") + ":8080"


_relay_host_cache = (0, None)
_relay_host_lock = asyncio.Lock()


async def push_relay_target(hass):
    global _relay_host_cache
    async with _relay_host_lock:
        if _relay_host_cache[0] <= time.monotonic():
            try:
                target = await hass.async_add_executor_job(find_push_relay)
            except Exception:
                # A transient store/Supervisor failure must not disconnect an
                # already verified relay. Its own HTTP health remains decisive.
                target = _relay_host_cache[1]
            _relay_host_cache = (time.monotonic() + (300 if target else 30), target)
        return _relay_host_cache[1]


RELAY_PUBLIC_PREFIX = "/api/callwebhook/push-relay"
RELAY_PUBLIC_ROUTES = {("GET", "healthz"), ("POST", "v1/challenge"),
    ("POST", "v1/register"), ("GET", "v1/registration"),
    ("DELETE", "v1/registration"), ("POST", "v1/ring"),
    ("POST", "hatts/v1/register"), ("POST", "hatts/v1/speak"),
    ("POST", "comatalarm/v1/register"), ("POST", "comatalarm/v1/watch"),
    ("GET", "comatalarm/v1/state"), ("DELETE", "comatalarm/v1/registration"),
    ("POST", "comatalarm/v1/test"), ("GET", "comatalarm/v1/testflight/groups"), ("POST", "comatalarm/v1/testflight/testers")}


async def forward_push_relay(request, endpoint):
    import re
    if (request.method, endpoint) not in RELAY_PUBLIC_ROUTES or request.query_string:
        raise web.HTTPNotFound()
    headers = {"Content-Type": "application/json"}
    if endpoint.startswith("hatts/"):
        setup_key = request.headers.get("X-HATTS-Setup-Key", "")
        if not setup_key:
            raise web.HTTPUnauthorized()
        headers["X-HATTS-Setup-Key"] = setup_key
    if endpoint == "comatalarm/v1/register" or endpoint.startswith("comatalarm/v1/testflight/"):
        setup_key = request.headers.get("X-ComatAlarm-Setup-Key", "")
        if not 32 <= len(setup_key) <= 256:
            raise web.HTTPUnauthorized()
        headers["X-ComatAlarm-Setup-Key"] = setup_key
    if endpoint in ("v1/registration", "v1/ring") or (endpoint.startswith("comatalarm/") and endpoint != "comatalarm/v1/register"):
        authorization = request.headers.get("Authorization", "")
        if not re.fullmatch(r"Bearer [0-9a-f]{64}", authorization):
            raise web.HTTPUnauthorized()
        headers["Authorization"] = authorization
    body_limit = 131072 if endpoint == "comatalarm/v1/watch" else 32768
    if request.content_length is not None and request.content_length > body_limit:
        raise web.HTTPRequestEntityTooLarge(max_size=body_limit, actual_size=request.content_length)
    body = bytearray()
    async for chunk in request.content.iter_chunked(4096):
        body.extend(chunk)
        if len(body) > body_limit:
            raise web.HTTPRequestEntityTooLarge(max_size=body_limit, actual_size=len(body))
    target = await push_relay_target(request.app["hass"])
    if not target:
        return web.json_response({"ready": False, "error": "Push-Dienst-Add-on nicht gestartet"}, status=503)
    try:
        async with ClientSession(timeout=ClientTimeout(total=65 if endpoint.startswith("comatalarm/v1/testflight/") else 8)) as session:
            async with session.request(request.method, target + "/" + endpoint,
                    data=bytes(body) or None, headers=headers, allow_redirects=False) as response:
                result = await response.read()
                if len(result) > 65536 or 300 <= response.status < 400:
                    raise ValueError("Invalid relay response")
                return web.Response(body=result, status=response.status, content_type="application/json",
                                    headers={"Cache-Control": "no-store"})
    except Exception:
        return web.json_response({"ready": False, "error": "Push-Dienst nicht erreichbar"}, status=502)



_relay_setup_state = {"state": "idle", "progress_step": 0, "message": "Push-Dienst noch nicht eingerichtet"}


def relay_operator_credentials(payload):
    """Validate before mutating Supervisor; never include credentials in errors."""
    import re
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from cryptography.hazmat.primitives.asymmetric import ec
    values = {name: payload.get(name, "") for name in ("apns_team_id", "apns_key_id", "apns_private_key")}
    if not any(values.values()):
        return {}
    try:
        if any(not isinstance(value, str) for value in values.values()):
            raise ValueError()
        for name in ("apns_team_id", "apns_key_id"):
            values[name] = values[name].strip()
            if not re.fullmatch(r"[A-Z0-9]{10}", values[name]):
                raise ValueError()
        pem = values["apns_private_key"]
        if len(pem) > 4096:
            raise ValueError()
        key = load_pem_private_key(pem.encode(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError()
    except Exception:
        raise ValueError("Gültige APNs-.p8-Datei, Team-ID und Key-ID erforderlich") from None
    return values


def provision_push_relay(credentials, report):
    """Operator-only provisioning; the repository identity determines the slug."""
    import re
    repository = "https://github.com/oooonoooorenoooo/-CallWebhook"
    def repositories(store):
        return {r["slug"] for r in store.get("repositories", [])
                if r.get("source", "").rstrip("/").removesuffix(".git").lower() == repository.lower()}
    report(0, "Push-Repository wird geprüft …")
    store = supervisor_request("GET", "/store")
    if not repositories(store):
        supervisor_request("POST", "/store/repositories", {"repository": repository})
    supervisor_request("POST", "/store/reload", timeout=120)
    deadline = time.monotonic() + 90
    slug = None
    while time.monotonic() < deadline:
        store = supervisor_request("GET", "/store")
        valid = repositories(store)
        candidates = [a.get("slug", "") for a in store.get("addons", store.get("apps", []))
            if re.fullmatch(r"[a-z0-9]+_callwebhook_push_relay", a.get("slug", ""))
            and a["slug"].removesuffix("_callwebhook_push_relay") in valid]
        if len(candidates) == 1:
            slug = candidates[0]
            break
        time.sleep(0.5)
    if not slug:
        raise RuntimeError("Push-Dienst im richtigen Repository noch nicht verfügbar; erneut versuchen")
    report(1, "Push-Dienst wird installiert …")
    endpoint = f"/store/addons/{slug}"
    info = supervisor_request("GET", endpoint)
    if not info.get("installed") or info.get("update_available"):
        action = "update" if info.get("installed") else "install"
        supervisor_request("POST", endpoint + "/" + action, {"background": True})
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            info = supervisor_request("GET", endpoint)
            if info.get("installed") and not info.get("update_available"):
                break
            time.sleep(1)
        else:
            raise RuntimeError("Installation noch nicht fertig; Status später erneut prüfen")
    report(2, "Push-Dienst wird konfiguriert …")
    info = supervisor_request("GET", f"/addons/{slug}/info")
    options = dict(info.get("options", {}))
    if credentials:
        options.update(credentials)
    # Missing credentials are actionable, never falsely mark installation as ready.
    relay_operator_credentials(options)
    if not all(options.get(k) for k in ("apns_team_id", "apns_key_id", "apns_private_key")):
        raise ValueError("Einmalig den APNs-Schlüssel im Assistenten auswählen")
    changed = options != info.get("options", {})
    if changed or info.get("boot") != "auto":
        supervisor_request("POST", f"/addons/{slug}/options", {"options": options, "boot": "auto"})
    report(3, "Push-Dienst wird gestartet …")
    if changed and info.get("state") == "started":
        supervisor_request("POST", f"/addons/{slug}/restart", timeout=120)
    elif info.get("state") != "started":
        supervisor_request("POST", f"/addons/{slug}/start", timeout=120)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        info = supervisor_request("GET", f"/addons/{slug}/info")
        if info.get("state") == "started":
            try:
                health = requests.get("http://" + slug.replace("_", "-") + ":8080/healthz", timeout=3,
                                      allow_redirects=False)
                if health.status_code == 200 and health.json().get("ready") is True:
                    return
            except Exception:
                pass
        time.sleep(0.5)
    raise RuntimeError("Push-Dienst startet noch nicht; Konfiguration und Add-on-Protokoll prüfen")


async def run_push_relay_setup(hass, credentials):
    global _relay_host_cache
    def report(step, message):
        # All public state mutations run on HA's event loop, including thread progress.
        hass.loop.call_soon_threadsafe(_relay_setup_state.update,
            {"state": "running", "progress_step": step, "message": message})
    try:
        deadline = time.monotonic() + 300
        while not (await setup_readiness(hass))["ready_for_asterisk"]:
            _relay_setup_state["message"] = "Home Assistant und Bootstrap werden noch gestartet …"
            if time.monotonic() >= deadline:
                raise RuntimeError("Home Assistant ist noch nicht bereit; erneut versuchen")
            await asyncio.sleep(0.5)
        await hass.async_add_executor_job(provision_push_relay, credentials, report)
        _relay_host_cache = (0, None)
        _relay_setup_state.update(state="completed", progress_step=4,
            message="Push-Dienst läuft; öffentliche Erreichbarkeit und iPhone-Anmeldung werden geprüft")
    except ValueError as error:
        _relay_setup_state.update(state="error", message=str(error))
    except Exception:
        # Supervisor responses can include option values; do not expose or log them.
        _relay_setup_state.update(state="error", message="Push-Dienst konnte nicht eingerichtet werden. HA-Bereitschaft, Store und Add-on prüfen; anschließend erneut versuchen.")
    finally:
        credentials.clear()


class CallWebhookPushRelaySetupView(HomeAssistantView):
    url = RELAY_PUBLIC_PREFIX + "/setup"
    name = "api:callwebhook:push-relay:setup"
    requires_auth = True

    def require_admin(self, request):
        from homeassistant.components.http.const import KEY_HASS_USER
        user = request.get(KEY_HASS_USER)
        if not user or not user.is_admin:
            raise web.HTTPForbidden()

    async def get(self, request):
        self.require_admin(request)
        return self.json(dict(_relay_setup_state))

    async def post(self, request):
        self.require_admin(request)
        if _relay_setup_state["state"] == "running":
            return self.json(dict(_relay_setup_state))
        if request.content_length is None or request.content_length > 8192:
            raise web.HTTPRequestEntityTooLarge(max_size=8192, actual_size=request.content_length or 8193)
        payload = await request.json()
        if not isinstance(payload, dict) or payload.get("operator") is not True:
            return self.json({"message": "Nur für den Betreiber des gemeinsamen Push-Dienstes"}, status_code=400)
        try:
            credentials = relay_operator_credentials(payload)
        except ValueError as error:
            return self.json({"message": str(error)}, status_code=400)
        # Recheck after reading the body: two simultaneous POSTs must create one job.
        if _relay_setup_state["state"] == "running":
            credentials.clear()
            return self.json(dict(_relay_setup_state))
        _relay_setup_state.update(state="running", progress_step=0, message="Push-Dienst wird vorbereitet …")
        request.app["hass"].async_create_background_task(
            run_push_relay_setup(request.app["hass"], credentials), "CallWebhook Push setup")
        return self.json(dict(_relay_setup_state))


class CallWebhookPushRelayHostView(HomeAssistantView):
    url = RELAY_PUBLIC_PREFIX + "/host"
    name = "api:callwebhook:push-relay:host"
    requires_auth = True

    async def get(self, request):
        from homeassistant.helpers.network import get_url, NoURLAvailableError
        hass = request.app["hass"]
        target = await push_relay_target(hass)
        if not target:
            return self.json({"available": False, "message": "Betreiber-Add-on nicht gestartet"})
        try:
            public_url = get_url(hass, require_ssl=True, allow_internal=False,
                                 allow_ip=False, prefer_cloud=True).rstrip("/") + RELAY_PUBLIC_PREFIX
        except NoURLAvailableError:
            return self.json({"available": False, "operator": True, "message": "Öffentliche HTTPS-Adresse fehlt. Nabu-Casa-Fernzugriff einschalten."})
        local_ready = public_ready = False
        async with ClientSession(timeout=ClientTimeout(total=8)) as session:
            for url, local in ((target + "/healthz", True), (public_url + "/healthz", False)):
                try:
                    async with session.get(url, allow_redirects=False) as response:
                        ready = response.status == 200 and (await response.json()).get("ready") is True
                        if local:
                            local_ready = ready
                        else:
                            public_ready = ready
                except Exception:
                    pass
        return self.json({"available": local_ready and public_ready, "operator": True, "public_url": public_url,
            "local_ready": local_ready, "public_ready": public_ready,
            "message": "Push-Dienst öffentlich erreichbar" if local_ready and public_ready
                       else "Push-Dienst noch nicht öffentlich erreichbar. Add-on und Nabu-Casa-Fernzugriff prüfen."})


class CallWebhookPushRelayProxyView(HomeAssistantView):
    # No HA login is needed by other app installations. The relay verifies App
    # Attest proofs and device-scoped grants. This is not a general-purpose proxy.
    url = RELAY_PUBLIC_PREFIX + "/{endpoint:.*}"
    name = "api:callwebhook:push-relay:proxy"
    requires_auth = False

    async def get(self, request, endpoint):
        return await forward_push_relay(request, endpoint)

    async def post(self, request, endpoint):
        return await forward_push_relay(request, endpoint)

    async def delete(self, request, endpoint):
        return await forward_push_relay(request, endpoint)


def wait_for_asterisk_started(addon):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        info = supervisor_request("GET", f"/addons/{addon}/info")
        if info.get("state") == "started":
            return
        if info.get("state") in ("error", "failed"):
            raise RuntimeError("Asterisk konnte nach der Konfiguration nicht starten")
        time.sleep(0.5)
    raise RuntimeError("Asterisk-Neustart wurde nicht bestätigt")


def install_asterisk_config(pjsip, extensions, addon, custom_path):
    if not isinstance(pjsip, str) or not isinstance(extensions, str):
        raise RuntimeError("Asterisk-Konfiguration fehlt")
    store = supervisor_request("GET", "/store")
    repositories = {item["slug"] for item in store.get("repositories", [])
        if item.get("source", "").rstrip("/").removesuffix(".git") == "https://github.com/oooonoooorenoooo/-CallWebhook"}
    bootstrap = next((item for item in store.get("addons", store.get("apps", []))
        if item.get("repository") in repositories
        and item.get("slug") == item.get("repository", "") + "_callwebhook_bootstrap"), None)
    if not bootstrap or not bootstrap.get("installed"):
        raise RuntimeError("Bootstrap muss zum Schreiben der Asterisk-Konfiguration installiert sein")
    if bootstrap.get("update_available"):
        raise RuntimeError("Bootstrap bitte zuerst in der App aktualisieren")
    slug = bootstrap["slug"]
    # Bootstrap has the actual Supervisor all_addon_configs mount. Core does not.
    deadline = time.monotonic() + 60
    while supervisor_request("GET", f"/addons/{slug}/info").get("state") == "started":
        if time.monotonic() >= deadline:
            raise RuntimeError("Bootstrap führt noch einen Auftrag aus")
        time.sleep(0.5)
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    request_path = BASE_DIR / "provision-request.json"
    result_path = BASE_DIR / "provision-result.json"
    job_id = secrets.token_hex(16)
    result_path.unlink(missing_ok=True)
    temporary = request_path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"job_id": job_id, "addon": addon, "pjsip": pjsip, "extensions": extensions}))
    temporary.chmod(0o600)
    temporary.replace(request_path)
    supervisor_request("POST", f"/addons/{slug}/start", {}, timeout=120)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if result_path.exists():
            result = json.loads(result_path.read_text())
            if result.get("job_id") == job_id:
                if not result.get("ok") or not result.get("config_verified"):
                    raise RuntimeError(result.get("error", "Konfiguration konnte nicht verifiziert werden"))
                return result["files"]
        time.sleep(0.25)
    raise RuntimeError("Bootstrap hat das Schreiben der Asterisk-Konfiguration nicht bestätigt")


def bootstrap_state():
    # Installed add-ons are authoritative here; a store refresh is not needed
    # to tell whether the one-shot installer has finished its previous job.
    listing = supervisor_request("GET", "/addons", timeout=5)
    addons = listing.get("addons", listing.get("apps", []))
    candidates = [item for item in addons
        if str(item.get("slug", "")).endswith("_callwebhook_bootstrap")]
    if len(candidates) != 1:
        return "missing" if not candidates else "ambiguous"
    info = supervisor_request("GET", f"/addons/{candidates[0]['slug']}/info", timeout=5)
    return info.get("state", "unknown")


async def setup_readiness(hass):
    # hass.is_running also includes STARTING, so it is intentionally not used.
    if hass.state is not CoreState.running:
        return {"home_assistant_state": str(hass.state), "ready_for_asterisk": False,
                "bootstrap_state": "waiting", "message": "Home Assistant fährt noch hoch …"}
    try:
        state = await hass.async_add_executor_job(bootstrap_state)
    except Exception:
        return {"home_assistant_state": str(hass.state), "ready_for_asterisk": False,
                "bootstrap_state": "unknown", "message": "Home Assistant läuft – Bootstrap-Abschluss wird geprüft …"}
    ready = state == "stopped"
    messages = {
        "stopped": "Home Assistant vollständig gestartet und Bootstrap abgeschlossen",
        "started": "Home Assistant läuft – Bootstrap schließt den vorherigen Auftrag ab …",
        "missing": "Bootstrap ist nicht installiert – bitte Bootstrap einrichten",
        "error": "Bootstrap meldet einen Fehler – bitte Bootstrap-Protokoll prüfen",
    }
    return {"home_assistant_state": str(hass.state), "bootstrap_state": state,
            "ready_for_asterisk": ready,
            "message": messages.get(state, "Bootstrap-Abschluss wird geprüft …")}


class CallWebhookSetupStatusView(HomeAssistantView):
    url = "/api/callwebhook/setup/status"
    name = "api:callwebhook:setup:status"
    requires_auth = True

    async def get(self, request):
        readiness = await setup_readiness(request.app["hass"])
        return self.json({
            "ok": True,
            "boot_id": BACKEND_BOOT_ID,
            **readiness,
            "domain": DOMAIN,
            "api_version": BACKEND_API_VERSION,
            "asterisk_provisioning": True,
            "mailbox": True,
            "archive": True,
        })


# FRITZ!OS 8.25 edit_tam.lua uses num_<slot> values (real numbers),
# num_selection=sel_nums and apply. Read the complete form before posting because
# the same handler also saves recording, PIN, mail and calendar settings.
class FritzTAMConfigurationError(ValueError):
    pass


def fritz_tam_form(html, index, numbers, timer_xml):
    import re
    from html.parser import HTMLParser
    class Form(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.inside = False
            self.found = False
            self.fields = {}
            self.choices = {}
            self.select = None
            self.options = []
        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "form":
                self.inside = a.get("id") == "main_form"
                if self.inside:
                    if self.found or urlparse(a.get("action", "")).path != "/fon_devices/edit_tam.lua":
                        raise FritzTAMConfigurationError("Unbekanntes FRITZ!-Anrufbeantworterformular")
                    self.found = True
                return
            if not self.inside:
                return
            name = a.get("name", "")
            if tag == "input" and name:
                kind = a.get("type", "text").lower()
                if re.fullmatch(r"num_\d+", name) and "disabled" not in a:
                    self.choices[name] = a.get("value", "")
                if "disabled" in a or kind in ("submit", "button", "reset", "file", "image"):
                    return
                if kind in ("checkbox", "radio") and "checked" not in a:
                    return
                self.fields[name] = a.get("value", "on" if kind in ("checkbox", "radio") else "")
            elif tag == "select":
                self.select = name if name and "disabled" not in a else None
                self.options = []
            elif tag == "option" and self.select and "disabled" not in a:
                self.options.append((a.get("value", ""), "selected" in a))
        def handle_endtag(self, tag):
            if tag == "form":
                self.inside = False
            if tag == "select" and self.select:
                if self.options:
                    self.fields[self.select] = next((v for v, selected in self.options if selected), self.options[0][0])
                self.select = None
    form = Form()
    form.feed(html)
    fields = form.fields
    if not form.found or fields.get("TamNr") != str(index):
        raise FritzTAMConfigurationError("Anrufbeantworterformular oder Web-Anmeldung nicht bestätigt")
    required = ("tam_name", "call_delay", "rec_len", "operation_mode")
    if any(not fields.get(key) or fields[key] == "tochoose" for key in required):
        raise FritzTAMConfigurationError("Vorhandene Anrufbeantworter-Einstellungen nicht vollständig lesbar")
    normalize = lambda value: "".join(c for c in value if c.isdigit())
    expected = {normalize(value) for value in numbers}
    if not expected or any(len(value) < 3 for value in expected):
        raise FritzTAMConfigurationError("Echte Festnetznummern erforderlich")
    chosen = {key: value for key, value in form.choices.items() if normalize(value) in expected}
    if {normalize(value) for value in chosen.values()} != expected:
        raise FritzTAMConfigurationError("Gewählte Rufnummer fehlt im echten FRITZ!-Anrufbeantworterformular")
    fields = {key: value for key, value in fields.items() if not re.fullmatch(r"num_\d+", key)
              and not key.startswith("timer_")}
    fields.update(chosen)
    fields.update(num_selection="sel_nums", apply="", page="edit_tam", xhr="1", lang="de")
    # Preserve the raw stored timer rather than inventing a default schedule.
    if not isinstance(timer_xml, str) or len(timer_xml) > 65536 or "<!" in timer_xml:
        raise FritzTAMConfigurationError("Zeitplan konnte nicht sicher übernommen werden")
    if timer_xml.strip():
        try:
            root = ET.fromstring(timer_xml)
            if root.tag != "rule" or root.get("id") != str(index):
                raise ValueError()
            for n, item in enumerate(root):
                t, action, day = item.get("time", ""), item.get("action", ""), item.get("day", "")
                if (item.tag != "item" or not re.fullmatch(r"(?:[01]\d|2[0-3])[0-5]\d", t)
                        or action not in ("0", "1", "2", "3") or not day.isdigit() or not 1 <= int(day) <= 127):
                    raise ValueError()
                fields[f"timer_item_{n}"] = f"{t};{action};{day}"
        except Exception:
            raise FritzTAMConfigurationError("Zeitplanformat unbekannt; keine Änderung geschrieben") from None
    elif fields["operation_mode"] == "timectrl":
        raise FritzTAMConfigurationError("Aktiver Zeitplan fehlt; keine Änderung geschrieben")
    return fields


def fritz_web_sid():
    import re
    service = "urn:dslforum-org:service:DeviceConfig:1"
    action = "X_AVM-DE_CreateUrlSID"
    soap = f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><u:{action} xmlns:u="{service}"/></s:Body></s:Envelope>'
    response = requests.post(f"http://{HOST}:49000/upnp/control/deviceconfig", data=soap.encode(),
        headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": f'"{service}#{action}"'},
        auth=get_auth(), timeout=10)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    value = next((e.text for e in root.iter() if e.tag.split("}")[-1] == "NewX_AVM-DE_UrlSID"), "")
    value = (value or "").strip()
    # FRITZ!OS returns "sid=<hex>" here, not necessarily a complete URL.
    # Extract only the credential; never follow a host supplied in this value.
    query = value if value.startswith("sid=") else urlparse(value).query
    values = parse_qs(query, keep_blank_values=True).get("sid", [])
    if len(values) != 1 or not re.fullmatch(r"[0-9a-fA-F]{16}", values[0]) or values[0] == "0" * 16:
        raise FritzTAMConfigurationError("FRITZ!Box hat keine gültige Web-Sitzungskennung geliefert (CreateUrlSID)")
    return values[0]


def read_tam_info(index):
    response = tam_control_request("GetInfo", f"<NewIndex>{index}</NewIndex>")
    return {element.tag.split("}")[-1]: element.text or "" for element in ET.fromstring(response.content).iter()}


def tam_numbers_match(info, numbers):
    normalize = lambda value: "".join(c for c in value if c.isdigit())
    actual = {normalize(value) for value in info.get("NewPhoneNumbers", "").split(",")}
    expected = {normalize(value) for value in numbers}
    return bool(expected) and all(len(value) >= 3 for value in actual | expected) and actual == expected


class FritzConfirmation:
    """One short-lived, owner-bound confirmation; OTPs are never persisted."""
    def __init__(self):
        import threading
        self.lock = threading.Lock()
        self.owner = None
        self.pending = None
        self.command = None

    def begin(self, state, google):
        import re
        parts = state.split(";", 1)
        advertised = parts[0].split(",")
        methods = []
        if "button" in advertised:
            methods.append("button")
        phone = ""
        if "dtmf" in advertised and len(parts) == 2 and re.fullmatch(r"[0-9]{1,12}", parts[1]):
            phone = "*1" + parts[1]
            methods.append("phone")
        if "googleauth" in advertised and google.get("isAvailable") is True and google.get("isConfigured") is True:
            methods.append("otp")
        if not methods:
            raise FritzTAMConfigurationError("FRITZ!Box bietet keinen unterstützten Bestätigungsweg an")
        with self.lock:
            self.command = None
            self.pending = {"id": secrets.token_hex(16), "methods": methods, "phone_code": phone,
                            "error": "", "expires": time.monotonic() + 120}

    def snapshot(self):
        with self.lock:
            return {k:v for k,v in (self.pending or {}).items() if k != "expires"}

    def submit(self, owner, payload):
        import re
        with self.lock:
            if (not self.pending or owner != self.owner or payload.get("id") != self.pending["id"]
                    or time.monotonic() >= self.pending["expires"]):
                raise ValueError("Bestätigungsauftrag nicht mehr gültig")
            action = payload.get("action")
            code = payload.get("code", "")
            if action != "cancel" and (action != "otp" or "otp" not in self.pending["methods"]
                    or not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code)):
                raise ValueError("Sechsstelligen Authenticator-Code eingeben")
            if self.command is not None:
                raise ValueError("Bestätigung wird bereits geprüft")
            self.command = (action, code if action == "otp" else "")
            self.pending["error"] = ""

    def take(self):
        with self.lock:
            command, self.command = self.command, None
            return command

    def reject(self):
        with self.lock:
            if self.pending:
                self.pending["error"] = "Code nicht akzeptiert. Aktuellen Code eingeben oder anderen Bestätigungsweg wählen."

    def clear(self):
        with self.lock:
            self.pending = self.command = None


def wait_fritz_confirmation(web_request, state, confirmation):
    if "starterror" in state:
        raise FritzTAMConfigurationError("FRITZ!Box-Bestätigung belegt oder gesperrt; später erneut versuchen")
    google = {}
    success = False
    try:
        methods = state.split(";", 1)[0].split(",")
        if "googleauth" in methods:
            try:
                value = web_request("/twofactor.lua", {"tfa_googleauth_info": ""}, "POST").json()
                google = value.get("googleauth") or {}
                if not isinstance(google, dict):
                    raise ValueError("Invalid authenticator metadata")
            except Exception:
                # Optional authenticator metadata must not block the router's
                # independently advertised button/telephone confirmation.
                google = {}
                if "button" not in methods and "dtmf" not in methods:
                    raise FritzTAMConfigurationError("Authenticator-Bestätigung konnte nicht geladen werden; erneut versuchen") from None
        if confirmation:
            confirmation.begin(state, google)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            command = confirmation.take() if confirmation else None
            if command:
                action, code = command
                if action == "cancel":
                    raise FritzTAMConfigurationError("FRITZ!Box-Bestätigung abgebrochen")
                answer = web_request("/twofactor.lua", {"tfa_googleauth": code}, "POST").json()
                code = command = None
                if answer.get("err") != 0:
                    confirmation.reject()
            check = web_request("/twofactor.lua", {"tfa_active": ""}, "POST").json()
            if check.get("done") is True:
                if check.get("active") is not True:
                    raise FritzTAMConfigurationError("FRITZ!Box-Bestätigung abgebrochen")
                success = True
                return
            time.sleep(0.5)
        raise FritzTAMConfigurationError("Zeitüberschreitung bei der FRITZ!Box-Bestätigung")
    finally:
        if confirmation:
            confirmation.clear()
        if not success:
            try:
                web_request("/twofactor.lua", {"tfa_cancel": ""}, "POST")
            except Exception:
                pass


_tam_confirmation = FritzConfirmation()


def fritz_tam_wizard_form(html, stage):
    """Read the native wizard's successful controls and labelled number choices."""
    from html.parser import HTMLParser
    class Form(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.inside = False
            self.found = False
            self.fields, self.checks, self.labels = {}, {}, {}
            self.label = None
            self.text = []
            self.select = None
            self.options = []
        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "form":
                self.inside = a.get("name") == "mainform"
                if self.inside:
                    if self.found or urlparse(a.get("action", "")).path != "/assis/assi_tam_intern.lua":
                        raise FritzTAMConfigurationError("Unbekanntes FRITZ!-Neuanlageformular")
                    self.found = True
            if not self.inside:
                return
            name = a.get("name")
            if tag == "input" and name and "disabled" not in a:
                kind = a.get("type", "text").lower()
                if kind == "checkbox" and name.startswith("NewFnc_"):
                    self.checks[a.get("id", "")] = name
                if kind in ("button", "submit", "reset", "file", "image"):
                    return
                if kind in ("radio", "checkbox") and "checked" not in a:
                    return
                self.fields[name] = a.get("value", "on" if kind in ("radio", "checkbox") else "")
            elif tag == "select":
                self.select = name if "disabled" not in a else None
                self.options = []
            elif tag == "option" and self.select and "disabled" not in a:
                self.options.append((a.get("value", ""), "selected" in a))
            elif tag == "label":
                self.label, self.text = a.get("for"), []
        def handle_data(self, data):
            if self.label:
                self.text.append(data)
        def handle_endtag(self, tag):
            if tag == "label" and self.label:
                self.labels.setdefault(self.label, []).append("".join(self.text).strip())
                self.label = None
            if tag == "select" and self.select:
                if self.options:
                    self.fields[self.select] = next((v for v, selected in self.options if selected), self.options[0][0])
                self.select = None
            if tag == "form":
                self.inside = False
    form = Form()
    form.feed(html)
    if (not form.found or form.fields.get("New_CurrSide") != stage
            or form.fields.get("Old_WhoAmI") != "/assis/assi_tam_intern.lua"
            or form.fields.get("Old_TamNr") not in ("0", "1", "2", "3", "4")):
        raise FritzTAMConfigurationError("FRITZ!-Neuanlage nicht bereit oder kein freier Anrufbeantworter verfügbar")
    form.fields.pop("sid", None)
    return form


def create_fritz_tam(line, number, report, confirmation=None):
    """Create a visible TAM through FRITZ!OS's own three-step wizard."""
    name = {1: "CallWebhook SIM 1", 2: "CallWebhook SIM 2", 3: "CallWebhook Festnetz"}[line]
    normalize = lambda value: "".join(c for c in value if c.isdigit())
    expected = normalize(number)
    if len(expected) < 3:
        raise FritzTAMConfigurationError("Echte Festnetznummer für die Neuanlage erforderlich")
    sid = fritz_web_sid()
    with requests.Session() as session:
        def web_request(path, fields, method="GET"):
            args = {"params" if method == "GET" else "data": dict(fields, sid=sid)}
            response = session.request(method, f"http://{HOST}" + path, timeout=15, allow_redirects=False, **args)
            if response.status_code != 200:
                raise FritzTAMConfigurationError("FRITZ!-Neuanlage nicht erreichbar oder Anmeldung abgelaufen")
            return response
        inventory = web_request("/query.lua", {
            **{f"d{i}": f"tam:settings/TAM{i}/Display" for i in range(5)},
            **{f"n{i}": f"tam:settings/TAM{i}/Name" for i in range(5)}}).json()
        if not isinstance(inventory, dict) or any(inventory.get(f"d{i}") not in ("0", "1") for i in range(5)):
            raise FritzTAMConfigurationError("Vorhandene FRITZ!-Anrufbeantworter konnten nicht sicher gelesen werden")
        # Retrying a partially completed setup must not create duplicate mailboxes.
        for i in range(5):
            if inventory[f"d{i}"] == "1" and inventory.get(f"n{i}") == name:
                if tam_numbers_match(read_tam_info(i), [number]):
                    return i
                raise FritzTAMConfigurationError(f"{name} ist bereits mit anderer Rufnummer vorhanden; vorhandenen AB auswählen")
        path = "/assis/assi_tam_intern.lua"
        report(f"Leitung {line}: neuen FRITZ!-Anrufbeantworter vorbereiten …")
        form = fritz_tam_wizard_form(web_request(path, {}).text, "AssiTamInternEinrichten")
        index = int(form.fields["Old_TamNr"])
        if inventory[f"d{index}"] != "0":
            raise FritzTAMConfigurationError("FRITZ!Box meldet keinen freien AB-Platz; vorhandene AB bleiben erhalten")
        fields = dict(form.fields, New_TamName=name, New_OperationMode="1", New_Delay="6",
                      New_RecordingLen="180", Submit_Next="")
        form = fritz_tam_wizard_form(web_request(path, fields, "POST").text, "AssiTamInternIncoming")
        if form.fields["Old_TamNr"] != str(index):
            raise FritzTAMConfigurationError("AB-Platz während Neuanlage geändert")
        choices = [field for identifier, field in form.checks.items()
                   if any(normalize(label.replace("●", "")) == expected for label in form.labels.get(identifier, []))]
        if len(choices) != 1:
            raise FritzTAMConfigurationError("Gewählte Rufnummer im FRITZ!-Neuanlageassistenten nicht eindeutig gefunden")
        fields = {key: value for key, value in form.fields.items() if key not in form.checks.values()}
        fields.update({choices[0]: "on", "NewFnc_ConnectToAll": "F", "Submit_Next": ""})
        form = fritz_tam_wizard_form(web_request(path, fields, "POST").text, "AssiTamInternSummary")
        selected = [value for key, value in form.fields.items() if key.startswith("OldFnc_IncomingNr") and value]
        if (form.fields["Old_TamNr"] != str(index) or form.fields.get("OldFnc_ConnectToAll") != "F"
                or selected != [choices[0].removeprefix("NewFnc_")]):
            raise FritzTAMConfigurationError("FRITZ!-Zusammenfassung bestätigt die ausgewählte Rufnummer nicht")
        fields = dict(form.fields, Submit_Save="", page="assi_tam_intern", xhr="1", lang="de")
        report(f"Leitung {line}: {name} mit Rufnummer {number} anlegen …")
        result = web_request("/data.lua", fields, "POST").json().get("data", {})
        if result.get("Submit_Save") == "twofactor":
            report("FRITZ!Box-Bestätigung für die AB-Neuanlage erforderlich …")
            wait_fritz_confirmation(web_request, result.get("twofactor", ""), confirmation)
            fields.update(confirmed="", twofactor="")
            result = web_request("/data.lua", fields, "POST").json().get("data", {})
        if result.get("Submit_Save") != "ok":
            raise FritzTAMConfigurationError("FRITZ!Box hat die AB-Neuanlage nicht bestätigt")
        visible = web_request("/query.lua", {"display": f"tam:settings/TAM{index}/Display"}).json()
        after = read_tam_info(index)
        if (visible.get("display") != "1" or not tam_numbers_match(after, [number])
                or after.get("NewEnable", "").lower() not in ("1", "true")):
            raise FritzTAMConfigurationError("AB-Neuanlage beim Zurücklesen nicht vollständig bestätigt")
        report(f"Leitung {line}: AB {index + 1} angelegt und Rufnummer zurückgelesen")
        return index


def configure_fritz_tams(groups, report, confirmation=None):
    with requests.Session() as session:
        sid = None
        for index, numbers in sorted(groups.items()):
            report(f"AB {index + 1}: aktuellen Zustand über TR-064 lesen …")
            before = read_tam_info(index)
            if tam_numbers_match(before, numbers) and before.get("NewEnable", "").lower() in ("1", "true"):
                report(f"AB {index + 1}: Rufnummern bereits richtig")
                continue
            if sid is None:
                report(f"AB {index + 1}: Web-Sitzung über CreateUrlSID anfordern …")
                sid = fritz_web_sid()
            def web_request(path, fields, method="GET"):
                args = {"params" if method == "GET" else "data": dict(fields, sid=sid)}
                response = session.request(method, f"http://{HOST}" + path, timeout=15, allow_redirects=False, **args)
                if response.status_code != 200:
                    raise FritzTAMConfigurationError(f"FRITZ!-Webzugriff auf {path} fehlgeschlagen (HTTP {response.status_code})")
                return response
            report(f"AB {index + 1}: bestehende Einstellungen und Zeitplan lesen …")
            html = web_request("/fon_devices/edit_tam.lua", {"TamNr": str(index)}).text
            timer = web_request("/query.lua", {"cw_timer": f"timer:settings/TamTimerXML{index}"}).json()
            if not isinstance(timer, dict) or "cw_timer" not in timer:
                raise FritzTAMConfigurationError("Vorhandener Zeitplan nicht lesbar; keine Änderung geschrieben")
            fields = fritz_tam_form(html, index, numbers, timer["cw_timer"])
            report(f"AB {index + 1}: ausgewählte Festnetznummern speichern …")
            result = web_request("/data.lua", fields, "POST").json().get("data", {})
            for attempt in range(3):
                if result.get("apply") != "twofactor":
                    break
                state = result.get("twofactor", "")
                report("FRITZ!Box-Bestätigung für Rufnummernzuordnung erforderlich – Bestätigungsweg wählen …")
                wait_fritz_confirmation(web_request, state, confirmation)
                fields.update(confirmed="", twofactor="")
                result = web_request("/data.lua", fields, "POST").json().get("data", {})
            if result.get("apply") == "twofactor":
                raise FritzTAMConfigurationError("FRITZ!Box fordert wiederholt eine neue Sicherheitsbestätigung an; Zuordnung noch nicht gespeichert")
            if result.get("apply") != "ok":
                raise FritzTAMConfigurationError("FRITZ!Box hat die AB-Einstellungen nicht übernommen")
            after = read_tam_info(index)
            if not tam_numbers_match(after, numbers):
                raise FritzTAMConfigurationError(f"AB {index + 1}: zurückgelesene Rufnummern stimmen nicht überein")
            if after.get("NewEnable", "").lower() not in ("1", "true"):
                tam_control_request("SetEnable", f"<NewIndex>{index}</NewIndex><NewEnable>1</NewEnable>")
                after = read_tam_info(index)
            if not tam_numbers_match(after, numbers) or after.get("NewEnable", "").lower() not in ("1", "true"):
                raise FritzTAMConfigurationError(f"AB {index + 1}: Aktivierung nicht bestätigt")
            report(f"AB {index + 1}: Rufnummern gespeichert und aus FRITZ!Box zurückgelesen")


_tam_setup_state = {"state": "idle", "message": "Noch nicht gestartet", "assignments": {}}


async def run_mailbox_setup(hass, payload, groups):
    stage = "Auftrag vorbereiten"
    def report(message):
        nonlocal stage
        stage = message
        hass.loop.call_soon_threadsafe(_tam_setup_state.update, {"message": message})
    try:
        payload = dict(payload)
        groups = {key: list(value) for key, value in groups.items()}
        for line in range(1, 4):
            key = f"mailbox_tam_{line}"
            if payload[key] == -2:
                number = payload["line_numbers"][str(line)]
                index = await hass.async_add_executor_job(create_fritz_tam, line, number, report, _tam_confirmation)
                payload[key] = index
                groups.setdefault(index, []).append(number)
        await hass.async_add_executor_job(configure_fritz_tams, groups, report, _tam_confirmation)
        keys = ("mailbox_tam_1", "mailbox_tam_2", "mailbox_tam_3")
        report("Bestätigte Zuordnung in Home Assistant speichern …")
        async with _refresh_lock:
            await hass.async_add_executor_job(save_setup, *(payload[key] for key in keys))
        _tam_setup_state.update(state="completed", message="FRITZ!-Anrufbeantworter gespeichert und geprüft",
            assignments={key: payload[key] for key in keys}, ok=True)
    except FritzTAMConfigurationError as error:
        _tam_setup_state.update(state="error", message=str(error), ok=False)
    except Exception as error:
        # Exception strings/response bodies may contain SID, password or OTP.
        # Report the operation and category only, never invent a permissions cause.
        kind = type(error).__name__
        detail = {
            "JSONDecodeError": "FRITZ!Box hat keine gültige JSON-Antwort geliefert",
            "ParseError": "FRITZ!Box hat keine gültige XML-Antwort geliefert",
            "ConnectTimeout": "Zeitüberschreitung beim Verbindungsaufbau",
            "ReadTimeout": "Zeitüberschreitung beim Lesen der Antwort",
            "ConnectionError": "Verbindung zur FRITZ!Box unterbrochen",
            "HTTPError": "HTTP-Anfrage abgelehnt",
        }.get(kind, "Interner Verarbeitungsfehler (" + kind + ")")
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int) and 100 <= status <= 599:
            detail += f" (HTTP {status})"
        _tam_setup_state.update(state="error", message=stage + " – " + detail,
                               failed_stage=stage, error_type=kind, ok=False)


_fritz_credentials_lock = asyncio.Lock()


class CallWebhookFritzCredentialsView(HomeAssistantView):
    url = "/api/callwebhook/setup/fritz-credentials"
    name = "api:callwebhook:setup:fritz-credentials"
    requires_auth = True

    async def post(self, request):
        from homeassistant.components.http.const import KEY_HASS_USER
        user = request.get(KEY_HASS_USER)
        if not user or not user.is_admin:
            raise web.HTTPForbidden()
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or set(payload) != {"username", "password"}:
                return self.json({"ok": False, "error": "Benutzer und Kennwort erforderlich"}, status_code=400)
            async with _fritz_credentials_lock:
                await request.app["hass"].async_add_executor_job(
                    save_fritz_credentials, SECRETS_FILE, payload["username"], payload["password"])
            return self.json({"ok": True})
        except ValueError:
            return self.json({"ok": False, "error": "FRITZ!Box-Zugangsdaten oder secrets.yaml konnten nicht sicher verarbeitet werden"}, status_code=400)
        except OSError:
            return self.json({"ok": False, "error": "FRITZ!Box-Zugangsdaten konnten nicht in secrets.yaml gespeichert werden"}, status_code=500)


class CallWebhookMailboxSetupView(HomeAssistantView):
    url = "/api/callwebhook/setup/mailboxes"
    name = "api:callwebhook:setup:mailboxes"
    requires_auth = True

    def require_owner(self, request):
        from homeassistant.components.http.const import KEY_HASS_USER
        user = request.get(KEY_HASS_USER)
        if not user or not user.is_admin:
            raise web.HTTPForbidden()
        return user.id

    async def get(self, request):
        owner = self.require_owner(request)
        state = dict(_tam_setup_state)
        if owner == _tam_confirmation.owner:
            state["confirmation"] = _tam_confirmation.snapshot()
        return self.json(state)

    async def post(self, request):
        try:
            payload = await request.json()
            if isinstance(payload, dict) and "confirmation" in payload:
                owner = self.require_owner(request)
                command = payload["confirmation"]
                if not isinstance(command, dict):
                    raise ValueError("Ungültige Bestätigung")
                try:
                    _tam_confirmation.submit(owner, command)
                except ValueError as error:
                    return self.json({"ok": False, "error": str(error)}, status_code=409)
                return self.json({"ok": True})
            keys = ("mailbox_tam_1", "mailbox_tam_2", "mailbox_tam_3")
            if not isinstance(payload, dict) or any(
                type(payload.get(key)) is not int or not (-2 if "line_numbers" in payload else -1) <= payload[key] <= 9
                for key in keys
            ):
                raise ValueError("Ungültige Anrufbeantworter-Zuordnung")
        except (ValueError, TypeError):
            return self.json({"ok": False, "error": "Ungültige Anrufbeantworter-Zuordnung"}, status_code=400)
        if _asterisk_setup_state.get("state") == "running":
            return self.json({"ok": False, "error": "Asterisk-Einrichtung läuft noch"}, status_code=409)
        hass = request.app["hass"]
        if "line_numbers" in payload:
            from homeassistant.components.http.const import KEY_HASS_USER
            user = request.get(KEY_HASS_USER)
            if not user or not user.is_admin:
                raise web.HTTPForbidden()
            if _tam_setup_state["state"] == "running":
                return self.json(dict(_tam_setup_state), status_code=202)
            import re
            numbers = payload["line_numbers"]
            groups = {}
            if not isinstance(numbers, dict):
                return self.json({"ok": False, "error": "Rufnummern fehlen"}, status_code=400)
            for n, key in enumerate(keys, 1):
                if payload[key] == -1:
                    continue
                number = numbers.get(str(n), "")
                if not isinstance(number, str) or not re.fullmatch(r"[+0-9 ()/-]{3,32}", number) or len(re.sub(r"\D", "", number)) < 3:
                    return self.json({"ok": False, "error": "Echte Festnetznummern erforderlich"}, status_code=400)
                groups.setdefault(payload[key] if payload[key] >= 0 else -n - 1, []).append(number)
            owners = {}
            for index, values in groups.items():
                for number in values:
                    normalized = re.sub(r"\D", "", number)
                    if normalized in owners and owners[normalized] != index:
                        return self.json({"ok": False, "error": "Eine Rufnummer darf nur einem Anrufbeantworter zugeordnet sein"}, status_code=400)
                    owners[normalized] = index
            groups = {key: values for key, values in groups.items() if key >= 0}
            _tam_confirmation.clear()
            _tam_confirmation.owner = user.id
            _tam_setup_state.update(state="running", message="FRITZ!-Anrufbeantworter werden eingerichtet …", assignments={}, ok=False)
            hass.async_create_background_task(run_mailbox_setup(hass, payload, groups), "CallWebhook TAM setup")
            return self.json(dict(_tam_setup_state), status_code=202)
        async with _refresh_lock:
            await hass.async_add_executor_job(save_setup, *(payload[key] for key in keys))
        return self.json({"ok": True, "assignments": {key: payload[key] for key in keys}})


class CallWebhookAsteriskSetupView(HomeAssistantView):
    url = "/api/callwebhook/setup/asterisk"
    name = "api:callwebhook:setup:asterisk"
    requires_auth = True

    async def post(self, request):
        global _asterisk_setup_state
        hass = request.app["hass"]
        try:
            payload = await request.json()
        except Exception:
            return self.json({"ok": False, "error": "Ungültiges JSON"}, status_code=400)
        if _asterisk_setup_state.get("state") == "running":
            return self.json({"ok": True, "state": "running"}, status_code=202)
        _asterisk_setup_state = {"state": "running", "message": "Asterisk-Einrichtung wird gestartet …", "progress_step": 0, "progress_total": 7, "result": None}
        hass.async_create_background_task(_run_asterisk_setup(hass, payload), "CallWebhook Asterisk setup")
        return self.json({"ok": True, "state": "started"}, status_code=202)


class CallWebhookAsteriskSetupStatusView(HomeAssistantView):
    url = "/api/callwebhook/setup/asterisk/status"
    name = "api:callwebhook:setup:asterisk:status"
    requires_auth = True

    async def get(self, request):
        state = dict(_asterisk_setup_state)
        state["ok"] = state.get("state") != "error"
        return self.json(state)


class CallWebhookMailboxView(
    HomeAssistantView
):
    url = "/api/callwebhook/mailbox"
    name = "api:callwebhook:mailbox"
    requires_auth = True

    async def get(
        self,
        request
    ):
        hass = request.app["hass"]

        refreshed = await async_refresh_mailbox(
            hass
        )

        if refreshed is not None:
            combined = await hass.async_add_executor_job(merge_mailbox_and_archive, refreshed)
            return self.json(combined)

        if not MAILBOX_FILE.exists():
            return self.json([])

        try:
            content = (
                await hass.async_add_executor_job(
                    MAILBOX_FILE.read_text,
                    "utf-8"
                )
            )

            data = json.loads(
                content
            )

            combined = await hass.async_add_executor_job(merge_mailbox_and_archive, data)
            return self.json(combined)

        except Exception as error:
            return self.json(
                {
                    "error": str(error)
                },
                status_code=500
            )


class CallWebhookMailboxArchiveView(HomeAssistantView):
    url = "/api/callwebhook/mailbox/{tam}/{index}/archive"
    name = "api:callwebhook:mailbox:archive"
    requires_auth = True

    async def post(self, request, tam, index):
        if not tam.isdigit() or not index.isdigit():
            raise web.HTTPBadRequest()
        hass = request.app["hass"]
        async with _refresh_lock:
            try:
                archived = await hass.async_add_executor_job(archive_message, tam, index)
            except Exception as error:
                return self.json({"success": False, "error": str(error)}, status_code=500)
        return self.json({"success": True, "tam": tam, "index": index, "message": archived})


class CallWebhookMailboxDeleteView(
    HomeAssistantView
):
    url = (
        "/api/callwebhook/"
        "mailbox/{tam}/{index}"
    )

    name = "api:callwebhook:mailbox:delete"
    requires_auth = True

    async def delete(
        self,
        request,
        tam,
        index
    ):
        if (
            not tam.isdigit()
            or not index.isdigit()
        ):
            raise web.HTTPBadRequest()

        hass = request.app["hass"]

        async with _refresh_lock:
            try:
                await hass.async_add_executor_job(
                    delete_fritz_message,
                    tam,
                    index
                )

                mailbox = (
                    await hass.async_add_executor_job(
                        fetch_fritz_mailbox
                    )
                )

            except requests.HTTPError as error:
                return self.json(
                    {
                        "success": False,
                        "error": str(error)
                    },
                    status_code=502
                )

            except Exception as error:
                return self.json(
                    {
                        "success": False,
                        "error": str(error)
                    },
                    status_code=500
                )

        return self.json(
            {
                "success": True,
                "tam": tam,
                "index": index,
                "mailbox": mailbox
            }
        )


class CallWebhookArchiveAudioView(HomeAssistantView):
    url = "/api/callwebhook/archive/audio/{tam}/{index}"
    name = "api:callwebhook:archive:audio"
    requires_auth = True

    async def get(self, request, tam, index):
        if not tam.isdigit() or not index.isdigit():
            raise web.HTTPBadRequest()
        audio_file = ARCHIVE_DIR / f"voicemail_{tam}_{index}.wav"
        if not audio_file.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(path=audio_file, headers={"Content-Type": "audio/x-wav", "Cache-Control": "private, no-store"})


class CallWebhookAudioView(
    HomeAssistantView
):
    url = (
        "/api/callwebhook/"
        "audio/{tam}/{index}"
    )

    name = "api:callwebhook:audio"
    requires_auth = True

    async def get(
        self,
        request,
        tam,
        index
    ):
        if (
            not tam.isdigit()
            or not index.isdigit()
        ):
            raise web.HTTPBadRequest()

        audio_file = (
            BASE_DIR /
            f"voicemail_{tam}_{index}.wav"
        )

        if not audio_file.exists():
            hass = request.app["hass"]
            async with _refresh_lock:
                try:
                    audio_file = await hass.async_add_executor_job(ensure_message_audio, tam, index)
                except Exception:
                    return self.json({"error": "FRITZ!Box-Aufnahme konnte nicht geladen werden"}, status_code=502)
            if audio_file is None:
                raise web.HTTPNotFound()

        return web.FileResponse(
            path=audio_file,
            headers={
                "Content-Type":
                    "audio/x-wav",
                "Cache-Control":
                    "private, no-store"
            }
        )


# VoIP push credentials stay on HA. They are never returned by any status endpoint.
VOIP_FILE = BASE_DIR / "voip.json"
VOIP_TOPIC = "de.reno.CallWebhook.U98PKCA4W7.voip"
_voip = {}
_voip_calls = {}
_voip_clients = {}
_voip_lock = asyncio.Lock()
_voip_last_status = "Noch kein Anruf-Push gesendet"


def load_voip():
    global _voip
    if VOIP_FILE.exists():
        _voip = json.loads(VOIP_FILE.read_text())
    _voip.setdefault("hook_secret", secrets.token_hex(32))


def save_voip(value):
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    temp = VOIP_FILE.with_suffix(".tmp")
    fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(value, file)
    os.chmod(temp, 0o600)
    temp.replace(VOIP_FILE)


def voip_configured():
    return bool(_voip.get("key") or (_voip.get("relay_url") and _voip.get("relay_credential")))


async def relay_request(url, credential, path, payload=None):
    # Never follow redirects with the installation's bearer credential.
    async with ClientSession(timeout=ClientTimeout(total=6)) as session:
        async with session.request("POST" if payload is not None else "GET", url + path,
                headers={"Authorization": "Bearer " + credential}, json=payload,
                allow_redirects=False) as response:
            if response.status != 200:
                raise RuntimeError(f"Push-Dienst antwortet mit HTTP {response.status}")
            return await response.json()


def fritz_incoming_pjsip(original):
    """Match the configured router even when it drops REGISTER's line parameter.

    Use one inbound-only endpoint for the router, not three competing IP matches.
    The iPhone keeps its authenticated endpoint; outgoing line credentials remain
    untouched. Never trust arbitrary caller IDs, networks or SIP header matches.
    """
    import ipaddress
    import re
    if not isinstance(original, str):
        raise ValueError("Asterisk-SIP-Konfiguration fehlt")
    owned = {"callwebhook-fritz-incoming", "callwebhook-fritz-identify"}
    kept, hosts, section = [], set(), ""
    for line in original.splitlines():
        match = re.fullmatch(r"\s*\[([^]]+)\]\s*(?:;.*)?", line)
        if match:
            section = match.group(1)
        if section in owned:
            continue
        kept.append(line)
        if section not in {"fritz1-registration", "fritz2-registration", "fritz3-registration"}:
            continue
        entry = re.fullmatch(r"\s*server_uri\s*=\s*sip:([^;\s]+)\s*(?:;.*)?", line)
        if not entry:
            continue
        uri = urlparse("sip://" + entry.group(1))
        host = uri.hostname
        if not host or uri.username or uri.password or uri.path or uri.query or uri.fragment:
            raise ValueError("Ungültige FRITZ!Box-Adresse für eingehende Anrufe")
        # Accessing .port rejects malformed ports before producing any config.
        if uri.port is not None and not 1 <= uri.port <= 65535:
            raise ValueError("Ungültiger FRITZ!Box-SIP-Port")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if (len(host) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host)
                    or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                           for label in host.split("."))):
                raise ValueError("Ungültiger FRITZ!Box-Hostname")
        else:
            if address.is_unspecified or address.is_multicast:
                raise ValueError("FRITZ!Box-Adresse muss einen einzelnen Router bezeichnen")
        hosts.add(host)
    if len(hosts) != 1:
        raise ValueError("Eine eindeutige FRITZ!Box-Adresse für eingehende Anrufe fehlt")
    host = hosts.pop()
    return "\n".join(kept).rstrip() + f"""

[callwebhook-fritz-incoming]
type=endpoint
transport=transport-udp
context=from-fritz
identify_by=ip
disallow=all
allow=alaw,ulaw
direct_media=no
force_rport=yes
rtp_symmetric=yes

[callwebhook-fritz-identify]
type=identify
endpoint=callwebhook-fritz-incoming
match={host}
srv_lookups=no
"""


def voip_dialplan(original):
    """Replace only the app-owned incoming contexts; preserve outgoing routes."""
    import re
    if not _voip.get("device") or not voip_configured():
        return original
    secret = _voip["hook_secret"]
    # Supervisor's HA hostname is reachable from the Asterisk add-on network.
    hook = f"http://homeassistant:8123/api/callwebhook/voip/hook/{secret}"
    owned = {"from-fritz", "from-easybell", "callwebhook-push-header"}
    kept, skip = [], False
    for line in original.splitlines():
        match = re.match(r"^\s*\[([^]]+)\]\s*$", line)
        if match:
            skip = match.group(1) in owned
        if not skip:
            kept.append(line)
    route = '''
[from-fritz]
exten => s,1,NoOp(CallWebhook incoming VoIP)
 same => n,Set(CW_ID=${UUID()})
 same => n,Set(CURLOPT(conntimeout)=2)
 same => n,Set(CURLOPT(httptimeout)=15)
 same => n,Set(CW_PUSH=${CURL(HOOK/ring?id=${CW_ID}&caller=${URIENCODE(${CALLERID(num)})}&line=${CW_LINE})})
 same => n,NoOp(CallWebhook push result: ${CW_PUSH})
 same => n,GotoIf($["${CW_PUSH}"="cancelled"]?done)
 same => n,Dial(${PJSIP_DIAL_CONTACTS(callwebhook-ios)},60,b(callwebhook-push-header^s^1(${CW_ID}^${CW_LINE})))
 same => n(done),Hangup()
exten => _.,1,Set(__CW_LINE=0)
 same => n,ExecIf($["${EXTEN}"="callwhapp1"]?Set(__CW_LINE=1))
 same => n,ExecIf($["${EXTEN}"="callwhapp2"]?Set(__CW_LINE=2))
 same => n,ExecIf($["${EXTEN}"="callwhapp3"]?Set(__CW_LINE=3))
 same => n,Goto(s,1)
exten => h,1,Set(CURLOPT(httptimeout)=2)
 same => n,Set(CW_END=${CURL(HOOK/end?id=${CW_ID})})
 same => n,Hangup()

[from-easybell]
exten => s,1,Goto(from-fritz,s,1)
exten => _.,1,Goto(from-fritz,s,1)

[callwebhook-push-header]
exten => s,1,Set(PJSIP_HEADER(add,X-CallWebhook-ID)=${ARG1})
 same => n,Set(PJSIP_HEADER(add,X-CallWebhook-Line)=${ARG2})
 same => n,Return()
'''.replace("HOOK", hook)
    return "\n".join(kept).rstrip() + "\n" + route


async def send_voip_push(call_id, caller):
    global _voip_last_status
    from aioapns import APNs, NotificationRequest, PushType
    device = _voip.get("device")
    if not device or not voip_configured():
        _voip_last_status = "Push-Dienst oder iPhone-Registrierung fehlt"
        return False
    if _voip.get("relay_url") and _voip.get("relay_credential"):
        try:
            response = await relay_request(_voip["relay_url"], _voip["relay_credential"], "/v1/ring",
                {"call_id": call_id, "caller": caller[:128]})
            if response.get("accepted") is not True:
                raise RuntimeError("Push nicht angenommen")
            _voip_last_status = "Anruf-Push von Apple angenommen (Zustellung noch nicht bestätigt)"
            return True
        except RuntimeError as error:
            # relay_request creates these messages locally without credentials.
            message = str(error)
            safe = message if message.startswith("Push-Dienst antwortet mit HTTP ") or message == "Push nicht angenommen" else "Push-Dienst nicht erreichbar"
            _voip_last_status = "Anruf-Push fehlgeschlagen: " + safe
            return False
        except Exception:
            _voip_last_status = "Anruf-Push über den gemeinsamen Dienst fehlgeschlagen"
            return False
    environment = device["environment"]
    client = _voip_clients.get(environment)
    if client is None:
        client = APNs(key=_voip["key"], key_id=_voip["key_id"], team_id=_voip["team_id"],
                      topic=VOIP_TOPIC, use_sandbox=environment == "development",
                      max_connections=1, max_connection_attempts=1)
        _voip_clients[environment] = client
    request = NotificationRequest(
        device_token=device["token"], notification_id=call_id,
        message={"aps": {}, "call_id": call_id, "caller": caller, "sent_at": int(time.time())},
        push_type=PushType.VOIP, priority=10, time_to_live=5)
    try:
        result = await asyncio.wait_for(client.send_notification(request), timeout=4)
        if not result.is_successful:
            _voip_last_status = f"Apple-Push abgelehnt: {result.status} {result.description}"
            return False
        _voip_last_status = "Anruf-Push von Apple angenommen (Zustellung noch nicht bestätigt)"
        return True
    except Exception:
        _voip_last_status = "Apple-Push-Verbindung fehlgeschlagen"
        return False


class CallWebhookVoIPView(HomeAssistantView):
    url = "/api/callwebhook/voip"
    name = "api:callwebhook:voip"
    requires_auth = True

    async def get(self, request):
        return self.json({"configured": voip_configured(), "api_version": BACKEND_API_VERSION,
                          "mode": "relay" if _voip.get("relay_credential") else "direct",
                          "route_ready": bool(_voip.get("route_ready") and _voip.get("incoming_route_revision") == 3),
                          "registered": bool(_voip.get("device")), "message": _voip_last_status})

    async def post(self, request):
        import re
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError()
            async with _voip_lock:
                value = dict(_voip)
                action = payload.get("action")
                if action == "register":
                    token, environment = payload.get("token", ""), payload.get("environment")
                    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32,512}", token) or environment not in ("development", "production"):
                        raise ValueError()
                    if value.get("device") != {"token": token, "environment": environment}:
                        # A failed new-device enrollment must never keep ringing
                        # the old device through its previously scoped grant.
                        value.pop("relay_url", None)
                        value.pop("relay_credential", None)
                    value["device"] = {"token": token, "environment": environment}
                elif action == "unregister":
                    if payload.get("token") == value.get("device", {}).get("token"):
                        value.pop("device", None)
                elif action == "relay":
                    url, credential = payload.get("url", ""), payload.get("credential", "")
                    if not isinstance(url, str) or not isinstance(credential, str):
                        raise ValueError()
                    parsed = urlparse(url)
                    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                            or parsed.query or parsed.fragment or parsed.path.rstrip("/") not in ("", RELAY_PUBLIC_PREFIX)
                            or not re.fullmatch(r"[0-9a-f]{64}", credential)):
                        raise ValueError()
                    url = url.rstrip("/")
                    device = value.get("device", {})
                    try:
                        registered = await relay_request(url, credential, "/v1/registration")
                    except Exception:
                        return self.json({"ok": False, "error": "Push-Dienst nicht erreichbar oder Anmeldung ungültig"}, status_code=502)
                    if (registered.get("registered") is not True
                            or registered.get("environment") != device.get("environment")
                            or registered.get("token_hash") != hashlib.sha256(device.get("token", "").encode()).hexdigest()):
                        raise ValueError()
                    value.update(relay_url=url, relay_credential=credential)
                elif action == "credentials":
                    from cryptography.hazmat.primitives.serialization import load_pem_private_key
                    from cryptography.hazmat.primitives.asymmetric import ec
                    key = payload.get("key", "")
                    if not isinstance(key, str) or len(key) > 4096:
                        raise ValueError()
                    parsed = load_pem_private_key(key.encode(), password=None)
                    if not isinstance(parsed, ec.EllipticCurvePrivateKey) or not isinstance(parsed.curve, ec.SECP256R1):
                        raise ValueError()
                    for field in ("team_id", "key_id"):
                        if not isinstance(payload.get(field), str) or not re.fullmatch(r"[A-Z0-9]{10}", payload[field]):
                            raise ValueError()
                        value[field] = payload[field]
                    value["key"] = key
                else:
                    raise ValueError()
                credentials_changed = action == "credentials" and any(value.get(field) != _voip.get(field) for field in ("key", "key_id", "team_id"))
                await request.app["hass"].async_add_executor_job(save_voip, value)
                _voip.clear()
                _voip.update(value)
                if credentials_changed:
                    for client in _voip_clients.values():
                        client.pool.close()
                    _voip_clients.clear()
            return self.json({"ok": True})
        except (ValueError, TypeError):
            return self.json({"ok": False, "error": "Ungültiger Push-Schlüssel oder ungültige Geräteanmeldung"}, status_code=400)


class CallWebhookVoIPCallView(HomeAssistantView):
    url = "/api/callwebhook/voip/call/{call_id}"
    name = "api:callwebhook:voip:call"
    requires_auth = True

    async def get(self, request, call_id):
        call = _voip_calls.get(call_id)
        return self.json({"active": bool(call and not call["ended"] and time.monotonic() - call["created"] < 90),
                          "line": call.get("line") if call else None})

    async def post(self, request, call_id):
        call = _voip_calls.get(call_id)
        if not call or call["ended"] or time.monotonic() - call["created"] >= 90:
            return self.json({"ok": False}, status_code=410)
        payload = await request.json()
        if payload.get("action") == "ready":
            call["ready"].set()
        elif payload.get("action") == "end":
            call["ended"] = True
            call["ready"].set()
        else:
            return self.json({"ok": False}, status_code=400)
        return self.json({"ok": True})


class CallWebhookVoIPHookView(HomeAssistantView):
    url = "/api/callwebhook/voip/hook/{secret}/{action}"
    name = "api:callwebhook:voip:hook"
    requires_auth = False  # Dedicated 256-bit secret; never accepts an HA token in the URL.

    async def get(self, request, secret, action):
        from uuid import UUID
        if not secrets.compare_digest(secret, _voip.get("hook_secret", "")) or not _voip.get("hook_secret"):
            return web.Response(status=401)
        try:
            call_id = str(UUID(request.query.get("id", "")))
        except ValueError:
            return web.Response(status=400)
        for key, call in list(_voip_calls.items()):
            if time.monotonic() - call["created"] > 120:
                del _voip_calls[key]
        if action == "end":
            if call_id in _voip_calls:
                _voip_calls[call_id]["ended"] = True
                _voip_calls[call_id]["ready"].set()
            return web.Response(text="ended")
        if action != "ring":
            return web.Response(status=404)
        if call_id in _voip_calls:
            return web.Response(text="cancelled" if _voip_calls[call_id]["ended"] else "duplicate")
        if len(_voip_calls) >= 64:
            return web.Response(status=429)
        line = request.query.get("line")
        call = {"created": time.monotonic(), "ended": False, "ready": asyncio.Event(),
                "line": int(line) if line in ("1", "2", "3") else None}
        _voip_calls[call_id] = call
        caller = request.query.get("caller", "Unbekannt")[:80]
        global _voip_last_status
        outcome = "push_failed"
        if await send_voip_push(call_id, caller):
            try:
                await asyncio.wait_for(call["ready"].wait(), timeout=8)
                outcome = "ready"
                if not call["ended"]:
                    _voip_last_status = "iPhone hat den Anruf-Push bestätigt und SIP vorbereitet"
            except asyncio.TimeoutError:
                outcome = "wake_timeout"
                _voip_last_status = "Apple hat den Push angenommen; keine Bereitschaftsbestätigung vom iPhone innerhalb von 8 Sekunden"
        # Direct SIP remains available, but a failed push must never claim ready.
        return web.Response(text="cancelled" if call["ended"] else outcome)


async def async_setup(
    hass: HomeAssistant,
    config: dict
) -> bool:
    BASE_DIR.mkdir(
        parents=True,
        exist_ok=True
    )
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    hass.http.register_view(
        CallWebhookSetupStatusView
    )

    await hass.async_add_executor_job(load_voip)
    hass.http.register_view(CallWebhookVoIPView)
    hass.http.register_view(CallWebhookVoIPCallView)
    hass.http.register_view(CallWebhookVoIPHookView)
    hass.http.register_view(CallWebhookPushRelaySetupView)
    hass.http.register_view(CallWebhookPushRelayHostView)
    hass.http.register_view(CallWebhookPushRelayProxyView)
    hass.http.register_view(CallWebhookMailboxSetupView)
    hass.http.register_view(CallWebhookFritzCredentialsView)

    hass.http.register_view(
        CallWebhookAsteriskSetupView
    )

    hass.http.register_view(
        CallWebhookAsteriskSetupStatusView
    )

    hass.http.register_view(
        CallWebhookMailboxView
    )

    hass.http.register_view(
        CallWebhookMailboxDeleteView
    )

    hass.http.register_view(
        CallWebhookMailboxArchiveView
    )

    hass.http.register_view(
        CallWebhookAudioView
    )

    hass.http.register_view(
        CallWebhookArchiveAudioView
    )

    hass.async_create_background_task(
        mailbox_refresh_loop(
            hass
        ),
        "CallWebhook mailbox refresh"
    )

    return True

