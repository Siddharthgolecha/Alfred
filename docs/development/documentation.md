# Documentation

The docs are Markdown in `docs/`, built with [Sphinx](https://www.sphinx-doc.org/), the MyST
parser, the Furo theme and `sphinxcontrib-mermaid`. Root files (`README.md`, `CONTRIBUTING.md`,
`SECURITY.md`, `CHANGELOG.md`) sit outside the Sphinx tree and are linked, not included.

## Build

```bash
pip install -e '.[docs]'
make -C docs html          # output: docs/_build/html
```

Warnings are errors in CI (`sphinx-build -W`), so fix every warning locally. If you use a virtual
environment that `make` cannot see, pass it explicitly:

```bash
make -C docs html PYTHON=../.venv/bin/python SPHINXBUILD="../.venv/bin/python -m sphinx"
```

## Check for drift

```bash
python scripts/check_docs.py
```

The script needs only the standard library. It fails when:

- a CLI flag in `alfred/cli.py` is missing from [`reference/cli.md`](../reference/cli.md),
- a tool in `alfred/runtime.py` is missing from [`reference/tools.md`](../reference/tools.md),
- a key of `DEFAULT` in `alfred/config.py`, or an `ALFRED_*` environment variable, is missing from
  [`reference/configuration.md`](../reference/configuration.md),
- a relative Markdown link in the repository docs points at a file that does not exist.

It checks that a name is *mentioned* in backticks. It does not check that the description is
right, so read the source when you edit a reference page.

## Layout

| Directory | Question it answers | Style |
|---|---|---|
| `getting-started/` | How do I install and run it? | Sequential steps. |
| `guides/` | How do I do X? | Task-oriented, one goal per page. |
| `architecture/` | How does it work, and why? | Explanation with diagrams. `decisions/` holds ADRs. |
| `reference/` | What exactly is the option, flag or tool? | Tables. No narrative. |
| `development/` | How do we build and release it? | For contributors. |

Do not organise pages by project phase or PR number. A reader looks things up by topic.

## Writing rules

1. **Verify against the source.** Do not copy a claim from an older page. Open the code.
2. **Keep honesty markers.** Alfred is not hardware-validated. Do not write that something works
   on a Jetson unless it was tested there.
3. **Say what happens on failure.** Quote the real message and the exit code.
4. **Use Mermaid** for diagrams, as a `{mermaid}` fenced directive:

   ````markdown
   ```{mermaid}
   flowchart LR
       a --> b
   ```
   ````

5. **Use admonitions** with the colon fence: `:::{note}`, `:::{warning}`, `:::{important}`.
6. **Link, do not repeat.** Put the exact values in the reference and link to them.
7. **Relative links** to repository files, such as `../../alfred/config.py`, so they work on
   GitHub and in the build. Root files outside the Sphinx tree (`CHANGELOG.md`, `AGENTS.md`,
   `SECURITY.md`) are the exception: link them with an absolute GitHub URL, because Sphinx
   cannot resolve them as pages.

## Architecture decision records

Add an ADR to `architecture/decisions/` for a decision that is hard to reverse or that a new
contributor would otherwise question. Number it in sequence, state the context, the decision and
the consequences, and add it to that folder's `index.md`.

## Changelog

Record every user-visible change under `[Unreleased]` in [`CHANGELOG.md`](https://github.com/Siddharthgolecha/Alfred/blob/main/CHANGELOG.md),
grouped as Added, Changed, Deprecated, Removed, Fixed or Security. See
[Release process](release-process.md).
