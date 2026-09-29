"""One-shot uninstall, independent of the backend being healthy or installed.

No arbitrary paths/slugs are accepted. HA is stopped while its shared YAML and
storage files are edited, and restarted even if cleanup fails. No router calls.
"""
import json
import os
from pathlib import Path
import re
import shutil
import stat
from urllib.request import Request, urlopen

import yaml

CALLWEBHOOK_REPO = "https://github.com/oooonoooorenoooo/-callwebhook"
ASTERISK_REPO = "https://github.com/tech7fox/asterisk-hass-addons"
HELPER = "iphone_call_active"


def supervisor(method, path, data=None):
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError("Supervisor-Token fehlt")
    request = Request("http://supervisor" + path, method=method,
                      data=json.dumps(data).encode() if data is not None else None,
                      headers={"Authorization": "Bearer " + token,
                               "Content-Type": "application/json"})
    with urlopen(request, timeout=300) as response:
        body = json.load(response)
    if body.get("result") != "ok":
        raise RuntimeError("Supervisor hat " + path + " nicht bestätigt")
    return body.get("data", {})


def owned_addons(store, installed):
    """Match both repository source and exact add-on slug, not name fragments."""
    sources = {item["slug"]: item.get("source", "").rstrip("/").removesuffix(".git").lower()
               for item in store.get("repositories", [])}
    expected = set()
    for prefix, source in sources.items():
        if not re.fullmatch(r"[a-z0-9]+", prefix):
            continue
        if source == CALLWEBHOOK_REPO:
            expected.update(prefix + "_" + name for name in
                            ("callwebhook_bootstrap", "callwebhook_push_relay"))
        elif source == ASTERISK_REPO:
            expected.add(prefix + "_asterisk")
    return sorted(item["slug"] for item in installed.get("addons", [])
                  if item.get("slug") in expected)


def checked_path(root, relative):
    root = Path(root).resolve()
    path = root / relative
    if not path.is_relative_to(root):
        raise ValueError("Pfad außerhalb der HA-Konfiguration")
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink():
            raise ValueError("Symlink wird nicht verändert: " + relative)
    return path


def remove_yaml_keys(text, paths):
    """Delete exact mapping nodes, preserving every other byte and HA YAML tag."""
    root = yaml.compose(text)
    spans = []
    for keys in paths:
        if len(keys) == 2 and isinstance(root, yaml.MappingNode):
            parent = next((v for k, v in root.value if k.value == keys[0]), None)
            if isinstance(parent, yaml.MappingNode) and len(parent.value) == 1 and parent.value[0][0].value == keys[1]:
                keys = keys[:1]  # Do not leave an empty/null input_boolean mapping.
        node = root
        for index, key in enumerate(keys):
            if not isinstance(node, yaml.MappingNode):
                break
            found = next(((k, v) for k, v in node.value if k.value == key), None)
            if found is None:
                break
            key_node, node = found
            if index == len(keys) - 1:
                start = key_node.start_mark.line
                # Shared flow mappings cannot be edited safely line by line.
                lines = text.splitlines(keepends=True)
                if lines[start][:key_node.start_mark.column].strip():
                    raise ValueError("CallWebhook-Konfiguration bitte zuerst als YAML-Block formatieren")
                end = node.end_mark.line
                # A mapping's end mark may point at the indentation of its
                # next sibling. That sibling must not be consumed.
                if end < len(lines) and lines[end][:node.end_mark.column].strip():
                    end += 1
                spans.append((start, max(start + 1, end)))
    lines = text.splitlines(keepends=True)
    result = "".join(line for index, line in enumerate(lines)
                     if not any(start <= index < end for start, end in spans))
    yaml.compose(result)  # Also rejects deleting an anchor used by another entry.
    return result


def file_plan(root):
    """Validate all shared files before making any filesystem changes."""
    edits = {}
    for relative, keys in (
        ("configuration.yaml", [("callwebhook",), ("input_boolean", HELPER)]),
        ("secrets.yaml", [("fritz_callwebhook_user",), ("fritz_callwebhook_password",)]),
    ):
        path = checked_path(root, relative)
        if path.exists():
            before = path.read_text()
            after = remove_yaml_keys(before, keys)
            if after != before:
                edits[path] = after

    helper_ids = {HELPER}
    entity_ids = {"input_boolean." + HELPER}
    path = checked_path(root, ".storage/input_boolean")
    if path.exists():
        value = json.loads(path.read_text())
        items = value["data"]["items"]
        removed = [item for item in items if item.get("id") == HELPER or
                   (item.get("name") == HELPER and item.get("icon") == "mdi:phone-in-talk")]
        helper_ids.update(item["id"] for item in removed)
        if removed:
            value["data"]["items"] = [item for item in items if item not in removed]
            edits[path] = json.dumps(value, ensure_ascii=False, indent=2) + "\n"

    path = checked_path(root, ".storage/core.entity_registry")
    if path.exists():
        value = json.loads(path.read_text())
        changed = False
        for key in ("entities", "deleted_entities"):
            items = value["data"].get(key, [])
            removed = [item for item in items if item.get("platform") == "input_boolean"
                       and item.get("unique_id") in helper_ids]
            entity_ids.update(item["entity_id"] for item in removed if "entity_id" in item)
            if removed:
                value["data"][key] = [item for item in items if item not in removed]
                changed = True
        if changed:
            edits[path] = json.dumps(value, ensure_ascii=False, indent=2) + "\n"

    path = checked_path(root, ".storage/core.restore_state")
    if path.exists():
        value = json.loads(path.read_text())
        items = value["data"]
        kept = [item for item in items if item.get("state", {}).get("entity_id") not in entity_ids]
        if kept != items:
            value["data"] = kept
            edits[path] = json.dumps(value, ensure_ascii=False, indent=2) + "\n"

    directories = [checked_path(root, relative) for relative in
                   ("callwebhook", "custom_components/callwebhook")]
    return edits, directories


def apply_plan(edits, directories):
    for path, content in edits.items():
        temporary = path.with_name(path.name + ".callwebhook-cleanup.tmp")
        # O_EXCL refuses pre-existing files or symlinks. No secret-bearing backups.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                             stat.S_IMODE(path.stat().st_mode))
        try:
            with os.fdopen(descriptor, "w") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        print("Bereinigt:", path)
    for path in directories:
        if path.is_dir():
            shutil.rmtree(path)
            print("Entfernt:", path)
        elif path.exists():
            raise ValueError("Erwarteter Ordner ist eine Datei: " + str(path))


def uninstall(root=Path("/homeassistant"), api=supervisor):
    own = api("GET", "/addons/self/info")["slug"]
    store = api("GET", "/store")
    installed = api("GET", "/addons")
    prefixes = {item["slug"] for item in store.get("repositories", [])
                if item.get("source", "").rstrip("/").removesuffix(".git").lower() == CALLWEBHOOK_REPO}
    if own not in {prefix + "_callwebhook_cleanup" for prefix in prefixes}:
        raise RuntimeError("Deinstallationswerkzeug gehört nicht zum bestätigten CallWebhook-Repository")
    targets = owned_addons(store, installed)
    file_plan(root)  # Fail before stopping HA if an owned entry cannot be edited.
    for slug in targets:
        print("Stoppe:", slug)
        if api("GET", f"/addons/{slug}/info").get("state") == "started":
            api("POST", f"/addons/{slug}/stop", {})
    print("Home Assistant wird für die Bereinigung angehalten.")
    try:
        api("POST", "/core/stop", {})
        edits, directories = file_plan(root)  # Read final storage after shutdown.
        for slug in targets:
            print("Deinstalliere einschließlich Konfiguration:", slug)
            api("POST", f"/addons/{slug}/uninstall", {"remove_config": True})
        remaining = {item["slug"] for item in api("GET", "/addons").get("addons", [])}
        if remaining.intersection(targets):
            raise RuntimeError("Supervisor meldet noch installierte CallWebhook-/Asterisk-Add-ons")
        apply_plan(edits, directories)
    finally:
        print("Home Assistant wird wieder gestartet.")
        api("POST", "/core/start", {})
    print("CallWebhook und Asterisk wurden entfernt. Dieses Werkzeug entfernt sich jetzt selbst.")
    api("POST", f"/addons/{own}/uninstall", {"remove_config": True})


if __name__ == "__main__":
    os.umask(0o077)
    try:
        uninstall()
    except Exception as error:
        # Do not print response bodies, credentials or YAML parser excerpts.
        print("Deinstallation nicht vollständig abgeschlossen (" + type(error).__name__ +
              "). Werkzeug bleibt für einen erneuten Start installiert.")
        if isinstance(error, RuntimeError):
            print(str(error))
        raise SystemExit(1)
