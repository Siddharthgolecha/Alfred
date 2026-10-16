# Security policy

Alfred controls services and runs tasks on your device, so security reports matter. This page
tells you how to report a problem. The design is described in the
[security model](docs/architecture/security-model.md).

## Supported versions

Alfred has no published release. Only the current `main` branch receives fixes.

## Report a vulnerability

Do **not** open a public issue for a security problem.

Use GitHub's private vulnerability reporting for
[Siddharthgolecha/Alfred](https://github.com/Siddharthgolecha/Alfred/security/advisories/new),
if it is enabled for the repository. Otherwise contact the maintainer through the email on their
GitHub profile.

Include:

- what you found and which component it affects,
- steps or a test that reproduce it,
- the commit you tested,
- the impact you expect.

Do not include real audio, transcripts, Bluetooth addresses or secrets.

## What counts

In scope are failures of the boundaries Alfred promises:

- speech, Needle output, Qwen or Cordis output, or tool output gaining authority that it should
  not have,
- a way to approve an action without the local CLI,
- execution of a tool, service or command outside the allowlists,
- replay of an interrupted action,
- a network listener or outbound connection that is not loopback,
- escape from the Cordis overlay (a shell, filesystem or network plugin becoming active).

Out of scope: an attacker who can already run code as your user, physical access, and the
[known limits](docs/architecture/security-model.md#known-limits).

## Handling

Reports are triaged on a best-effort basis. There is no response-time guarantee. Fixes go
through the normal review process in [CONTRIBUTING.md](CONTRIBUTING.md).
