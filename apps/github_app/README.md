# GitHub App webhook receiver

This first integration receives GitHub App `pull_request` webhooks, authenticates
GitHub's HMAC signature before parsing or routing the request, and sends the PR
diff to the existing GitReins `SecondPassReviewer` review core. It does not own
or fork a second review engine.

## GitHub App manifest

```yaml
name: GitReins review
url: https://example.invalid/github/webhook
public: false
default_permissions:
  pull_requests: write
  contents: read
default_events:
  - pull_request
```

The receiver handles only `pull_request.opened` and `pull_request.synchronize`.
It fetches the PR diff, posts a review summary comment plus a completed check,
and posts an explicit failure comment/check when the configured provider raises
or times out. Delivery ids (or the event/PR/head fallback identity) are
suppressed after the first authenticated delivery.

## Run

```sh
export GITHUB_WEBHOOK_SECRET=...
export GITHUB_APP_ID=...
export GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/github-app.pem
export GITREINS_LLM_PROVIDER=openai
export GITREINS_LLM_MODEL=your-model
python -m apps.github_app
```

The service uses the GitHub REST API by default. `GITHUB_API_URL` may override
it for GitHub Enterprise. The app JWT is RS256-signed through `openssl`; the
installation access token exchange is `POST /app/installations/{id}/access_tokens`.

ch:trace row=GR-187 spec=docs/integrations/architecture.md#github-app-webhook wave=none:serial-worker test=tests/test_github_app.py::TestWebhookService::test_pull_request_pipeline doc=apps/github_app/README.md evidence=tests/test_github_app.py witness=none:hermetic-fixtures-only verdict=none:foreman-owned commit=none:not-published memory=none:duckbrain-read-failed
