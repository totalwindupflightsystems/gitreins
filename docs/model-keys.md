# Model keys — bring your own key

GitReins does not supply a model credential. For the Tier 2 LLM judge, provide
`GITREINS_LLM_API_KEY` to the process that runs the judge. Set
`GITREINS_LLM_BASE_URL` and `GITREINS_LLM_MODEL` when using a provider or model
other than the defaults; make sure the endpoint, model and key belong together.
The defaults are the DeepSeek endpoint and `deepseek-v4-flash`.

The separate Jev resolution gate uses `GITREINS_OPENROUTER_KEY`; it is not an
alias for the judge's key. See [Jev resolution gate](jev-resolution-gate.md).
The repository's `.gitreins/config.yaml` can set `defaults.model` as an
alternative model configuration, but it is **not** an alternative API-key
source for the LLM client. See the [CLI reference](cli-reference.md#llm-runtime-environment)
for environment precedence and optional provider-key fallback.

Never commit credentials or environment files; use CI masked/secret variables
and restrict secret access to trusted jobs and code.

## App server — gitreins serve

`gitreins serve` is a read-only judgment-history viewer; it does not call a
model and does not need a model key. Supply `GITREINS_LLM_API_KEY` in the
shell or service environment of the separate process that runs the judge.
The distinct [GitHub App webhook service](../apps/github_app/README.md) runs
reviews: supply the judge key in that service's environment, alongside its
separately configured GitHub App credentials. Do not confuse those credentials
with the model provider key.

## GitHub Action

Create a repository or environment secret in GitHub and map it to
`GITREINS_LLM_API_KEY` in the calling workflow's step environment. The shipped
Action's `api-key-env` input names the environment variable to read; it defaults
to `GITREINS_LLM_API_KEY`, and the Action copies that variable into the judge's
`GITREINS_LLM_API_KEY`. It is a variable name, not the secret value. The key is
required unless `tier1-only: 'true'` skips Tier 2. See the complete
[GitHub Action example](integrations/github-action.md); do not expose secrets
to untrusted pull-request code.

## GitLab CI

This checkout does not ship a GitLab merge-request webhook listener or a GitLab
CI template. To run the local CLI in your own GitLab CI job, create a masked
GitLab CI/CD variable named `GITREINS_LLM_API_KEY` and make it available to a
trusted job running the judge. Configure the endpoint and model in the job's
environment as needed. If the job separately invokes the Jev resolution gate,
provide its OpenRouter key as the masked variable `GITREINS_OPENROUTER_KEY`.
This is a consumer-configured CI job, not a shipped GitLab integration.

## Local CLI

Export `GITREINS_LLM_API_KEY` in the shell before running `gitreins judge` or
`gitreins task complete`. Set the optional endpoint and model variables in the
same environment, or set the model in `.gitreins/config.yaml` under
`defaults.model`. For Jev resolution, supply `GITREINS_OPENROUTER_KEY`
separately. A Tier-1-only run with `--skip-tier2` does not perform the LLM
judge; it is not a substitute for a Tier 2 verdict.
