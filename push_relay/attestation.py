"""Apple App Attest validation. No client-supplied trust anchors are accepted.

Protocol: https://developer.apple.com/documentation/devicecheck/
validating-apps-that-connect-to-your-server
"""
import base64
import hashlib
import io

import cbor2
from OpenSSL import crypto
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec


def digest(data):
    return hashlib.sha256(data).digest()


def decode(value, maximum=20000):
    if not isinstance(value, str) or len(value) > maximum * 2:
        raise ValueError("Invalid base64 value")
    result = base64.b64decode(value, validate=True)
    if len(result) > maximum:
        raise ValueError("Oversized value")
    return result


def cbor(data):
    stream = io.BytesIO(data)
    result = cbor2.CBORDecoder(stream).decode()
    if stream.read():
        raise ValueError("Trailing CBOR data")
    return result


def client_data(challenge):
    # All registration properties are bound to the Apple proof, not just a nonce.
    fields = ("callwebhook-register-v1", challenge["nonce"], challenge["key_id"],
              challenge["environment"], challenge["token"], challenge["credential_hash"])
    return "\n".join(fields).encode()


def validate_extensions(data, environment):
    if not data:
        raise ValueError("Missing extension data")
    extensions = cbor(data)
    category = extensions.get("apple_validation_category_01")
    version = extensions.get("apple_bundle_version_01")
    # These properties are supplied by newer OS versions. Older, valid App Attest
    # proofs have no extension flag and are checked with the original protocol.
    if not isinstance(category, bytes) or len(category) != 4:
        raise ValueError("Missing validation category")
    allowed = {2, 4} if environment == "production" else {3, 5}
    if int.from_bytes(category, "little") not in allowed:
        raise ValueError("Distribution category is not allowed")
    if not isinstance(version, str) or not version or len(version) > 100:
        raise ValueError("Missing bundle version")


def verify_attestation(encoded, challenge, app_id, root_pem):
    obj = cbor(decode(encoded))
    if obj["fmt"] != "apple-appattest":
        raise ValueError("Not App Attest")
    chain = obj["attStmt"]["x5c"]
    if not isinstance(chain, list) or len(chain) != 2:
        raise ValueError("Invalid certificate chain")
    store = crypto.X509Store()
    store.add_cert(crypto.load_certificate(crypto.FILETYPE_PEM, root_pem))
    certs = [crypto.load_certificate(crypto.FILETYPE_ASN1, item) for item in chain]
    crypto.X509StoreContext(store, certs[0], certs[1:]).verify_certificate()
    leaf = x509.load_der_x509_certificate(chain[0])
    public_key = leaf.public_key()
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(public_key.curve, ec.SECP256R1):
        raise ValueError("Invalid key type")
    auth = obj["authData"]
    key_id = decode(challenge["key_id"], 32)
    if len(auth) < 87 or auth[:32] != digest(app_id.encode()) or not auth[32] & 0x40:
        raise ValueError("Wrong app identity")
    if auth[33:37] != bytes(4) or auth[53:55] != b"\x00\x20" or auth[55:87] != key_id:
        raise ValueError("Invalid credential data")
    expected = b"appattest" + bytes(7) if challenge["environment"] == "production" else b"appattestdevelop"
    if auth[37:53] != expected:
        raise ValueError("Wrong attestation environment")
    point = public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    if digest(point) != key_id:
        raise ValueError("Wrong key identifier")
    nonce = digest(auth + digest(client_data(challenge)))
    extension = leaf.extensions.get_extension_for_oid(x509.ObjectIdentifier("1.2.840.113635.100.8.2")).value.value
    # Apple's DER SEQUENCE -> [1] -> OCTET STRING (32 bytes), exact encoding.
    if extension != b"\x30\x24\xa1\x22\x04\x20" + nonce:
        raise ValueError("Wrong challenge nonce")
    stream = io.BytesIO(auth[87:])
    cose = cbor2.CBORDecoder(stream).decode()
    numbers = public_key.public_numbers()
    if (cose.get(1), cose.get(3), cose.get(-1), cose.get(-2), cose.get(-3)) != (
            2, -7, 1, numbers.x.to_bytes(32, "big"), numbers.y.to_bytes(32, "big")):
        raise ValueError("Wrong embedded public key")
    remaining = stream.read()
    if auth[32] & 0x80:
        validate_extensions(remaining, challenge["environment"])
    elif remaining:
        raise ValueError("Unexpected authenticator data")
    return public_key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def verify_assertion(encoded, challenge, app_id, pem, previous_counter):
    obj = cbor(decode(encoded))
    auth = obj["authenticatorData"]
    if len(auth) < 37 or auth[:32] != digest(app_id.encode()) or auth[32] & 0x40:
        raise ValueError("Wrong app identity")
    counter = int.from_bytes(auth[33:37], "big")
    if counter <= previous_counter:
        raise ValueError("Replayed assertion")
    if auth[32] & 0x80:
        validate_extensions(auth[37:], challenge["environment"])
    elif len(auth) != 37:
        raise ValueError("Unexpected authenticator data")
    key = serialization.load_pem_public_key(pem.encode())
    key.verify(obj["signature"], auth + digest(client_data(challenge)), ec.ECDSA(hashes.SHA256()))
    return counter
