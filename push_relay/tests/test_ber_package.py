"""Synthetic geometry only. No production BER files, passphrases or Firebase calls."""
import asyncio
import copy
import hashlib
import importlib.util
from io import BytesIO
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import pyrage
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from push_relay.ber_document import parse_page, parse_document
from push_relay.ber_package import BERPackageImporter, ImportFailure, decrypt_document, fetch_package, parse_pdf
from push_relay.stand_model import StandModel
from push_relay.comatalarm import protect_index, unprotect_index


def pages():
    result = [''] * 40
    result[3] = '06L 52.369000 13.512000'
    result[5] = 'M7 52.368000 13.512000'
    result[18] = 'M9 52.365000 13.510000\n19 (Stopp 1) 52.361000 13.501000\nB05 52.362000 13.502000'
    result[19] = 'B14 52.363000 13.503000'
    result[37] = 'E35 (Stopp 2) 52.364000 13.504000\nM7 52.360000 13.500000'
    return result


def pdf_bytes(texts):
    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')}))
    for text in texts:
        page = writer.add_blank_page(width=600, height=800)
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
        content = ['BT /F1 12 Tf 40 740 Td 16 TL']
        for line in text.splitlines():
            escaped = line.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
            content.append(f'({escaped}) Tj T*')
        content.append('ET')
        stream = DecodedStreamObject(); stream.set_data('\n'.join(content).encode('ascii'))
        page[NameObject('/Contents')] = writer._add_object(stream)
    out = BytesIO(); writer.write(out)
    return out.getvalue()


class ParserTests(unittest.TestCase):
    def test_coordinate_formats_and_invalid_pairs(self):
        examples = [
            ('C04 N52 21 30.00 E013 30 20.00', ['C04']),
            ('C04 52°21′30.00″N 013°30′20.00″E', ['C04']),
            ('C04 N522130.00 E0133020.00', ['C04']),
            ('C04 522130.00N 0133020.00E', ['C04']),
            ('101 52,358333 13,505556', ['GAT101']),
            ('STANDPOSITION C11a\nN52 21 30.00\nE013 30 20.00', ['C11A']),
            ('C04 N52 21 30.00\nC05 E013 30 20.00', []),
            ('C04 52.358333 13.505556 C05 52.358400 13.505600', ['C04', 'C05']),
            ('C04 N52 61 30.00 E013 30 20.00', []),
            ('C04 N52 21 60.00 E013 30 20.00', []),
            ('C04 S52 21 30.00 W013 30 20.00', []),
            ('RUNWAY\n06L 52.358333 13.505556\nAIRCRAFT STANDS\nC04 52.358333 13.505556', ['C04']),
            ('C04\ntext\n52.358333 13.505556', []),
            ('E12 (STOP 1) 52.358333 13.505556', ['E12']),
            ('C04\nN52 21 30.00', []), ('E013 30 20.00', [])]
        for text, expected in examples:
            with self.subTest(text=text):
                self.assertEqual([row['name'] for row in parse_page(text)], expected)
        point = parse_page(examples[0][0])[0]
        self.assertAlmostEqual(point['latitude'], 52+21/60+30/3600)
        self.assertAlmostEqual(point['longitude'], 13+30/60+20/3600)

    def test_pdf_table_boundaries_keep_references_out_of_stands(self):
        rows = parse_pdf(pdf_bytes(pages()))
        self.assertEqual([r['name'] for r in rows if r['kind']=='stand'], ['GAT19','B05','B14','E35'])
        self.assertTrue(any(r['name']=='M7' and r['kind']=='stop_bar_cat_i' for r in rows))
        self.assertTrue(any(r['name']=='06L' and r['kind']=='runway' for r in rows))
        self.assertFalse(any(r['name'] in ('M07','M09') and r['kind']=='stand' for r in rows))
        unknown = pages(); unknown[18] = 'B05 52.362000 13.502000'
        with self.assertRaises(ImportFailure): parse_pdf(pdf_bytes(unknown))
        split = pages(); split[37] = 'E 35 (Stopp 2)\nN52 21 50.40\nE013 30 14.40\nM7 52.360000 13.500000'
        self.assertEqual([r['name'] for r in parse_document(split) if r['kind']=='stand'][-1], 'E35')


def firebase_refs(document, identity):
    refs = {}
    for name, data in [('document', document), ('identity', identity)]:
        info = dict(size=len(data), chunkCount=1 if data else 0, sha256=hashlib.sha256(data).hexdigest())
        ref = Mock()
        ref.get.return_value.to_dict.return_value = info
        ref.collection.return_value.document.return_value.get.return_value.to_dict.return_value = dict(index=0, data=data)
        refs[name] = ref
    db = Mock(); db.collection.return_value.document.side_effect = refs.__getitem__
    return db, refs


class ImportTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.pdf = pdf_bytes(pages())
        cls.secret = ' TEST-only secret with spaces '
        identity = pyrage.x25519.Identity.generate()
        cls.document = pyrage.encrypt(cls.pdf, [identity.to_public()])
        cls.identity = pyrage.passphrase.encrypt((str(identity)+'\n').encode(), cls.secret)
        cls.direct = pyrage.passphrase.encrypt(cls.pdf, cls.secret)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.secret_file = self.root/'secret'; self.secret_file.write_text(self.secret)
        self.env = patch.dict(os.environ, COMATALARM_SETUP_KEY='s'*64, COMATALARM_FIREBASE_CREDENTIALS='configured', COMATALARM_BER_PASSPHRASE_FILE=str(self.secret_file)); self.env.start()
        self.db = sqlite3.connect(self.root/'relay.sqlite3')
        self.model = StandModel(self.db, protect_index, unprotect_index)
        self.store = Mock()
        self.importer = BERPackageImporter(self.model, self.store)
        self.firebase, self.refs = firebase_refs(self.document, self.identity)
        self.firebase_patch = patch('firebase_admin.firestore.client', return_value=self.firebase); self.firebase_patch.start()

    def tearDown(self):
        self.firebase_patch.stop(); self.env.stop(); self.db.close(); self.temp.cleanup()

    def test_real_age_identity_and_direct_password_formats(self):
        self.assertEqual(decrypt_document(self.document, self.identity, self.secret), self.pdf)
        self.assertEqual(decrypt_document(self.direct, b'', self.secret), self.pdf)
        with self.assertRaises(ImportFailure) as failure:
            decrypt_document(self.document, self.identity, 'WRONG-PRIVATE-SECRET')
        self.assertNotIn('WRONG-PRIVATE-SECRET', str(failure.exception))

    async def test_direct_import_encrypted_restart_and_metadata_only_check(self):
        await self.importer.check(now=100)
        self.assertTrue(self.model.ready)
        self.assertIn('bereit', self.importer.status)
        disk = (self.root/'relay.sqlite3').with_suffix('.stands-backup').read_text()
        self.assertNotIn('B05', disk); self.assertNotIn(self.secret, disk)
        before = self.refs['document'].get.call_count
        await self.importer.check(now=101)
        self.assertEqual(self.refs['document'].get.call_count, before)
        restored = StandModel(self.db, protect_index, unprotect_index)
        self.assertTrue(restored.ready)
        # Same manifest after restart: neither blobs nor decryption are repeated.
        self.refs['document'].collection.reset_mock()
        again = BERPackageImporter(restored, self.store)
        with patch('push_relay.ber_package.decrypt_document', side_effect=AssertionError('must use cache')):
            await again.check(now=102)
        self.refs['document'].collection.assert_not_called()
        self.assertIn('bereit', again.status)

    async def test_corrupt_incomplete_changed_package_and_wrong_secret_preserve_learning(self):
        await self.importer.check(now=1)
        now = time.time()
        self.model.learn(dict(id='real',stand='B05',paxGate='C1',exit='',corridor='',runway='',timeBucket=0,airline='LH',aircraft='A320',observedAt=now), now)
        self.model.save()
        prior = copy.deepcopy(self.model.data)
        self.refs['document'].get.return_value.to_dict.return_value['sha256'] = '0'*64
        await self.importer.check(now=22000)
        self.assertEqual(self.model.data, prior)
        self.assertIn('Prüfsumme', self.importer.status)
        self.assertGreater(self.importer.next_check, 22000)
        self.firebase, self.refs = firebase_refs(self.document, self.identity)
        with patch('firebase_admin.firestore.client', return_value=self.firebase):
            changed_info = self.refs['document'].get.return_value.to_dict.return_value.copy()
            self.refs['document'].get.side_effect = [Mock(to_dict=lambda: changed_info), Mock(to_dict=lambda: dict(changed_info, sha256='f'*64))]
            with self.assertRaisesRegex(ImportFailure, 'aktualisiert'):
                fetch_package(self.store, self.secret, None)
        self.assertEqual(self.model.data, prior)
        self.firebase, self.refs = firebase_refs(self.document, self.identity)
        self.model.data.pop('firebase_package_source')
        prior = copy.deepcopy(self.model.data)
        self.secret_file.write_text('WRONG-PRIVATE-SECRET')
        with patch('firebase_admin.firestore.client', return_value=self.firebase):
            await self.importer.check(now=25000)
        self.assertEqual(self.model.data, prior)
        self.assertIn('Passphrase', self.importer.status)
        self.assertNotIn('WRONG-PRIVATE-SECRET', self.importer.status)

    async def test_empty_option_disables_without_wiping_and_no_error_leak(self):
        await self.importer.check(now=1)
        prior = copy.deepcopy(self.model.data)
        with patch.dict(os.environ, COMATALARM_BER_PASSPHRASE_FILE=''):
            await self.importer.check(now=22000)
        self.assertEqual(self.model.data, prior)
        with patch('push_relay.ber_package.fetch_package', side_effect=RuntimeError('SECRET upstream body')):
            with self.assertLogs(level='WARNING') as logs:
                await self.importer.check(now=23000)
        self.assertNotIn('SECRET', repr(logs.output))
        self.assertIn('bleibt verfügbar', self.importer.status)

    def upload_admin_package(self, model=None, rows=None):
        model = model or self.model
        rows = rows or parse_document(pages())
        return model.upload(dict(airport='BER', kind='package', digest='a'*64,
                                 rows=rows, offset=0, total=len(rows)), 'admin', time.time())

    async def test_admin_package_disables_firebase_after_restart_and_backup_restore(self):
        self.upload_admin_package()
        now = time.time()
        self.model.learn(dict(id='real',stand='B05',paxGate='C1',exit='',corridor='',runway='',timeBucket=0,airline='LH',aircraft='A320',observedAt=now), now)
        self.model.save()
        self.secret_file.write_text('OLD-WRONG-SECRET')
        prior = copy.deepcopy(self.model.data)
        for use_backup in (False, True):
            with self.subTest(backup=use_backup):
                if use_backup:
                    self.db.execute('DELETE FROM comat_stand_models'); self.db.commit()
                restored = StandModel(self.db, protect_index, unprotect_index)
                importer = BERPackageImporter(restored, self.store)
                with patch('push_relay.ber_package.fetch_package') as fetch, self.assertNoLogs(level='WARNING'):
                    await importer.check(now=1)
                    await importer.check(now=22000)
                fetch.assert_not_called()
                self.assertIn('Administrator-App bereit', importer.status)
                self.assertEqual(restored.data, prior)

    async def test_completed_admin_upload_wins_over_inflight_firebase_result_or_error(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                self.db.execute('DELETE FROM comat_stand_models'); self.db.commit()
                if self.model.backup: self.model.backup.unlink(missing_ok=True)
                self.model = StandModel(self.db, protect_index, unprotect_index)
                importer = BERPackageImporter(self.model, self.store)
                started, finish = asyncio.Event(), asyncio.Event()
                async def pending_fetch(*args):
                    started.set()
                    await finish.wait()
                    if fail: raise ImportFailure('BER-Paket konnte nicht entschlüsselt werden.')
                    return 'firebase-late', parse_document(pages())
                with patch('push_relay.ber_package.asyncio.to_thread', side_effect=pending_fetch), self.assertNoLogs(level='WARNING'):
                    task = asyncio.create_task(importer.check(now=1))
                    await started.wait()
                    self.upload_admin_package(rows=[dict(name='C04',latitude=52.362,longitude=13.502,kind='stand')])
                    prior = copy.deepcopy(self.model.data)
                    finish.set()
                    await task
                self.assertEqual(self.model.data, prior)
                self.assertIn('Administrator-App bereit', importer.status)
                self.assertEqual(importer.failures, 0)

    async def test_partial_admin_upload_does_not_disable_direct_import(self):
        rows = parse_document(pages())
        self.model.upload(dict(airport='BER', kind='package', digest='a'*64,
                              rows=rows[:1], offset=0, total=len(rows)), 'admin', time.time())
        with patch('push_relay.ber_package.fetch_package', return_value=('firebase', rows)) as fetch:
            await self.importer.check(now=1)
        fetch.assert_called_once()
        self.assertTrue(self.model.ready)
        self.assertNotIn('Administrator-App', self.importer.status)

    async def test_removed_or_blank_passphrase_file_never_attempts_decryption(self):
        for value in ('', ' \t\n', None):
            with self.subTest(value=value):
                if value is None: self.secret_file.unlink(missing_ok=True)
                else: self.secret_file.write_text(value)
                importer = BERPackageImporter(self.model, self.store)
                with patch('push_relay.ber_package.fetch_package') as fetch, self.assertNoLogs(level='WARNING'):
                    await importer.check(now=1)
                fetch.assert_not_called()
                self.assertIn('nicht eingerichtet', importer.status)

    async def test_missing_chunk_and_failed_persistence_do_not_replace_model(self):
        await self.importer.check(now=1)
        prior = copy.deepcopy(self.model.data)
        self.refs['identity'].collection.return_value.document.return_value.get.return_value.to_dict.return_value = {}
        with self.assertRaisesRegex(ImportFailure, 'vollständig'):
            fetch_package(self.store, self.secret, None)
        self.assertEqual(self.model.data, prior)
        self.db.execute('PRAGMA query_only=ON')
        with self.assertRaises(sqlite3.OperationalError):
            self.model.install_package(parse_document(pages()), 'failed-storage')
        self.assertEqual(self.model.data, prior)
        self.db.execute('PRAGMA query_only=OFF')
        self.assertEqual(StandModel(self.db, protect_index, unprotect_index).data, prior)

    async def test_import_runs_off_event_loop_and_keeps_observations(self):
        await self.importer.check(now=1)
        now = time.time()
        self.model.learn(dict(id='real',stand='B05',paxGate='C1',exit='',corridor='',runway='',timeBucket=0,airline='LH',aircraft='A320',observedAt=now), now)
        self.model.save()
        rows = parse_document(pages())
        def slow(*args):
            time.sleep(.1)
            return 'new-revision', rows
        with patch('push_relay.ber_package.fetch_package', side_effect=slow):
            task = asyncio.create_task(self.importer.check(now=22000))
            await asyncio.sleep(.01)
            self.assertFalse(task.done(), 'import must not block the asyncio event loop')
            await task
        self.assertEqual(len(self.model.data['observations']), 1)
        with self.assertRaises(ValueError): self.model.install_package([dict(name='bad',latitude=0,longitude=0)], 'bad')
        self.assertEqual(self.model.data['firebase_package_source'], 'new-revision')


class AddonOptionTests(unittest.TestCase):
    def test_passphrase_file_private_exact_and_removable(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        spec = importlib.util.spec_from_file_location('relay_addon_run', Path(__file__).parents[2]/'callwebhook_push_relay/run.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        key = ec.generate_private_key(ec.SECP256R1()).private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        options = dict(apns_team_id='ABCDEFGHIJ', apns_key_id='1234567890', apns_private_key=key, comatalarm_ber_passphrase=' exact secret ')
        with tempfile.TemporaryDirectory() as temp:
            env = module.configure(options, Path(temp))
            path = Path(env['COMATALARM_BER_PASSPHRASE_FILE'])
            self.assertEqual(path.read_text(), ' exact secret ')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(' exact secret ', repr(env))
            for empty in ('', ' \t\n', None):
                with self.subTest(empty=empty):
                    path.write_text('old secret')
                    options['comatalarm_ber_passphrase'] = empty
                    env = module.configure(options, Path(temp))
                    self.assertEqual(env['COMATALARM_BER_PASSPHRASE_FILE'], '')
                    self.assertFalse(path.exists())
            path.write_text('old secret')
            del options['comatalarm_ber_passphrase']
            env = module.configure(options, Path(temp))
            self.assertEqual(env['COMATALARM_BER_PASSPHRASE_FILE'], '')
            self.assertFalse(path.exists())
