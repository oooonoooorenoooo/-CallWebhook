"""Single-process relay: verified app enrollment, installation-scoped HA grants.

Run behind HTTPS. The APNs key is mounted only here, never returned to clients.
"""
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from uuid import UUID

from aiohttp import web
from aioapns import APNs, NotificationRequest, PushType

from .attestation import decode, verify_assertion, verify_attestation

BUNDLE_ID = "de.reno.CallWebhook.U98PKCA4W7"


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Relay:
    def __init__(self, database, app_id, root_pem, sender, allow_development=False):
        self.db = sqlite3.connect(database)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS devices (
              key_id TEXT PRIMARY KEY, public_key TEXT NOT NULL,
              counter INTEGER NOT NULL, environment TEXT NOT NULL,
              token TEXT NOT NULL, credential_hash TEXT NOT NULL UNIQUE,
              updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS challenges (
              id TEXT PRIMARY KEY, payload TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS deliveries (
              key_id TEXT NOT NULL, call_id TEXT NOT NULL, created REAL NOT NULL,
              PRIMARY KEY(key_id, call_id));
            CREATE TABLE IF NOT EXISTS limits (
              bucket TEXT PRIMARY KEY, count INTEGER NOT NULL, expires REAL NOT NULL);
        """)
        self.app_id, self.root_pem, self.sender = app_id, root_pem, sender
        self.allow_development = allow_development

    def limit(self, bucket, count, seconds):
        now = time.time()
        with self.db:
            self.db.execute("DELETE FROM limits WHERE expires < ?", (now,))
            row = self.db.execute("SELECT count FROM limits WHERE bucket=?", (bucket,)).fetchone()
            if row and row[0] >= count:
                raise web.HTTPTooManyRequests()
            self.db.execute("INSERT INTO limits VALUES (?,1,?) ON CONFLICT(bucket) DO UPDATE SET count=count+1", (bucket, now + seconds))

    async def challenge(self, request):
        # Peer IP only: never trust arbitrary forwarded headers from clients.
        self.limit("enroll:" + (request.remote or "unknown"), 60, 60)
        body = await request.json()
        key_id = body["key_id"]
        if len(decode(key_id, 32)) != 32:
            raise ValueError("Invalid key")
        token, environment, credential_hash = body["token"], body["environment"], body["credential_hash"]
        if (not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32,512}", token)
                or not isinstance(credential_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", credential_hash)
                or environment not in (("production", "development") if self.allow_development else ("production",))):
            raise ValueError("Invalid registration")
        existing = self.db.execute("SELECT environment FROM devices WHERE key_id=?", (key_id,)).fetchone()
        if existing and existing[0] != environment:
            raise ValueError("Environment cannot change")
        now = time.time()
        with self.db:
            self.db.execute("DELETE FROM challenges WHERE expires < ?", (now,))
            if self.db.execute("SELECT count(*) FROM challenges").fetchone()[0] >= 10000:
                raise web.HTTPServiceUnavailable()
            nonce, identifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            payload = dict(nonce=nonce, key_id=key_id, token=token, environment=environment, credential_hash=credential_hash)
            self.db.execute("INSERT INTO challenges VALUES (?,?,?)", (identifier, json.dumps(payload), now + 120))
        return web.json_response(dict(challenge_id=identifier, nonce=nonce, attested=bool(existing)))

    async def register(self, request):
        body = await request.json()
        with self.db:
            challenge = self.db.execute("SELECT * FROM challenges WHERE id=?", (body["challenge_id"],)).fetchone()
            if not challenge or challenge["expires"] < time.time():
                raise web.HTTPUnauthorized()
            # Consume before validation; failed proofs cannot be retried/replayed.
            self.db.execute("DELETE FROM challenges WHERE id=?", (body["challenge_id"],))
        data = json.loads(challenge["payload"])
        row = self.db.execute("SELECT * FROM devices WHERE key_id=?", (data["key_id"],)).fetchone()
        try:
            if row:
                counter = verify_assertion(body["assertion"], data, self.app_id, row["public_key"], row["counter"])
                pem = row["public_key"]
            else:
                pem = verify_attestation(body["attestation"], data, self.app_id, self.root_pem)
                counter = 0
        except Exception:
            # Never log proof objects, device tokens, caller numbers or grants.
            raise web.HTTPUnauthorized(text="App verification failed") from None
        with self.db:
            self.db.execute("""INSERT INTO devices VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(key_id) DO UPDATE SET public_key=excluded.public_key,
                counter=excluded.counter, token=excluded.token,
                credential_hash=excluded.credential_hash, updated=excluded.updated""",
                (data["key_id"], pem, counter, data["environment"], data["token"], data["credential_hash"], time.time()))
        return web.json_response({"registered": True})

    def device(self, request):
        authorization = request.headers.get("Authorization", "")
        if not re.fullmatch(r"Bearer [0-9a-f]{64}", authorization):
            raise web.HTTPUnauthorized()
        row = self.db.execute("SELECT * FROM devices WHERE credential_hash=?", (sha(authorization[7:]),)).fetchone()
        if not row:
            raise web.HTTPUnauthorized()
        return row

    async def registration(self, request):
        row = self.device(request)
        return web.json_response({"registered": True, "token_hash": sha(row["token"]), "environment": row["environment"]})

    async def revoke(self, request):
        row = self.device(request)
        with self.db:
            self.db.execute("DELETE FROM devices WHERE key_id=?", (row["key_id"],))
        return web.json_response({"revoked": True})

    async def ring(self, request):
        row = self.device(request)
        body = await request.json()
        # An HA grant may ring only its registered device, never arbitrary tokens.
        if set(body) != {"call_id", "caller"} or not isinstance(body["caller"], str) or len(body["caller"]) > 128:
            raise ValueError("Invalid call")
        call_id = str(UUID(body["call_id"]))
        now = time.time()
        with self.db:
            self.db.execute("DELETE FROM deliveries WHERE created < ?", (now - 3600,))
            if self.db.execute("SELECT 1 FROM deliveries WHERE key_id=? AND call_id=?", (row["key_id"], call_id)).fetchone():
                return web.json_response({"accepted": True, "duplicate": True})
            self.limit("ring-minute:" + row["key_id"], 12, 60)
            self.limit("ring-hour:" + row["key_id"], 120, 3600)
            self.db.execute("INSERT INTO deliveries VALUES (?,?,?)", (row["key_id"], call_id, now))
        try:
            accepted = await self.sender(row["token"], row["environment"], call_id, body["caller"])
        except Exception:
            accepted = False
        if not accepted:
            with self.db:
                self.db.execute("DELETE FROM deliveries WHERE key_id=? AND call_id=?", (row["key_id"], call_id))
            return web.json_response({"accepted": False}, status=502)
        return web.json_response({"accepted": True})


@web.middleware
async def errors(request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except (ValueError, TypeError, KeyError, sqlite3.IntegrityError):
        return web.json_response({"error": "Invalid request"}, status=400)


def application(relay):
    app = web.Application(client_max_size=32768, middlewares=[errors])
    app.router.add_post("/v1/challenge", relay.challenge)
    app.router.add_post("/v1/register", relay.register)
    app.router.add_get("/v1/registration", relay.registration)
    app.router.add_delete("/v1/registration", relay.revoke)
    app.router.add_post("/v1/ring", relay.ring)
    async def health(request):
        return web.json_response({"ready": True})
    app.router.add_get("/healthz", health)
    async def close(app):
        relay.db.close()
    app.on_cleanup.append(close)
    return app


def main():
    os.umask(0o077)
    team_id, key_id = os.environ["APNS_TEAM_ID"], os.environ["APNS_KEY_ID"]
    if not re.fullmatch(r"[A-Z0-9]{10}", team_id) or not re.fullmatch(r"[A-Z0-9]{10}", key_id):
        raise ValueError("Invalid Apple key/team ID")
    key = Path(os.environ.get("APNS_KEY_FILE", "/run/secrets/apns.p8")).read_text()
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from cryptography.hazmat.primitives.asymmetric import ec
    parsed = load_pem_private_key(key.encode(), password=None)
    if not isinstance(parsed, ec.EllipticCurvePrivateKey) or not isinstance(parsed.curve, ec.SECP256R1):
        raise ValueError("APNs requires a P-256 key")
    clients = {}
    async def send(token, environment, call_id, caller):
        if environment not in clients:
            clients[environment] = APNs(key=key, key_id=key_id, team_id=team_id,
                topic=BUNDLE_ID + ".voip", use_sandbox=environment == "development",
                max_connections=2, max_connection_attempts=1)
        result = await asyncio.wait_for(clients[environment].send_notification(NotificationRequest(
            device_token=token, notification_id=call_id, push_type=PushType.VOIP,
            priority=10, time_to_live=5,
            message={"aps": {}, "call_id": call_id, "caller": caller, "sent_at": int(time.time())})), timeout=4)
        if not result.is_successful:
            logging.warning("APNs rejected a push: %s %s", result.status, result.description)
        return result.is_successful
    relay = Relay(os.environ.get("RELAY_DATABASE", "/data/relay.sqlite3"),
        os.environ.get("APP_ID_PREFIX", team_id) + "." + BUNDLE_ID,
        Path(__file__).with_name("apple-app-attest-root.pem").read_bytes(), send,
        allow_development=os.environ.get("ALLOW_DEVELOPMENT") == "1")
    web.run_app(application(relay), host="0.0.0.0", port=8080, access_log=None)


if __name__ == "__main__":
    main()
