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
from homeassistant.core import HomeAssistant

DOMAIN = "callwebhook"
BACKEND_API_VERSION = 2

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
_asterisk_setup_state = {"state": "idle", "message": "Noch nicht gestartet", "result": None}

async def _notify_asterisk_setup(hass, title, message):
    try:
        await hass.services.async_call(
            "notify",
            "mobile_app_iphone_von_reno",
            {"title": title, "message": message},
            blocking=False,
        )
    except Exception as error:
        print(f"CallWebhook Diagnose-Push fehlgeschlagen: {error}")

async def _run_asterisk_setup(hass, payload):
    global _asterisk_setup_state
    try:
        _asterisk_setup_state = {"state": "running", "message": "Asterisk-Repository und Store werden geprüft …", "result": None}
        await _notify_asterisk_setup(hass, "CallWebhook", "Asterisk-Installation wird jetzt über Home Assistant gestartet.")
        actual_addon = await hass.async_add_executor_job(ensure_asterisk_addon)
        await _notify_asterisk_setup(hass, "CallWebhook", f"Asterisk ist installiert und gestartet ({actual_addon}).")
        _asterisk_setup_state["message"] = "Asterisk installiert – Konfiguration wird geschrieben …"
        actual_path = f"/addon_configs/{actual_addon}/asterisk/custom"
        files = await hass.async_add_executor_job(install_asterisk_config, payload.get("pjsip"), payload.get("extensions"), actual_addon, actual_path)
        configured_tams = await hass.async_add_executor_job(save_setup, payload.get("mailbox_tam_1"), payload.get("mailbox_tam_2"), payload.get("mailbox_tam_3"))
        _asterisk_setup_state["message"] = "Asterisk-Konfiguration geschrieben – Neustart läuft …"
        await hass.services.async_call("hassio", "addon_restart", {"addon": actual_addon}, blocking=True)
        await hass.async_add_executor_job(ensure_asterisk_addon)
        result = {"addon": actual_addon, "files": files, "config_verified": all(Path(path).exists() for path in files), "mailbox_tams": configured_tams}
        _asterisk_setup_state = {"state": "done", "message": "Asterisk installiert, gestartet und konfiguriert", "result": result}
    except Exception as error:
        _asterisk_setup_state = {"state": "error", "message": str(error), "result": None}
        await _notify_asterisk_setup(hass, "CallWebhook", f"Asterisk-Installation fehlgeschlagen: {error}")



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
    temp.write_text(json.dumps({"tams": values}, ensure_ascii=False, indent=2), encoding="utf-8")
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



def ensure_asterisk_addon():
    host = os.environ.get("SUPERVISOR", "supervisor")
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError("Home Assistant Supervisor-Token ist intern nicht verfügbar")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    repository_url = "https://github.com/TECH7Fox/asterisk-hass-addons"

    repo = requests.post(
        f"http://{host}/store/repositories",
        headers=headers,
        json={"repository": repository_url},
        timeout=120,
    )
    if repo.status_code not in (200, 201, 400, 409):
        raise RuntimeError(f"Asterisk-Repository konnte nicht hinzugefügt werden: HTTP {repo.status_code} – {repo.text}")

    # Force Supervisor to refresh the store, then discover the real generated app slug.
    requests.post(f"http://{host}/store/reload", headers=headers, json={}, timeout=120)
    addon_slug = None
    addon_info = None
    for _ in range(45):
        listing = requests.get(f"http://{host}/store/addons", headers=headers, timeout=30)
        if listing.status_code < 300:
            try:
                payload = listing.json()
                candidates = payload.get("data", payload)
                if isinstance(candidates, dict):
                    candidates = candidates.get("addons", [])
                for item in candidates if isinstance(candidates, list) else []:
                    repo_value = str(item.get("repository", ""))
                    name_value = str(item.get("name", ""))
                    slug_value = str(item.get("slug", ""))
                    if (
                        "TECH7Fox/asterisk-hass-addons" in repo_value
                        or name_value.lower() == "asterisk"
                        or slug_value.lower().endswith("_asterisk")
                    ):
                        addon_slug = slug_value
                        addon_info = item
                        break
            except (ValueError, TypeError):
                pass
        if addon_slug:
            break
        time.sleep(2)
    if not addon_slug:
        raise RuntimeError("Asterisk-Repository ist vorhanden, aber Supervisor liefert noch keinen Asterisk-App-Slug")

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
        for _ in range(60):
            last_store_response = requests.get(store_info_url, headers=headers, timeout=30)
            if last_store_response.status_code < 300:
                try:
                    current = last_store_response.json().get("data", {})
                    installed_version = current.get("installed")
                except ValueError:
                    installed_version = None
                if installed_version:
                    break
            time.sleep(2)
        if not installed_version:
            detail = "" if last_store_response is None else f"HTTP {last_store_response.status_code} – {last_store_response.text}"
            raise RuntimeError(f"Supervisor hat die Asterisk-Installation nicht bestätigt (installed ist leer): {detail}")

    info_url = f"http://{host}/addons/{addon_slug}/info"

    # TECH7Fox Asterisk requires ami_password before the app can start.
    ami_password = secrets.token_urlsafe(32)
    options = requests.post(
        f"http://{host}/addons/{addon_slug}/options",
        headers=headers,
        json={"options": {"ami_password": ami_password}},
        timeout=60,
    )
    if options.status_code not in (200, 201):
        raise RuntimeError(f"Asterisk wurde installiert, aber die Pflichtkonfiguration konnte nicht gesetzt werden: HTTP {options.status_code} – {options.text}")

    start = requests.post(f"http://{host}/addons/{addon_slug}/start", headers=headers, json={}, timeout=120)
    if start.status_code not in (200, 201):
        raise RuntimeError(f"Asterisk {installed_version} ist installiert, konnte aber nicht gestartet werden: HTTP {start.status_code} – {start.text}")
    for _ in range(45):
        state = requests.get(info_url, headers=headers, timeout=30)
        if state.status_code < 300:
            try:
                data = state.json().get("data", {})
                current = str(data.get("state", "")).lower()
                if current == "started":
                    return addon_slug
                if current in ("error", "failed"):
                    raise RuntimeError(f"Asterisk meldet nach dem Start den Zustand {current}")
            except ValueError:
                pass
        time.sleep(2)
    raise RuntimeError(f"Asterisk {installed_version} ist installiert, wurde aber innerhalb von 90 Sekunden nicht gestartet")

def install_asterisk_config(pjsip, extensions, addon, custom_path):
    if not addon or not addon.endswith("_asterisk"):
        raise RuntimeError("Unbekanntes Asterisk-Add-on")
    requested_dir = Path(custom_path)
    expected_dir = Path(f"/addon_configs/{addon}/asterisk/custom")
    if requested_dir != expected_dir:
        raise RuntimeError("Unzulässiger Asterisk-Konfigurationspfad")
    try:
        requested_dir.mkdir(parents=True, exist_ok=True)
    except Exception as error:
        raise RuntimeError(
            "Asterisk-Konfigurationsordner konnte nicht angelegt werden: "
            f"{requested_dir}: {error}"
        ) from error
    if not requested_dir.is_dir():
        raise RuntimeError(
            "Asterisk-Konfigurationspfad ist kein Verzeichnis: "
            f"{requested_dir}"
        )
    if not isinstance(pjsip, str) or not pjsip.strip():
        raise RuntimeError("pjsip-Konfiguration fehlt")
    if not isinstance(extensions, str) or not extensions.strip():
        raise RuntimeError("extensions-Konfiguration fehlt")
    required_pjsip = ("[fritz1-auth]", "[fritz1-endpoint]", "[fritz2-auth]", "[fritz2-endpoint]")
    required_extensions = ("[from-callwebhook-ios]",)
    if not all(token in pjsip for token in required_pjsip):
        raise RuntimeError("pjsip-Konfiguration unvollständig")
    if not all(token in extensions for token in required_extensions):
        raise RuntimeError("extensions-Konfiguration unvollständig")

    targets = {
        requested_dir / "pjsip.conf": pjsip,
        requested_dir / "extensions.conf": extensions,
    }
    backups = {}
    try:
        for target, content in targets.items():
            if target.exists():
                backup = target.with_suffix(target.suffix + ".bak")
                backup.write_bytes(target.read_bytes())
                backups[target] = backup
            temp = target.with_suffix(target.suffix + ".tmp")
            temp.write_text(content.rstrip() + "\n", encoding="utf-8")
            temp.replace(target)
            written = target.read_text(encoding="utf-8")
            if written != content.rstrip() + "\n":
                raise RuntimeError(f"Asterisk-Konfiguration konnte nicht verifiziert werden: {target.name}")
    except Exception:
        for target, backup in backups.items():
            if backup.exists():
                backup.replace(target)
        raise
    return [str(path) for path in targets]


class CallWebhookSetupStatusView(HomeAssistantView):
    url = "/api/callwebhook/setup/status"
    name = "api:callwebhook:setup:status"
    requires_auth = True

    async def get(self, request):
        return self.json({
            "ok": True,
            "domain": DOMAIN,
            "api_version": BACKEND_API_VERSION,
            "asterisk_provisioning": True,
            "mailbox": True,
            "archive": True,
        })


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
        _asterisk_setup_state = {"state": "running", "message": "Asterisk-Einrichtung wird gestartet …", "result": None}
        hass.async_create_task(_run_asterisk_setup(hass, payload))
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

    hass.async_create_task(
        mailbox_refresh_loop(
            hass
        ),
        "CallWebhook mailbox refresh"
    )

    return True