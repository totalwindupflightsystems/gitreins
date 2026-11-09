import base64
import hashlib
import hmac
import io
import json

import pytest

from apps.github_app.service import (
    GitHubAppWSGI,
    GitHubHttpClient,
    GitReinsReviewAdapter,
    InstallationToken,
    InstallationTokenIssuer,
    ReviewSummary,
    WebhookService,
)


class Clock:
    def __init__(self, value: float = 1_700_000_000) -> None:
        self.value = value

    def now(self) -> float:
        return self.value


class FakeGitHub:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.token_requests: list[tuple[str, int]] = []
        self.diff_requests: list[tuple[str, int, str]] = []
        self.comments: list[tuple[str, int, str, str]] = []
        self.checks: list[tuple[str, str, str, str, str]] = []
        self.diff = "diff --git a/app.py b/app.py\n+safe change\n"

    def create_installation_token(self, app_jwt: str, installation_id: int) -> InstallationToken:
        self.token_requests.append((app_jwt, installation_id))
        return InstallationToken(f"installation-{len(self.token_requests)}", self.clock.now() + 300)

    def get_pull_request_diff(self, repo: str, number: int, token: str) -> str:
        self.diff_requests.append((repo, number, token))
        return self.diff

    def post_issue_comment(self, repo: str, number: int, body: str, token: str) -> None:
        self.comments.append((repo, number, body, token))

    def post_check_run(
        self, repo: str, head_sha: str, conclusion: str, summary: str, token: str
    ) -> None:
        self.checks.append((repo, head_sha, conclusion, summary, token))


class FakeReviewCore:
    def __init__(self, result: ReviewSummary | None = None, error: Exception | None = None) -> None:
        self.result = result or ReviewSummary("success", "No issues found.")
        self.error = error
        self.calls: list[tuple[str, str | None, str | None]] = []

    def review(self, diff: str, *, provider: str | None, model: str | None) -> ReviewSummary:
        self.calls.append((diff, provider, model))
        if self.error:
            raise self.error
        return self.result


def payload(action: str = "opened") -> dict[str, object]:
    return {
        "action": action,
        "number": 7,
        "repository": {"full_name": "octo/example"},
        "installation": {"id": 42},
        "pull_request": {"head": {"sha": "abc123"}},
    }


def signed_headers(
    body: bytes, *, event: str = "pull_request", delivery: str = "delivery-1"
) -> dict[str, str]:
    signature = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    return {
        "X-Hub-Signature-256": f"sha256={signature}",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
    }


def make_service(
    core: FakeReviewCore | None = None,
) -> tuple[WebhookService, FakeGitHub, Clock, FakeReviewCore]:
    clock = Clock()
    github = FakeGitHub(clock)
    issuer = InstallationTokenIssuer(
        "123",
        "/unused/private.pem",
        github,
        now=clock.now,
        signer=lambda _, signing_input: b"signature:" + signing_input[:4],
    )
    review_core = core or FakeReviewCore()
    return (
        WebhookService(
            "secret", github, issuer, review_core, provider="openai", model="review-model"
        ),
        github,
        clock,
        review_core,
    )


class TestInstallationTokens:
    def test_jwt_exchange_and_expiry_cache_use_hermetic_fixture(self) -> None:
        clock = Clock()
        github = FakeGitHub(clock)
        signed_inputs: list[tuple[str, bytes]] = []
        issuer = InstallationTokenIssuer(
            "app-123",
            "/fixture/app.pem",
            github,
            now=clock.now,
            signer=lambda path, data: signed_inputs.append((path, data)) or b"fixture-signature",
        )

        assert issuer.token_for(9) == "installation-1"
        assert issuer.token_for(9) == "installation-1"
        assert len(github.token_requests) == 1
        assert signed_inputs[0][0] == "/fixture/app.pem"
        jwt = github.token_requests[0][0]
        claims = json.loads(base64.urlsafe_b64decode(jwt.split(".")[1] + "=="))
        assert claims == {
            "iat": int(clock.value) - 60,
            "exp": int(clock.value) + 540,
            "iss": "app-123",
        }

        clock.value += 241
        assert issuer.token_for(9) == "installation-2"
        assert len(github.token_requests) == 2

    def test_http_client_posts_installation_token_exchange(self) -> None:
        calls: list[tuple[str, str, dict[str, str], bytes | None, float]] = []

        def request(
            method: str, path: str, headers: dict[str, str], body: bytes | None, timeout: float
        ):
            calls.append((method, path, headers, body, timeout))
            return 201, b'{"token":"issued","expires_at":"2025-01-01T00:00:00Z"}'

        token = GitHubHttpClient(request=request).create_installation_token("app-jwt", 42)

        assert token.value == "issued"
        assert calls[0][0:2] == ("POST", "/app/installations/42/access_tokens")
        assert calls[0][2]["Authorization"] == "Bearer app-jwt"


class TestWebhookService:
    def test_signature_rejection(self) -> None:
        service, github, _, core = make_service()
        body = json.dumps(payload()).encode()
        response = service.handle({"X-Hub-Signature-256": "sha256=wrong"}, body)

        assert response.status == 401
        assert core.calls == []
        assert github.diff_requests == []

    @pytest.mark.parametrize("action", ["opened", "synchronize"])
    def test_pull_request_pipeline(self, action: str) -> None:
        service, github, _, core = make_service()
        body = json.dumps(payload(action)).encode()

        response = service.handle(signed_headers(body, delivery=f"delivery-{action}"), body)

        assert response.status == 200
        assert core.calls == [(github.diff, "openai", "review-model")]
        assert github.comments[0][0:2] == ("octo/example", 7)
        assert "GitReins review" in github.comments[0][2]
        assert github.checks[0][1:3] == ("abc123", "success")

    def test_wsgi_header_mapping_reaches_service(self) -> None:
        service, github, _, core = make_service()
        body = json.dumps(payload()).encode()
        status: list[str] = []

        def start_response(status_line: str, headers: object) -> None:
            status.append(status_line)

        environ = {
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
            **{
                f"HTTP_{key.upper().replace('-', '_')}": value
                for key, value in signed_headers(body).items()
            },
        }

        response = b"".join(GitHubAppWSGI(service)(environ, start_response))

        assert status == ["200 OK"]
        assert response == b"review posted"
        assert core.calls == [(github.diff, "openai", "review-model")]

    def test_failed_review_delivery_can_retry(self) -> None:
        failing = FakeReviewCore(error=TimeoutError("provider timed out"))
        service, github, _, core = make_service(failing)
        body = json.dumps(payload()).encode()
        headers = signed_headers(body, delivery="retry-me")

        assert service.handle(headers, body).status == 502
        core.error = None
        assert service.handle(headers, body).status == 200
        assert len(github.comments) == 2

    def test_non_pull_request_event_is_ignored(self) -> None:
        service, github, _, core = make_service()
        body = json.dumps(payload()).encode()

        response = service.handle(signed_headers(body, event="push"), body)

        assert response.status == 202
        assert core.calls == []
        assert github.comments == []

    def test_duplicate_delivery_is_suppressed(self) -> None:
        service, github, _, core = make_service()
        body = json.dumps(payload()).encode()
        headers = signed_headers(body, delivery="same-delivery")

        assert service.handle(headers, body).status == 200
        assert service.handle(headers, body).status == 202
        assert len(core.calls) == 1
        assert len(github.comments) == 1

    def test_provider_failure_posts_visible_failure_comment_and_check(self) -> None:
        service, github, _, _ = make_service(
            FakeReviewCore(error=TimeoutError("provider timed out"))
        )
        body = json.dumps(payload()).encode()

        response = service.handle(signed_headers(body), body)

        assert response.status == 502
        assert "review failed" in github.comments[0][2].lower()
        assert "provider timed out" in github.comments[0][2]
        assert github.checks[0][2] == "failure"


class TestReviewAdapter:
    def test_configured_provider_and_model_reach_existing_review_core(self) -> None:
        llm_calls: list[dict[str, str | None]] = []

        class Reviewer:
            def review(self, diff: str):
                assert diff == "diff fixture"
                return type("Result", (), {"error": None, "findings": [], "summary": "Reviewed"})()

        adapter = GitReinsReviewAdapter(
            llm_factory=lambda **kwargs: llm_calls.append(kwargs) or object(),
            reviewer_factory=lambda _, __: Reviewer(),
        )

        result = adapter.review("diff fixture", provider="anthropic", model="model-fixture")

        assert result == ReviewSummary("success", "Reviewed")
        assert llm_calls == [{"provider": "anthropic", "model": "model-fixture"}]
