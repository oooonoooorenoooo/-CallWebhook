import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

from aiohttp.test_utils import TestClient, TestServer
import cbor2
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from push_relay.attestation import client_data, digest, verify_assertion, verify_attestation
from push_relay.server import Relay, application, sha

APP_ID = 'ABCDEFGHIJ.de.reno.CallWebhook.U98PKCA4W7'


def b64(value):
    return base64.b64encode(value).decode()


class AppleProof:
    """Independent test CA/proofs exercise real certificate/signature validation."""
    def __init__(self):
        self.root_key = ec.generate_private_key(ec.SECP384R1())
        self.ca_key = ec.generate_private_key(ec.SECP384R1())
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.root = self.certificate('test root', self.root_key, self.root_key, ca=True)
        self.ca = self.certificate('test CA', self.ca_key, self.root_key, issuer=self.root, ca=True)
        self.root_pem = self.root.public_bytes(serialization.Encoding.PEM)
        self.pem = self.key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        point = self.key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.identifier = digest(point)
        self.key_id = b64(self.identifier)

    def certificate(self, name, key, signer, issuer=None, ca=False, nonce=None, expired=False):
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        now = datetime.now(timezone.utc)
        builder = (x509.CertificateBuilder().subject_name(subject)
            .issuer_name(issuer.subject if issuer else subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=2))
            .not_valid_after(now + timedelta(days=-1 if expired else 2))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
        if nonce is not None:
            builder = builder.add_extension(x509.UnrecognizedExtension(
                x509.ObjectIdentifier('1.2.840.113635.100.8.2'), b'\x30\x24\xa1\x22\x04\x20' + nonce), critical=False)
        return builder.sign(signer, hashes.SHA256())

    def challenge(self, **kw):
        result = dict(nonce=secrets.token_urlsafe(32), key_id=self.key_id, token='a'*64,
                      environment='production', credential_hash=sha('d'*64))
        result.update(kw)
        return result

    def attest(self, challenge, app_id=APP_ID, environment='production', expired=False, category=None):
        key = self.key.public_key().public_numbers()
        flags = 0x40 | (0x80 if category is not None else 0)
        aaguid = b'appattest' + bytes(7) if environment == 'production' else b'appattestdevelop'
        auth = digest(app_id.encode()) + bytes([flags]) + bytes(4) + aaguid + b'\x00\x20' + self.identifier
        auth += cbor2.dumps({1: 2, 3: -7, -1: 1, -2: key.x.to_bytes(32, 'big'), -3: key.y.to_bytes(32, 'big')})
        if category is not None:
            auth += cbor2.dumps({'apple_validation_category_01': category.to_bytes(4, 'little'), 'apple_bundle_version_01': '273'})
        leaf = self.certificate('device', self.key, self.ca_key, issuer=self.ca,
            nonce=digest(auth + digest(client_data(challenge))), expired=expired)
        return b64(cbor2.dumps(dict(fmt='apple-appattest', authData=auth,
            attStmt=dict(x5c=[leaf.public_bytes(serialization.Encoding.DER), self.ca.public_bytes(serialization.Encoding.DER)], receipt=b'test'))))

    def assertion(self, challenge, counter=1, app_id=APP_ID):
        auth = digest(app_id.encode()) + b'\x00' + counter.to_bytes(4, 'big')
        signature = self.key.sign(auth + digest(client_data(challenge)), ec.ECDSA(hashes.SHA256()))
        return b64(cbor2.dumps(dict(authenticatorData=auth, signature=signature)))


class AttestationTests(unittest.TestCase):
    def setUp(self):
        self.proof = AppleProof()
        self.challenge = self.proof.challenge()

    def verify(self, encoded, challenge=None, root=None):
        return verify_attestation(encoded, challenge or self.challenge, APP_ID, root or self.proof.root_pem)

    def test_valid_attestation_and_new_os_extensions(self):
        for category in (None, 2, 4):
            self.assertEqual(self.verify(self.proof.attest(self.challenge, category=category)), self.proof.pem)

    def test_wrong_ca_app_environment_expired_cert_and_distribution_rejected(self):
        encoded = self.proof.attest(self.challenge)
        with self.assertRaises(Exception): self.verify(encoded, root=AppleProof().root_pem)
        for options in (dict(app_id='other.app'), dict(environment='development'), dict(expired=True), dict(category=3), dict(category=10)):
            with self.assertRaises(Exception, msg=str(options)):
                self.verify(self.proof.attest(self.challenge, **options))

    def test_every_registration_field_bound_to_proof(self):
        encoded = self.proof.attest(self.challenge)
        for field in ('nonce', 'key_id', 'token', 'environment', 'credential_hash'):
            changed = dict(self.challenge, **{field: 'tampered'})
            with self.assertRaises(Exception, msg=field): self.verify(encoded, challenge=changed)

    def test_assertion_signature_identity_and_monotonic_counter(self):
        assertion = self.proof.assertion(self.challenge, counter=2)
        self.assertEqual(verify_assertion(assertion, self.challenge, APP_ID, self.proof.pem, 1), 2)
        for challenge, app_id, pem, previous in (
                (self.challenge, APP_ID, self.proof.pem, 2),
                (dict(self.challenge, token='b'*64), APP_ID, self.proof.pem, 0),
                (self.challenge, 'other.app', self.proof.pem, 0),
                (self.challenge, APP_ID, AppleProof().pem, 0)):
            with self.assertRaises(Exception): verify_assertion(assertion, challenge, app_id, pem, previous)


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.proof = AppleProof()
        self.directory = tempfile.TemporaryDirectory()
        self.sender = AsyncMock(return_value=True)
        self.relay = Relay(str(Path(self.directory.name)/'relay.db'), APP_ID, self.proof.root_pem, self.sender)
        self.client = TestClient(TestServer(application(self.relay)))
        await self.client.start_server()
        self.headers = {'Authorization': 'Bearer ' + 'd'*64}

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    async def challenge(self, token='a'*64):
        response = await self.client.post('/v1/challenge', json=dict(key_id=self.proof.key_id,
            token=token, environment='production', credential_hash=sha('d'*64)))
        self.assertEqual(response.status, 200)
        body = await response.json()
        return body, self.proof.challenge(nonce=body['nonce'], token=token)

    async def enroll(self):
        body, data = await self.challenge()
        response = await self.client.post('/v1/register', json=dict(challenge_id=body['challenge_id'], attestation=self.proof.attest(data)))
        self.assertEqual(response.status, 200)

    async def test_registration_token_rotation_and_no_secret_in_database(self):
        await self.enroll()
        row = dict(self.relay.db.execute('SELECT * FROM devices').fetchone())
        self.assertNotIn('d'*64, json.dumps(row))
        body, data = await self.challenge(token='b'*64)
        self.assertTrue(body['attested'])
        payload = dict(challenge_id=body['challenge_id'], assertion=self.proof.assertion(data))
        self.assertEqual((await self.client.post('/v1/register', json=payload)).status, 200)
        self.assertEqual((await self.client.post('/v1/register', json=payload)).status, 401)
        state = await (await self.client.get('/v1/registration', headers=self.headers)).json()
        self.assertEqual(state['token_hash'], sha('b'*64))
        self.assertNotIn('token', state)

    async def test_one_time_challenges_expire_and_bad_proofs_do_not_register(self):
        body, data = await self.challenge()
        payload = dict(challenge_id=body['challenge_id'], attestation=self.proof.attest(dict(data, token='b'*64)))
        self.assertEqual((await self.client.post('/v1/register', json=payload)).status, 401)
        payload['attestation'] = self.proof.attest(data)
        self.assertEqual((await self.client.post('/v1/register', json=payload)).status, 401)
        body, data = await self.challenge()
        with self.relay.db:
            self.relay.db.execute('UPDATE challenges SET expires=0')
        self.assertEqual((await self.client.post('/v1/register', json=dict(challenge_id=body['challenge_id'], attestation=self.proof.attest(data)))).status, 401)
        self.assertEqual(self.relay.db.execute('SELECT count(*) FROM devices').fetchone()[0], 0)

    async def test_send_is_bound_to_device_deduplicated_and_revocable(self):
        await self.enroll()
        body = dict(call_id=str(uuid4()), caller='030123')
        self.assertEqual((await self.client.post('/v1/ring', json=body)).status, 401)
        self.assertEqual((await self.client.post('/v1/ring', headers=self.headers, json=dict(body, token='victim'))).status, 400)
        for _ in range(2):
            self.assertEqual((await self.client.post('/v1/ring', headers=self.headers, json=body)).status, 200)
        self.sender.assert_awaited_once_with('a'*64, 'production', body['call_id'], '030123')
        self.assertEqual((await self.client.delete('/v1/registration', headers=self.headers)).status, 200)
        self.assertEqual((await self.client.post('/v1/ring', headers=self.headers, json=body)).status, 401)

    async def test_provider_failure_not_reported_as_success_and_rate_limit(self):
        await self.enroll()
        self.sender.return_value = False
        body = dict(call_id=str(uuid4()), caller='030123')
        response = await self.client.post('/v1/ring', headers=self.headers, json=body)
        self.assertEqual(response.status, 502)
        self.sender.return_value = True
        self.assertEqual((await self.client.post('/v1/ring', headers=self.headers, json=body)).status, 200)
        for _ in range(10):
            self.assertEqual((await self.client.post('/v1/ring', headers=self.headers, json=dict(body, call_id=str(uuid4())))).status, 200)
        self.assertEqual((await self.client.post('/v1/ring', headers=self.headers, json=dict(body, call_id=str(uuid4())))).status, 429)


if __name__ == '__main__':
    unittest.main()
