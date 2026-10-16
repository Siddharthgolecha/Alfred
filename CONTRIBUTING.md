# Contributing

Thanks for helping with Alfred. This page is the short version. Details are in the
[development docs](docs/development/testing.md).

## Before you start

- Read the [architecture overview](docs/architecture/overview.md) and the
  [security model](docs/architecture/security-model.md). Alfred's design rule is that models
  propose and Python decides. A change must not weaken that.
- Alfred uses the [Maestaris](docs/development/maestaris.md) workflow. Work is tracked as GitHub
  Issues labelled `maestaris:task`. Claim an issue with a comment before you start, work on one
  issue at a time, and do not merge your own PR. The full protocol is in
  [`AGENTS.md`](AGENTS.md).
- Report vulnerabilities privately. See [SECURITY.md](SECURITY.md).

## Set up

```bash
git clone https://github.com/Siddharthgolecha/Alfred.git
cd Alfred
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test,docs]'
```

You do not need a Jetson, a microphone or a model to run the tests.

## Make a change

1. Branch from the latest `main`: `maestaris/<issue>-<slug>`.
2. Keep the change focused on one issue. Do not touch `intrinsic_jetson` directly.
3. Add or update tests. For anything that touches approvals, shell or network access,
   persistence or replay, test the negative cases too.
4. Update the docs in the same PR (see below).
5. Run the checks:

   ```bash
   python -m compileall -q alfred tests
   python -m unittest discover -s tests -v
   python scripts/check_docs.py
   make -C docs html
   ```

6. Open a PR to `main` that says `Resolves #N`, and describe the implementation, safety impact,
   tests, and any resource or performance evidence. CI must pass on Python 3.10 and 3.12.

## Update the documentation

Docs are part of the change, not a follow-up.

| If you change… | Update… |
|---|---|
| A CLI flag | [`docs/reference/cli.md`](docs/reference/cli.md) |
| A config key or default | [`docs/reference/configuration.md`](docs/reference/configuration.md) |
| A tool | [`docs/reference/tools.md`](docs/reference/tools.md) |
| A behaviour users rely on | The matching page in `docs/guides/` |
| How the system is structured | `docs/architecture/`, and an ADR for a significant decision |
| Anything user-visible | `[Unreleased]` in [`CHANGELOG.md`](CHANGELOG.md) |

`scripts/check_docs.py` fails if a CLI flag, tool or config key is missing from the reference, or
if a relative link is broken. See [Documentation](docs/development/documentation.md).

## Style

- Follow the existing code style. Keep modules small and single-purpose.
- Prefer a simple solution. Do not add features or abstractions that the issue does not ask for.
- Never commit secrets, real Bluetooth addresses, audio or transcripts. Keep `config.json`
  untracked.
- Do not claim hardware validation you have not done. Say what was tested and how.

## Hardware claims

CI is offline. A change that affects audio, Bluetooth, CUDA, memory or latency needs evidence from
a real Orin Nano before anyone describes it as validated. See the
[hardware checklist](docs/getting-started/hardware-setup.md#hardware-acceptance-checklist).

## License

No license has been chosen yet, so contributions are accepted on that understanding.
