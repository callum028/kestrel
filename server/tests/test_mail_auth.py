import httpx
import pytest

from kestrel.mail_auth import run_device_code_flow

TENANT = "test-tenant"
CLIENT = "test-client"


def device_code_response():
    return httpx.Response(
        200,
        json={
            "device_code": "dc-1",
            "user_code": "ABCD1234",
            "verification_uri": "https://microsoft.com/devicelogin",
            "message": "Go to https://microsoft.com/devicelogin and enter ABCD1234",
            "interval": 0,  # no real waiting in tests
            "expires_in": 900,
        },
    )


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_requests_a_device_code_with_the_right_scope():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/devicecode"):
            seen["body"] = request.content.decode()
            return device_code_response()
        return httpx.Response(
            200, json={"access_token": "a", "refresh_token": "r", "expires_in": 1}
        )

    run_device_code_flow(TENANT, CLIENT, client=client_for(handler))
    assert f"client_id={CLIENT}" in seen["body"]
    assert "scope=Mail.Read" in seen["body"]


def test_polls_until_authorization_pending_resolves():
    poll_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/devicecode"):
            return device_code_response()
        poll_count["n"] += 1
        if poll_count["n"] < 3:
            return httpx.Response(400, json={"error": "authorization_pending"})
        return httpx.Response(
            200,
            json={"access_token": "a", "refresh_token": "the-refresh-token", "expires_in": 3600},
        )

    token = run_device_code_flow(TENANT, CLIENT, client=client_for(handler))
    assert token == "the-refresh-token"
    assert poll_count["n"] == 3


def test_declined_consent_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/devicecode"):
            return device_code_response()
        return httpx.Response(
            400, json={"error": "authorization_declined", "error_description": "user declined"}
        )

    with pytest.raises(RuntimeError, match="authorization_declined"):
        run_device_code_flow(TENANT, CLIENT, client=client_for(handler))


def test_slow_down_backs_off_and_still_completes():
    poll_count = {"n": 0}
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/devicecode"):
            return device_code_response()
        poll_count["n"] += 1
        if poll_count["n"] == 1:
            return httpx.Response(400, json={"error": "slow_down"})
        return httpx.Response(
            200, json={"access_token": "a", "refresh_token": "r", "expires_in": 1}
        )

    # `sleep` is faked so the test covers the backoff logic (interval grows by
    # 5 after a slow_down) without actually waiting out a real, seconds-scale
    # polling interval - this was previously the slowest test in the suite.
    token = run_device_code_flow(TENANT, CLIENT, client=client_for(handler), sleep=slept.append)
    assert token == "r"
    assert slept == [0, 5]  # the device code's own interval, then +5 after slow_down
