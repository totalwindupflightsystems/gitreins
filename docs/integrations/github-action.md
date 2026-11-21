# GitHub Action

The root `action.yml` is a client-side composite Action that runs in a consumer
repository's workflow. Unlike the inbound GitHub App webhook, it does not post
reviews or checks itself; its step status gates the workflow. Check out the
repository first and configure GitReins and its guard tools in the checkout.
Use a trusted, immutable Action commit SHA in production; `@v0` below is an
illustrative release reference, not a claim that a v0 tag already exists.

```yaml
steps:
  - uses: actions/checkout@v4
  # action.yml from the GitReins repository
  - uses: totalwindupflightsystems/gitreins@v0
    id: gitreins
    with:
      task-id: example-task
    env:
      GITREINS_LLM_API_KEY: ${{ secrets.GITREINS_API_KEY }}
  - if: ${{ always() && steps.gitreins.outputs.summary == 'fail' }}
    run: echo 'GitReins did not pass'
```

Keys must come from GitHub secrets, never from the manifest. `api-key-env` names
an environment variable, not a secret or key value; GitHub Actions cannot
look up secrets dynamically inside a composite Action. Map the secret to that
variable in the calling workflow. Provider model/base URL settings follow the
normal GitReins configuration. Use secrets only with trusted workflow code: do
not expose them to untrusted pull request code.

Inputs (all optional individually; supply either a task ID or both inline fields):

| Input | Default | Meaning |
| --- | --- | --- |
| `path` | `.` | Checkout path relative to `GITHUB_WORKSPACE`. |
| `scope` | `working-tree` | Judge change scope: `staged` or `working-tree`. |
| `task-id` | empty | Existing task in the checkout's GitReins task store. |
| `title` | empty | Ephemeral task title, used instead of `task-id`. |
| `criterion` | empty | One ephemeral criterion, required with `title`. |
| `tier1-only` | `false` | `true` skips the LLM judge; this is not a Tier 2 review. |
| `api-key-env` | `GITREINS_LLM_API_KEY` | Name of the environment variable holding the key. |

Outputs are `verdict-path` (runner-local JSON evidence, not an uploaded artifact)
and `summary` (`pass` or `fail`). Upload the file explicitly if it must survive
the job. A non-pass, command error, invalid input or missing key fails the step.
The standalone guard runs with `--scope tree --full`; the judge uses the chosen
change scope and runs Tier 1 again. On a clean checkout, `working-tree` and
`staged` may contain no changes: prepare the desired diff in the checkout before
calling the Action. The Action does not fetch a PR base or stage changes for you.
Existing-task judge mode persists its normal verdict history; ephemeral mode does
not. The pinned GitReins installation is independent of the consumer checkout.
