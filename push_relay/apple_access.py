"""Server-only Apple/TestFlight grants. No client or master can mint BER access."""
import asyncio
import os
import time
from aiohttp import web, ClientError
from .testflight import AppleError, failure

PROJECT = "comat-ber-alarm"
TTL_SECONDS = 180


class AccessDenied(Exception):
    pass


def apple_identity(claims, user):
    firebase = claims.get("firebase", {})
    identities = firebase.get("identities", {}).get("apple.com", [])
    providers = [p for p in user.provider_data if p.provider_id == "apple.com"]
    if firebase.get("sign_in_provider") != "apple.com" or len(identities) != 1 or len(providers) != 1:
        raise AccessDenied("Bitte mit Apple anmelden und E-Mail teilen auswählen.")
    provider = providers[0]
    email = (provider.email or "").strip().casefold()
    if (not email or len(email) > 254 or "@" not in email
            or email.endswith("@privaterelay.appleid.com")
            or claims.get("email_verified") is not True
            or email != str(claims.get("email", "")).casefold()
            or provider.uid != identities[0]):
        raise AccessDenied("Für die Testerprüfung ist E-Mail teilen erforderlich. Eine private Relay-Adresse wird nicht freigegeben.")
    return provider.uid, email


class FirebaseAccessStore:
    def __init__(self):
        self.app = None

    def ready(self):
        if self.app is not None:
            return
        filename = os.environ.get("COMATALARM_FIREBASE_CREDENTIALS", "")
        if not filename:
            raise AppleError("Apple-Gerätefreigabe ist auf dem Push-Dienst noch nicht eingerichtet.")
        import firebase_admin
        from firebase_admin import credentials
        credential = credentials.Certificate(filename)
        if credential.project_id != PROJECT:
            raise AppleError("Firebase-Projekt der Gerätefreigabe stimmt nicht.")
        self.app = firebase_admin.initialize_app(credential, {"projectId": PROJECT}, name="comatalarm-access")

    def verify(self, device_token, apple_token):
        self.ready()
        from firebase_admin import auth
        try:
            device = auth.verify_id_token(device_token, app=self.app, check_revoked=True)
            apple = auth.verify_id_token(apple_token, app=self.app, check_revoked=True)
            user = auth.get_user(apple["uid"], app=self.app)
            subject, email = apple_identity(apple, user)
            if device["uid"] == apple["uid"]:
                raise AccessDenied("Apple-Konto und Geräteanmeldung müssen getrennt bleiben.")
            return device["uid"], apple["uid"], subject, email
        except AccessDenied:
            raise
        except Exception:
            raise AccessDenied("Anmeldung konnte nicht bestätigt werden. Bitte erneut anmelden.") from None

    def device_uid(self, token):
        """Authenticate the existing device identity, without another Apple check."""
        self.ready()
        from firebase_admin import auth
        try:
            return auth.verify_id_token(token, app=self.app, check_revoked=True)["uid"]
        except Exception:
            raise AccessDenied("Firebase-Geräteanmeldung konnte nicht bestätigt werden.") from None

    def device_access(self, uid):
        self.ready()
        from firebase_admin import firestore
        db = firestore.client(app=self.app)
        device = db.collection("sync_devices").document(uid).get().to_dict() or {}
        if device.get("uid") != uid or device.get("approved") is not True or device.get("blocked") is True:
            raise AccessDenied("Firebase-Gerät ist nicht freigegeben.")
        administrator = db.collection("sync_meta").document("master").get().to_dict() or {}
        return {"uid": uid, "administrator": uid == (administrator.get("uid") or "tmsSpw6bYbdVf6WMtgCjUozXZ252")}

    def grant(self, uid, account_uid, subject, email):
        from firebase_admin import firestore
        from google.cloud.firestore import transactional
        db = firestore.client(app=self.app)
        device_ref = db.collection("sync_devices").document(uid)
        access_ref = db.collection("sync_apple_access").document(uid)
        event_ref = db.collection("sync_approval_events").document()
        now = int(time.time() * 1000)
        expires = now + TTL_SECONDS * 1000

        @transactional
        def commit(transaction):
            device = device_ref.get(transaction=transaction).to_dict() or {}
            prior = access_ref.get(transaction=transaction).to_dict() or {}
            if device.get("uid") != uid or device.get("blocked") is True:
                raise AccessDenied("Gerät ist nicht registriert oder wurde vom Master gesperrt.")
            if prior.get("appleSubject") not in (None, subject):
                raise AccessDenied("Dieses Gerät ist bereits einem anderen Apple-Konto zugeordnet.")
            transaction.set(access_ref, {"uid": uid, "accountUID": account_uid, "appleSubject": subject,
                "email": email, "checkedAt": now, "expiresAt": expires, "source": "internal-testflight"})
            transaction.update(device_ref, {"approved": True, "appleEmail": email,
                "approvalSource": "internal-testflight", "appleAccountUID": account_uid})
            if not prior or device.get("approved") is not True:
                transaction.set(event_ref, {"uid": uid, "email": email, "createdAt": now,
                    "deviceName": device.get("deviceName", "Gerät ohne Namen"),
                    "deviceModel": device.get("deviceModel", ""), "type": "automatic-approval"})
            return expires
        return commit(db.transaction())

    def revoke(self, uid):
        from firebase_admin import firestore
        # Keep the binding so a different Apple account cannot take over the device.
        firestore.client(app=self.app).collection("sync_apple_access").document(uid).set({"expiresAt": 0}, merge=True)


class AppleAccess:
    def __init__(self, relay, testflight, store=None):
        self.relay, self.testflight = relay, testflight
        self.store = store or FirebaseAccessStore()
        self.lock = asyncio.Lock()

    async def approve(self, request):
        self.relay.limit("apple-access-ip:" + (request.remote or "unknown"), 10, 60)
        bearer = request.headers.get("Authorization", "")
        try:
            body = await request.json()
            token = body.get("appleToken") if isinstance(body, dict) else None
            if not bearer.startswith("Bearer ") or not isinstance(token, str) or not 20 <= len(token) <= 16000 or len(bearer) > 16007:
                raise AccessDenied("Gültige Geräte- und Apple-Anmeldung erforderlich.")
            async with self.lock:
                uid, account, subject, email = await asyncio.to_thread(self.store.verify, bearer[7:], token)
                self.relay.limit("apple-access-device:" + uid, 4, 60)
                if not await self.testflight.is_internal_tester(email):
                    await asyncio.to_thread(self.store.revoke, uid)
                    raise AccessDenied("Diese bestätigte E-Mail-Adresse gehört keinem internen ComatAlarm-Tester.")
                expires = await asyncio.to_thread(self.store.grant, uid, account, subject, email)
            return web.json_response({"approved": True, "email": email, "expiresAt": expires})
        except AccessDenied as error:
            return failure(str(error), 403)
        except (AppleError, ClientError, asyncio.TimeoutError):
            return failure("Testerprüfung momentan nicht verfügbar. Das Standplatzpaket bleibt gesperrt.", 503)
        except web.HTTPException:
            raise
        except Exception:
            # Never log identity tokens, service credentials or private user data.
            return failure("Gerätefreigabe konnte nicht sicher bestätigt werden.", 503)

