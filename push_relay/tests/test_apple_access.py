import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from push_relay.apple_access import AppleAccess, AccessDenied, apple_identity
from push_relay.testflight import TestFlight, AppleError


def identity(email="tester@example.org", subject="apple-user"):
    claims = {"email": email, "email_verified": True, "firebase": {"sign_in_provider": "apple.com", "identities": {"apple.com": [subject]}}}
    user = SimpleNamespace(provider_data=[SimpleNamespace(provider_id="apple.com", uid=subject, email=email)])
    return claims, user


class IdentityTests(unittest.TestCase):
    def test_verified_shared_email(self):
        self.assertEqual(apple_identity(*identity()), ("apple-user", "tester@example.org"))

    def test_hidden_email_is_denied(self):
        with self.assertRaises(AccessDenied):
            apple_identity(*identity("hidden@privaterelay.appleid.com"))

    def test_unverified_or_changed_email_or_provider_is_denied(self):
        for key, value in [("email_verified", False), ("email", "someone-else@example.org"), ("firebase", {})]:
            claims, user = identity()
            claims[key] = value
            with self.assertRaises(AccessDenied):
                apple_identity(claims, user)

    def test_apple_subject_must_match_verified_provider(self):
        claims, user = identity()
        user.provider_data[0].uid = "different-apple-user"
        with self.assertRaises(AccessDenied):
            apple_identity(claims, user)


class MembershipTests(unittest.IsolatedAsyncioTestCase):
    def service(self, *, email="tester@example.org", groups=None):
        service = TestFlight()
        service.app_id = AsyncMock(return_value="comat")
        service.groups = AsyncMock(return_value=groups if groups is not None else [{"id": "internal"}])
        async def request(method, path, **kwargs):
            self.assertEqual(method, "GET")
            self.assertIn(path, ["betaGroups/internal/betaTesters", "betaGroups/other/betaTesters"])
            return {"data": [{"id": "tester", "attributes": {"email": email}}]}
        service.request = AsyncMock(side_effect=request)
        return service

    async def test_internal_member_is_eligible_without_global_directory_join(self):
        service = self.service()
        self.assertTrue(await service.is_internal_tester("tester@example.org"))
        service.groups.assert_awaited_once_with("comat")
        service.request.assert_awaited_once_with("GET", "betaGroups/internal/betaTesters", params={"limit": "200"})

    async def test_account_holder_email_in_internal_group_is_eligible(self):
        self.assertTrue(await self.service(email=" Reno.Klose@posteo.eu ").is_internal_tester("reno.klose@posteo.eu"))

    async def test_unlisted_email_is_denied(self):
        self.assertFalse(await self.service(email="someone-else@example.org").is_internal_tester("tester@example.org"))

    async def test_no_internal_group_is_denied_without_querying_external_testers(self):
        service = self.service(groups=[])
        self.assertFalse(await service.is_internal_tester("tester@example.org"))
        service.request.assert_not_awaited()

    async def test_missing_email_is_denied(self):
        self.assertFalse(await self.service(email=None).is_internal_tester("tester@example.org"))

    async def test_later_internal_group_is_checked(self):
        service = self.service(groups=[{"id": "other"}, {"id": "internal"}])
        service.request.side_effect = [{"data": []}, {"data": [{"attributes": {"email": "tester@example.org"}}]}]
        self.assertTrue(await service.is_internal_tester("tester@example.org"))

    async def test_truncated_response_is_not_a_non_member_verdict(self):
        service = self.service()
        service.request.side_effect = None
        service.request.return_value = {"data": [], "links": {"next": "next-page"}}
        with self.assertRaises(AppleError):
            await service.is_internal_tester("tester@example.org")

    async def test_apple_error_is_not_an_approval(self):
        service = self.service()
        service.request.side_effect = AppleError("unavailable")
        with self.assertRaises(AppleError):
            await service.is_internal_tester("tester@example.org")


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, *, eligible=True, verification=None, grant_error=None):
        service = SimpleNamespace(is_internal_tester=AsyncMock(return_value=eligible))
        store = Mock()
        store.verify.return_value = ("device", "account", "apple-user", "tester@example.org")
        store.verify.side_effect = verification
        store.grant.return_value = 9999999999999
        store.grant.side_effect = grant_error
        request = SimpleNamespace(remote="127.0.0.1", headers={"Authorization": "Bearer " + "d" * 100}, json=AsyncMock(return_value={"appleToken": "a" * 100}))
        access = AppleAccess(SimpleNamespace(limit=Mock()), service, store)
        response = await access.approve(request)
        return response, store

    async def test_success_writes_server_grant(self):
        response, store = await self.call()
        self.assertEqual(response.status, 200)
        store.grant.assert_called_once_with("device", "account", "apple-user", "tester@example.org")

    async def test_non_member_revokes_lease_without_grant(self):
        response, store = await self.call(eligible=False)
        self.assertEqual(response.status, 403)
        store.grant.assert_not_called()
        store.revoke.assert_called_once_with("device")

    async def test_bad_identity_and_blocked_device_never_succeed(self):
        for options in [{"verification": AccessDenied("bad token")}, {"grant_error": AccessDenied("blocked")}]:
            response, _ = await self.call(**options)
            self.assertEqual(response.status, 403)

    async def test_database_failure_is_not_an_approval(self):
        response, _ = await self.call(grant_error=RuntimeError("private service detail"))
        self.assertEqual(response.status, 503)
        self.assertNotIn("private service detail", response.text)


if __name__ == "__main__":
    unittest.main()
