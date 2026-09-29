from pathlib import Path
from datetime import datetime
import asyncio
import json
import os
import secrets
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, parse_qs, quote

import requests
from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import CoreState, HomeAssistant

DOMAIN = "callwebhook"
BACKEND_API_VERSION = 6
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
        files = await hass.async_add_executor_job(install_asterisk_config, payload.get("pjsip"), voip_dialplan(payload.get("extensions") or ""), actual_addon, actual_path)
        _asterisk_setup_state["progress_step"] = 6
        configured_tams = await hass.async_add_executor_job(save_setup, payload.get("mailbox_tam_1"), payload.get("mailbox_tam_2"), payload.get("mailbox_tam_3"))
        _asterisk_setup_state["message"] = "Asterisk-Konfiguration geschrieben – Neustart läuft …"
        await hass.services.async_call("hassio", "addon_restart", {"addon": actual_addon}, blocking=True)
        _asterisk_setup_state["progress_step"] = 6
        _asterisk_setup_state["message"] = "Asterisk wird neu gestartet und abschließend geprüft …"
        await hass.async_add_executor_job(wait_for_asterisk_started, actual_addon)
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


def get_secret(name):
    with SECRETS_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            if not line or line.startswith("#") or ":" not in line:
                continue

            key, value = line.split(":", 1)

            if key.strip() == name:
                return value.strip().strip('"').strip("'")

    raise RuntimeError(f"Secret {name} nicht gefunden")


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
    source_audio = BASE_DIR / f"voicemail_{tam}_{index}.wav"
    if not source_audio.exists():
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

def fetch_fritz_mailbox_for_tam(target_tam):
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

    if not sid:
        raise RuntimeError(
            "Keine SID in der "
            "MessageList-URL gefunden"
        )

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

        if not index:
            continue

        if not tam:
            tam = target_tam

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
            if audio_file.exists():
                valid_audio_files.add(
                    filename
                )

                audio_api_path = (
                    "/api/callwebhook/"
                    f"audio/{tam}/{index}"
                )

            else:
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
        Path(item.get("audio", "")).name
        for item in combined
        if item.get("audio", "").startswith("/api/callwebhook/audio/")
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


class CallWebhookMailboxSetupView(HomeAssistantView):
    url = "/api/callwebhook/setup/mailboxes"
    name = "api:callwebhook:setup:mailboxes"
    requires_auth = True

    async def post(self, request):
        try:
            payload = await request.json()
            keys = ("mailbox_tam_1", "mailbox_tam_2", "mailbox_tam_3")
            if not isinstance(payload, dict) or any(
                type(payload.get(key)) is not int or not -1 <= payload[key] <= 9
                for key in keys
            ):
                raise ValueError("Ungültige Anrufbeantworter-Zuordnung")
        except (ValueError, TypeError):
            return self.json({"ok": False, "error": "Ungültige Anrufbeantworter-Zuordnung"}, status_code=400)
        if _asterisk_setup_state.get("state") == "running":
            return self.json({"ok": False, "error": "Asterisk-Einrichtung läuft noch"}, status_code=409)
        hass = request.app["hass"]
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


def voip_dialplan(original):
    """Replace only the app-owned incoming contexts; preserve outgoing routes."""
    import re
    if not _voip.get("device") or not _voip.get("key"):
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
 same => n,Set(CW_PUSH=${CURL(HOOK/ring?id=${CW_ID}&caller=${URIENCODE(${CALLERID(num)})})})
 same => n,GotoIf($["${CW_PUSH}"="cancelled"]?done)
 same => n,Dial(${PJSIP_DIAL_CONTACTS(callwebhook-ios)},60,b(callwebhook-push-header^s^1(${CW_ID})))
 same => n(done),Hangup()
exten => _.,1,Goto(s,1)
exten => h,1,Set(CURLOPT(httptimeout)=2)
 same => n,Set(CW_END=${CURL(HOOK/end?id=${CW_ID})})

[from-easybell]
exten => s,1,Goto(from-fritz,s,1)
exten => _.,1,Goto(from-fritz,s,1)

[callwebhook-push-header]
exten => s,1,Set(PJSIP_HEADER(add,X-CallWebhook-ID)=${ARG1})
 same => n,Return()
'''.replace("HOOK", hook)
    return "\n".join(kept).rstrip() + "\n" + route


async def send_voip_push(call_id, caller):
    global _voip_last_status
    from aioapns import APNs, NotificationRequest, PushType
    device = _voip.get("device")
    if not device or not _voip.get("key"):
        _voip_last_status = "Apple-Push-Schlüssel oder iPhone-Registrierung fehlt"
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
        push_type=PushType.VOIP, priority=10, time_to_live=0)
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
        return self.json({"configured": bool(_voip.get("key")),
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
                    value["device"] = {"token": token, "environment": environment}
                elif action == "unregister":
                    if payload.get("token") == value.get("device", {}).get("token"):
                        value.pop("device", None)
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
                await request.app["hass"].async_add_executor_job(save_voip, value)
                _voip.clear()
                _voip.update(value)
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
        return self.json({"active": bool(call and not call["ended"] and time.monotonic() - call["created"] < 90)})

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
        call = {"created": time.monotonic(), "ended": False, "ready": asyncio.Event()}
        _voip_calls[call_id] = call
        caller = request.query.get("caller", "Unbekannt")[:80]
        if await send_voip_push(call_id, caller):
            try:
                await asyncio.wait_for(call["ready"].wait(), timeout=8)
            except asyncio.TimeoutError:
                pass  # Keep direct SIP as fallback if APNs cannot wake this device.
        return web.Response(text="cancelled" if call["ended"] else "ready")


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
    hass.http.register_view(CallWebhookMailboxSetupView)

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