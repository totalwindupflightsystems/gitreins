# Integrations architecture

## GitHub App webhook

`apps.github_app` is the inbound GitHub App boundary. It authenticates the raw
webhook body with `X-Hub-Signature-256` before JSON parsing, event routing,
deduplication, token exchange, or review. The only supported event/actions are
`pull_request.opened` and `pull_request.synchronize`.

The service uses a small injectable `GitHubApi` adapter for the GitHub REST
contract. `InstallationTokenIssuer` signs a short-lived App JWT from the
configured private-key path and exchanges it for an installation token through
`POST /app/installations/{id}/access_tokens`; tokens are cached only until one
minute before expiry. The `ReviewCore` protocol connects the boundary to
`GitReinsReviewAdapter`, which invokes `engine.review_profiles.SecondPassReviewer`.
No duplicate review engine exists in the integration.

Every review attempt posts a PR comment and a completed GitHub check. A provider
or review failure posts a failure comment/check instead of reporting success.
Authenticated GitHub delivery ids, with an event/PR/head fallback, are deduplicated
in-process before any GitHub or provider call.
