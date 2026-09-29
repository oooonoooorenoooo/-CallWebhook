"""Real mailbox/cache functions with a simulated FRITZ!Box, no live messages."""
import ast
from datetime import datetime
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, quote, urlparse
import xml.etree.ElementTree as ET

SOURCE = Path(__file__).parents[1] / 'homeassistant/custom_components/callwebhook/__init__.py'

class MailboxAudioTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name)
        names = {'get_text', 'sort_key', 'fetch_fritz_mailbox_for_tam', 'fetch_fritz_mailbox', 'ensure_message_audio'}
        nodes = [n for n in ast.parse(SOURCE.read_text()).body if isinstance(n, ast.FunctionDef) and n.name in names]
        self.audio_gets = []
        self.entries = [('0', '0'), ('0', '1'), ('2', '0')]
        def response(data, content_type='text/xml'):
            return SimpleNamespace(content=data, text=data.decode(errors='replace'), headers={'Content-Type': content_type}, raise_for_status=lambda: None)
        def control(action, body):
            tam = ET.fromstring(body).text
            return response(f'<Response><NewURL>http://fritz/list?tam={tam}&amp;sid=fresh</NewURL></Response>'.encode())
        def get(url, **kwargs):
            if '/list?' in url:
                tam = parse_qs(urlparse(url).query)['tam'][0]
                records = ''.join(f'<Message><Tam>{t}</Tam><Index>{i}</Index><Date>29.09.26 20:00</Date><Path>/data/tam/rec.{t}.{i}</Path></Message>' for t, i in self.entries if t == tam)
                return response(f'<Root>{records}</Root>'.encode())
            self.audio_gets.append(url)
            return response(b'RIFF-test-recording', 'audio/wav')
        self.ns = dict(Path=Path, datetime=datetime, json=json, ET=ET, urlparse=urlparse, parse_qs=parse_qs, quote=quote,
                       BASE_DIR=path, XML_FILE=path/'mailbox.xml', MAILBOX_FILE=path/'mailbox.json', HOST='fritz',
                       get_auth=lambda: None, get_configured_tams=lambda: ('0', '2'),
                       tam_control_request=Mock(side_effect=control), requests=SimpleNamespace(get=Mock(side_effect=get)))
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), self.ns)

    def test_list_does_not_download_audio(self):
        result = self.ns['fetch_fritz_mailbox']()
        self.assertEqual(len(result), 3)
        self.assertEqual(self.audio_gets, [])
        self.assertEqual(result[0]['audio'], '/api/callwebhook/audio/0/0')

    def test_cleanup_keeps_real_filenames_and_removes_only_deleted_messages(self):
        path = self.ns['BASE_DIR']
        wanted = path/'voicemail_0_0.wav'
        deleted = path/'voicemail_0_99.wav'
        wanted.write_bytes(b'recording')
        deleted.write_bytes(b'obsolete')
        self.ns['fetch_fritz_mailbox']()
        self.assertTrue(wanted.exists())
        self.assertFalse(deleted.exists())
        self.assertEqual(self.audio_gets, [])

    def test_missing_audio_recovers_only_selected_message_and_is_reused(self):
        file = self.ns['ensure_message_audio']('0', '1')
        self.assertEqual(file.read_bytes(), b'RIFF-test-recording')
        self.assertEqual(len(self.audio_gets), 1)
        self.assertIn('rec.0.1', self.audio_gets[0])
        self.ns['fetch_fritz_mailbox']()
        self.assertTrue(file.exists())
        self.assertEqual(self.ns['ensure_message_audio']('0', '1'), file)
        self.assertEqual(len(self.audio_gets), 1)

    def test_genuinely_missing_message_does_not_download_or_create_audio(self):
        self.assertIsNone(self.ns['ensure_message_audio']('0', '99'))
        self.assertEqual(self.audio_gets, [])
        with self.assertRaises(ValueError):
            self.ns['ensure_message_audio']('../0', '1')
