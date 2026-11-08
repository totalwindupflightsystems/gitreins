"""Adapter-based GitHub App webhook service.

The transport and GitHub API live at this boundary. Review policy stays in the
existing GitReins review core (``engine.review_profiles.SecondPassReviewer``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class WebhookResponse:
    status: int
    body: str


@dataclass(frozen=True)
class InstallationToken:
    value: str
    expires_at: float


@dataclass(frozen=True)
class ReviewSummary:
    conclusion: str
    summary: str


class GitHubApi(Protocol):
    """The GitHub wire contract; tests inject this boundary without egress."""

    def create_installation_token(
        self, app_jwt: str, installation_id: int
    ) -> InstallationToken: ...

    def get_pull_request_diff(self, repo: str, number: int, token: str) -> str: ...

    def post_issue_comment(self, repo: str, number: int, body: str, token: str) -> None: ...

    def post_check_run(
        self, repo: str, head_sha: str, conclusion: str, summary: str, token: str
    ) -> None: ...


class ReviewCore(Protocol):
    """A narrow adapter to the already-existing GitReins review core."""

    def review(self, diff: str, *, provider: str | None, model: str | None) -> ReviewSummary: ...


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _parse_github_time(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """Validate GitHub's SHA-256 webhook MAC over the original request bytes."""
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class GitHubHttpClient:
    """Minimal GitHub REST client; all requests remain behind this replaceable adapter."""

    def __init__(
        self,
        api_url: str = "https://api.github.com",
        timeout: float = 15.0,
        request: (
            Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]] | None
        ) = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self._request = request or self._url_request

    def _url_request(
        self, method: str, path: str, headers: dict[str, str], body: bytes | None, timeout: float
    ) -> tuple[int, bytes]:
        request = Request(self.api_url + path, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured GitHub API endpoint
                return response.status, response.read()
        except HTTPError as exc:
            return exc.code, exc.read()
        except URLError as exc:
            raise RuntimeError(f"GitHub API request failed: {exc.reason}") from exc

    def _json(
        self, method: str, path: str, headers: dict[str, str], payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/vnd.github+json", **headers}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        status, response = self._request(method, path, headers, body, self.timeout)
        if not 200 <= status < 300:
            raise RuntimeError(f"GitHub API {method} {path} returned {status}: {response[:300]!r}")
        return json.loads(response.decode("utf-8")) if response else {}

    def create_installation_token(self, app_jwt: str, installation_id: int) -> InstallationToken:
        data = self._json(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            {"Authorization": f"Bearer {app_jwt}"},
        )
        token = str(data.get("token", ""))
        expires_at = str(data.get("expires_at", ""))
        if not token or not expires_at:
            raise RuntimeError("GitHub installation token response omitted token or expires_at")
        return InstallationToken(token, _parse_github_time(expires_at))

    def get_pull_request_diff(self, repo: str, number: int, token: str) -> str:
        status, body = self._request(
            "GET",
            f"/repos/{repo}/pulls/{number}",
            {"Accept": "application/vnd.github.v3.diff", "Authorization": f"Bearer {token}"},
            None,
            self.timeout,
        )
        if not 200 <= status < 300:
            raise RuntimeError(f"GitHub diff request returned {status}: {body[:300]!r}")
        return body.decode("utf-8")

    def post_issue_comment(self, repo: str, number: int, body: str, token: str) -> None:
        self._json(
            "POST",
            f"/repos/{repo}/issues/{number}/comments",
            {"Authorization": f"Bearer {token}"},
            {"body": body},
        )

    def post_check_run(
        self, repo: str, head_sha: str, conclusion: str, summary: str, token: str
    ) -> None:
        self._json(
            "POST",
            f"/repos/{repo}/check-runs",
            {"Authorization": f"Bearer {token}"},
            {
                "name": "GitReins review",
                "head_sha": head_sha,
                "status": "completed",
                "conclusion": conclusion,
                "output": {"title": "GitReins review", "summary": summary},
            },
        )


def openssl_rs256_sign(private_key_path: str, signing_input: bytes) -> bytes:
    """Sign a JWT payload with OpenSSL so the runtime needs no extra crypto package."""
    try:
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", private_key_path],
            input=signing_input,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("could not sign GitHub App JWT with OpenSSL") from exc
    return result.stdout


class InstallationTokenIssuer:
    """Mints short-lived App JWTs and caches installation tokens before expiry."""

    def __init__(
        self,
        app_id: str,
        private_key_path: str,
        client: GitHubApi,
        *,
        now: Callable[[], float] = time.time,
        signer: Callable[[str, bytes], bytes] = openssl_rs256_sign,
    ) -> None:
        self.app_id = app_id
        self.private_key_path = private_key_path
        self.client = client
        self.now = now
        self.signer = signer
        self._tokens: dict[int, InstallationToken] = {}

    def _app_jwt(self) -> str:
        issued_at = int(self.now())
        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
        claim_data = {"iat": issued_at - 60, "exp": issued_at + 540, "iss": self.app_id}
        claims = _b64url(json.dumps(claim_data, separators=(",", ":")).encode())
        signing_input = f"{header}.{claims}".encode("ascii")
        signature = _b64url(self.signer(self.private_key_path, signing_input))
        return f"{header}.{claims}.{signature}"

    def token_for(self, installation_id: int) -> str:
        cached = self._tokens.get(installation_id)
        if cached and cached.expires_at - self.now() > 60:
            return cached.value
        token = self.client.create_installation_token(self._app_jwt(), installation_id)
        self._tokens[installation_id] = token
        return token.value


class GitReinsReviewAdapter:
    """Calls GitReins' existing diff-review core; this integration owns no review logic."""

    def __init__(
        self,
        *,
        llm_factory: Callable[..., Any] | None = None,
        reviewer_factory: Callable[[Any, str], Any] | None = None,
    ) -> None:
        self.llm_factory = llm_factory
        self.reviewer_factory = reviewer_factory

    def review(self, diff: str, *, provider: str | None, model: str | None) -> ReviewSummary:
        llm_factory: Callable[..., Any]
        reviewer_factory: Callable[[Any, str], Any]
        if self.llm_factory is None:
            from engine.llm import LLMClient

            llm_factory = LLMClient
        else:
            llm_factory = self.llm_factory
        if self.reviewer_factory is None:
            from engine.review_profiles import SecondPassReviewer

            def default_reviewer_factory(llm: Any, _: str) -> Any:
                return SecondPassReviewer(llm, workdir=os.getcwd())

            reviewer_factory = default_reviewer_factory
        else:
            reviewer_factory = self.reviewer_factory
        result = reviewer_factory(llm_factory(provider=provider, model=model), diff).review(diff)
        if getattr(result, "error", None):
            raise RuntimeError(str(result.error))
        findings = getattr(result, "findings", [])
        summary = str(getattr(result, "summary", "") or "No findings.")
        if findings:
            summary = f"{summary}\n\nFindings: {len(findings)}"
        return ReviewSummary("neutral" if findings else "success", summary)


class WebhookService:
    """Routes authenticated pull-request events into GitHub and GitReins adapters."""

    def __init__(
        self,
        webhook_secret: str,
        github: GitHubApi,
        token_issuer: InstallationTokenIssuer,
        review_core: ReviewCore,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        self.webhook_secret = webhook_secret
        self.github = github
        self.token_issuer = token_issuer
        self.review_core = review_core
        self.provider = provider
        self.model = model
        self._deliveries: set[str] = set()

    def handle(self, headers: dict[str, str], body: bytes) -> WebhookResponse:
        signature = headers.get("X-Hub-Signature-256") or headers.get("x-hub-signature-256")
        if not verify_signature(self.webhook_secret, body, signature):
            return WebhookResponse(401, "invalid webhook signature")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return WebhookResponse(400, "invalid JSON payload")
        event = headers.get("X-GitHub-Event") or headers.get("x-github-event")
        action = str(payload.get("action", ""))
        if event != "pull_request" or action not in {"opened", "synchronize"}:
            return WebhookResponse(202, "event ignored")
        delivery = headers.get("X-GitHub-Delivery") or headers.get("x-github-delivery")
        repository = payload.get("repository") or {}
        pull_request = payload.get("pull_request") or {}
        repo = str(repository.get("full_name", ""))
        number = payload.get("number")
        installation = payload.get("installation") or {}
        installation_id = installation.get("id")
        head = pull_request.get("head") or {}
        head_sha = str(head.get("sha", ""))
        valid_payload = all((repo, isinstance(number, int), head_sha))
        if not valid_payload or not isinstance(installation_id, int):
            return WebhookResponse(
                400,
                "pull request payload missing repository, number, installation, or head SHA",
            )
        identity = delivery or f"{event}:{action}:{repo}:{number}:{head_sha}"
        if identity in self._deliveries:
            return WebhookResponse(202, "duplicate delivery")
        self._deliveries.add(identity)
        try:
            token = self.token_issuer.token_for(installation_id)
            diff = self.github.get_pull_request_diff(repo, number, token)
            review = self.review_core.review(diff, provider=self.provider, model=self.model)
            summary = f"## GitReins review\n\n{review.summary}"
            self.github.post_issue_comment(repo, number, summary, token)
            self.github.post_check_run(repo, head_sha, review.conclusion, review.summary, token)
            return WebhookResponse(200, "review posted")
        except Exception as exc:
            failure = f"## GitReins review failed\n\nProvider or review error: {exc}"
            try:
                token = self.token_issuer.token_for(installation_id)
                self.github.post_issue_comment(repo, number, failure, token)
                self.github.post_check_run(repo, head_sha, "failure", failure, token)
            except Exception:
                pass
            return WebhookResponse(502, "review failed; failure status requested")


class GitHubAppWSGI:
    """A tiny runnable WSGI wrapper; deploy behind any normal WSGI server."""

    def __init__(self, service: WebhookService) -> None:
        self.service = service

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        body = environ["wsgi.input"].read(length)
        headers = {
            key[5:].replace("_", "-"): value
            for key, value in environ.items()
            if key.startswith("HTTP_")
        }
        response = self.service.handle(headers, body)
        status_text = "OK" if response.status < 400 else "ERROR"
        start_response(
            f"{response.status} {status_text}",
            [("Content-Type", "text/plain")],
        )
        return [response.body.encode("utf-8")]


def service_from_env() -> WebhookService:
    """Build the runnable service from explicit GitHub App environment settings."""
    secret = os.environ["GITHUB_WEBHOOK_SECRET"]
    app_id = os.environ["GITHUB_APP_ID"]
    key_path = os.environ["GITHUB_APP_PRIVATE_KEY_PATH"]
    github = GitHubHttpClient(api_url=os.getenv("GITHUB_API_URL", "https://api.github.com"))
    issuer = InstallationTokenIssuer(app_id, key_path, github)
    return WebhookService(
        secret,
        github,
        issuer,
        GitReinsReviewAdapter(),
        provider=os.getenv("GITREINS_LLM_PROVIDER"),
        model=os.getenv("GITREINS_LLM_MODEL"),
    )
