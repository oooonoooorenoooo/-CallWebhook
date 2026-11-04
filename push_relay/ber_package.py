"""Operator-configured Firebase -> encrypted Pi stand model import.

No HTTP import endpoint and no new device/Apple grants. The existing server
service account reads only ber_package/{document,identity}. Plaintext PDF and
age identity exist only in memory; the model retains its encrypted /data store.
"""
import asyncio
import hashlib
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import re
import time

from .ber_document import parse_document

MAX_DOCUMENT = 5 * 1024 * 1024
MAX_IDENTITY = 16384
PARSER_VERSION = 1


class ImportFailure(Exception):
    """Messages are fixed safe text, never upstream exceptions or file content."""


def manifest(reference, identity=False):
    data = reference.get(retry=None, timeout=15).to_dict() or {}
    size, count, digest = data.get('size'), data.get('chunkCount'), data.get('sha256')
    maximum = MAX_IDENTITY if identity else MAX_DOCUMENT
    if (type(size) is not int or not 0 <= size <= maximum or (size == 0 and not identity)
            or type(count) is not int or not 0 <= count <= 32
            or bool(count) != bool(size) or not isinstance(digest, str)
            or not re.fullmatch('[0-9a-f]{64}', digest)):
        raise ImportFailure('BER-Paket fehlt in Firebase oder ist unvollständig.')
    return dict(size=size, chunkCount=count, sha256=digest)


def download(reference, info):
    chunks, size = [], 0
    for index in range(info['chunkCount']):
        row = reference.collection('chunks').document(f'{index:06d}').get(retry=None, timeout=15).to_dict() or {}
        blob = row.get('data')
        if type(row.get('index')) is not int or row['index'] != index or not isinstance(blob, bytes) or not 1 <= len(blob) <= 700000:
            raise ImportFailure('BER-Paket ist noch nicht vollständig veröffentlicht.')
        size += len(blob)
        if size > info['size']:
            raise ImportFailure('BER-Paketgröße stimmt nicht.')
        chunks.append(blob)
    data = b''.join(chunks)
    if len(data) != info['size'] or hashlib.sha256(data).hexdigest() != info['sha256']:
        raise ImportFailure('BER-Paket-Prüfsumme stimmt nicht; vorhandene Daten bleiben erhalten.')
    return data


def decrypt_document(document, identity_backup, secret):
    import pyrage
    try:
        if b'-> scrypt ' in document[:4096]:
            pdf = pyrage.passphrase.decrypt(document, secret)
        else:
            identity_text = pyrage.passphrase.decrypt(identity_backup, secret).decode('utf-8')
            identity = next(line.strip() for line in identity_text.splitlines() if line.strip().startswith('AGE-SECRET-KEY-1'))
            pdf = pyrage.decrypt(document, [pyrage.x25519.Identity.from_str(identity)])
        if not pdf.startswith(b'%PDF-') or len(pdf) > MAX_DOCUMENT:
            raise ValueError()
        return pdf
    except Exception:
        raise ImportFailure('BER-Paket konnte nicht entschlüsselt werden. Paket-Passphrase und Schlüssel-Backup prüfen.') from None


def parse_pdf(pdf):
    from pypdf import PdfReader
    try:
        reader = PdfReader(BytesIO(pdf), strict=True)
        if reader.is_encrypted or not 38 <= len(reader.pages) <= 100:
            raise ValueError()
        # Layout extraction keeps table labels and coordinates on the same row.
        pages = [page.extract_text(extraction_mode='layout', layout_mode_space_vertically=False) for page in reader.pages]
        if sum(map(len, pages)) > 2_000_000:
            raise ValueError()
        return parse_document(pages)
    except Exception:
        raise ImportFailure('BER-PDF erkannt, aber die geprüfte Standplatztabelle konnte nicht gelesen werden.') from None


def fetch_package(store, secret, known_source):
    """Blocking IO/crypto/PDF work only; never touches the event-loop SQLite DB."""
    store.ready()
    from firebase_admin import firestore
    db = firestore.client(app=store.app)
    document_ref = db.collection('ber_package').document('document')
    identity_ref = db.collection('ber_package').document('identity')
    document_info = manifest(document_ref)
    identity_info = manifest(identity_ref, identity=True)
    source = hashlib.sha256(json.dumps([PARSER_VERSION, document_info, identity_info], sort_keys=True).encode()).hexdigest()
    if source == known_source:
        return None
    document, identity = download(document_ref, document_info), download(identity_ref, identity_info)
    # Publication is chunked, not transactional. Never install a mixed revision.
    if document_info != manifest(document_ref) or identity_info != manifest(identity_ref, identity=True):
        raise ImportFailure('BER-Paket wird gerade aktualisiert; Import wird später wiederholt.')
    positions = parse_pdf(decrypt_document(document, identity, secret))
    return source, positions


class BERPackageImporter:
    def __init__(self, model, store):
        self.model, self.store = model, store
        self.status = 'Direkter BER-Import nicht eingerichtet'
        self.next_check = 0
        self.failures = 0
        self.lock = asyncio.Lock()

    async def check(self, now=None):
        async with self.lock:
            now = time.monotonic() if now is None else now
            if now < self.next_check:
                return
            filename = os.environ.get('COMATALARM_BER_PASSPHRASE_FILE', '')
            if not filename:
                self.status = 'Direkter BER-Import nicht eingerichtet'
                self.next_check = now + 60
                return
            try:
                if not os.environ.get('COMATALARM_FIREBASE_CREDENTIALS') or len(os.environ.get('COMATALARM_SETUP_KEY', '')) < 32:
                    raise ImportFailure('Direkter BER-Import: Firebase-Dienstkonto und ComatAlarm-Einrichtungsschlüssel erforderlich.')
                secret = Path(filename).read_text()
                if not 1 <= len(secret) <= 4096:
                    raise ImportFailure('BER-Paket-Passphrase fehlt oder ist ungültig.')
                known = self.model.data.get('firebase_package_source') if self.model.ready else None
                result = await asyncio.to_thread(fetch_package, self.store, secret, known)
                if result is not None:
                    source, positions = result
                    self.model.install_package(positions, source)
                    logging.warning('ComatAlarm: BER-Standplatzpaket aus Firebase geladen und verschlüsselt gespeichert (%d Standplätze, %d Referenzpunkte).', len(self.model.positions()), len(positions)-len(self.model.positions()))
                self.status = 'BER-Standplatzpaket aus Firebase bereit'
                self.failures = 0
                self.next_check = now + 6*3600
            except asyncio.CancelledError:
                raise
            except Exception as error:
                message = str(error) if isinstance(error, ImportFailure) else 'BER-Import fehlgeschlagen: Firebase-Zugriff oder Paket prüfen.'
                if self.model.ready:
                    message += ' Gespeicherter Standplatzbestand bleibt verfügbar.'
                if self.status != message:
                    logging.warning('ComatAlarm: %s', message)
                self.status = message
                self.failures += 1
                self.next_check = now + min(300 * 2**min(self.failures-1, 3), 1800)

    async def run(self):
        while True:
            await self.check()
            await asyncio.sleep(30)
