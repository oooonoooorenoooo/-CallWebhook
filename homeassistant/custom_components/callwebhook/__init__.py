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
BACKEND_API_VERSION = 10
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
        extensions = voip_dialplan(payload.get("extensions") or "")
        push_route_applied = f"/api/callwebhook/voip/hook/{_voip.get('hook_secret', '')}" in extensions
        files = await hass.async_add_executor_job(install_asterisk_config, payload.get("pjsip"), extensions, actual_addon, actual_path)
        _asterisk_setup_state["progress_step"] = 6
        configured_tams = await hass.async_add_executor_job(save_setup, payload.get("mailbox_tam_1"), payload.get("mailbox_tam_2"), payload.get("mailbox_tam_3"))
        _asterisk_setup_state["message"] = "Asterisk-Konfiguration geschrieben – Neustart läuft …"
        await hass.services.async_call("hassio", "addon_restart", {"addon": actual_addon}, blocking=True)
        _asterisk_setup_state["progress_step"] = 6
        _asterisk_setup_state["message"] = "Asterisk wird neu gestartet und abschließend geprüft …"
        await hass.async_add_executor_job(wait_for_asterisk_started, actual_addon)
        async with _voip_lock:
            value = dict(_voip, route_ready=push_route_applied and voip_configured())
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
    ("DELETE", "v1/registration"), ("POST", "v1/ring")}


async def forward_push_relay(request, endpoint):
    import re
    if (request.method, endpoint) not in RELAY_PUBLIC_ROUTES or request.query_string:
        raise web.HTTPNotFound()
    headers = {"Content-Type": "application/json"}
    if endpoint in ("v1/registration", "v1/ring"):
        authorization = request.headers.get("Authorization", "")
        if not re.fullmatch(r"Bearer [0-9a-f]{64}", authorization):
            raise web.HTTPUnauthorized()
        headers["Authorization"] = authorization
    if request.content_length is not None and request.content_length > 32768:
        raise web.HTTPRequestEntityTooLarge(max_size=32768, actual_size=request.content_length)
    body = bytearray()
    async for chunk in request.content.iter_chunked(4096):
        body.extend(chunk)
        if len(body) > 32768:
            raise web.HTTPRequestEntityTooLarge(max_size=32768, actual_size=len(body))
    target = await push_relay_target(request.app["hass"])
    if not target:
        return web.json_response({"ready": False, "error": "Push-Dienst-Add-on nicht gestartet"}, status=503)
    try:
        async with ClientSession(timeout=ClientTimeout(total=8)) as session:
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
                          "route_ready": bool(_voip.get("route_ready")),
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
    hass.http.register_view(CallWebhookPushRelaySetupView)
    hass.http.register_view(CallWebhookPushRelayHostView)
    hass.http.register_view(CallWebhookPushRelayProxyView)
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
