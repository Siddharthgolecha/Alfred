#!/usr/bin/env python3
"""Fail if the reference docs drift from the source code.

Checks, using only the standard library:

* every CLI flag in ``alfred/cli.py`` appears in ``docs/reference/cli.md``;
* every tool in ``alfred/runtime.py`` appears in ``docs/reference/tools.md``;
* every key of ``DEFAULT`` in ``alfred/config.py`` and every ``ALFRED_*``
  environment variable appears in ``docs/reference/configuration.md``;
* every relative Markdown link in the repository docs resolves to a file.

Run from anywhere: ``python scripts/check_docs.py``. Exit status is 1 on drift.
"""
from __future__ import annotations

import ast
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "docs" / "reference"
LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)\)")
FENCE = re.compile(r"^(```|~~~)")


def cli_flags() -> set[str]:
    source = (ROOT / "alfred" / "cli.py").read_text(encoding="utf-8")
    flags = set()
    for call in re.findall(r"add_argument\(([^)]*)", source):
        flags.update(re.findall(r'"(--[a-z][a-z-]*)"', call))
    return flags


def tool_names() -> set[str]:
    source = (ROOT / "alfred" / "runtime.py").read_text(encoding="utf-8")
    return set(re.findall(r'schema\("([a-z_]+)"', source))


def config_keys() -> set[str]:
    """Extract ``section.key`` names without evaluating config values.

    Platform-dependent defaults can contain calls such as ``Path(...)`` and
    ``_default_state_dir()``, which ``ast.literal_eval`` cannot parse.
    Only the dictionary *keys* are relevant for documentation checks.
    """
    tree = ast.parse((ROOT / "alfred" / "config.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if not (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "DEFAULT" for t in node.targets)):
            continue
        if not isinstance(node.value, ast.Dict):
            raise ValueError("config.DEFAULT must be a dictionary literal")
        result = set()
        for section_node, body_node in zip(node.value.keys, node.value.values):
            if not (isinstance(section_node, ast.Constant)
                    and isinstance(section_node.value, str)
                    and isinstance(body_node, ast.Dict)):
                raise ValueError("config.DEFAULT sections must have static string keys")
            section = section_node.value
            for key_node in body_node.keys:
                if not (isinstance(key_node, ast.Constant)
                        and isinstance(key_node.value, str)):
                    raise ValueError("config.DEFAULT keys must be static strings")
                result.add(f"{section}.{key_node.value}")
        return result
    raise SystemExit("DEFAULT not found in alfred/config.py")


def env_vars() -> set[str]:
    source = (ROOT / "alfred" / "config.py").read_text(encoding="utf-8")
    return set(re.findall(r'"(ALFRED_[A-Z_]+)"', source))


def missing(names: set[str], page: Path) -> list[str]:
    """Names that never appear as a whole word inside backticks on *page*."""
    text = page.read_text(encoding="utf-8")
    spans = " ".join(re.findall(r"`([^`\n]+)`", text))
    return sorted(n for n in names
                  if not re.search(r"(?<![\w-])" + re.escape(n) + r"(?![\w-])", spans))


def check_config(page: Path) -> list[str]:
    """Each key must appear in backticks under its own ``## `section` `` heading."""
    text = page.read_text(encoding="utf-8")
    problems = []
    for dotted in sorted(config_keys()):
        section, key = dotted.split(".")
        body = re.search(rf"^## `{section}`\n(.*?)(?=^## |\Z)", text, re.M | re.S)
        if body is None:
            problems.append(f"{dotted} (no '## `{section}`' section)")
        elif f"`{key}`" not in body.group(1):
            problems.append(dotted)
    problems += [var for var in sorted(env_vars()) if f"`{var}`" not in text]
    return problems


def doc_files() -> list[Path]:
    files = [ROOT / name for name in
             ("README.md", "CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md")]
    files += sorted((ROOT / "docs").rglob("*.md"))
    return [f for f in files if f.is_file() and "_build" not in f.parts]


def broken_links() -> list[str]:
    problems = []
    for path in doc_files():
        in_fence = False
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FENCE.match(line.strip()):
                in_fence = not in_fence
            if in_fence:
                continue
            for target in LINK.findall(line):
                if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
                    continue
                file_part = target.split("#", 1)[0]
                if file_part and not (path.parent / file_part).resolve().exists():
                    problems.append(f"{path.relative_to(ROOT)}:{number}: {target}")
    return problems


def main() -> int:
    failures = []
    for label, names, page in (
            ("CLI flag", cli_flags(), REF / "cli.md"),
            ("tool", tool_names(), REF / "tools.md")):
        failures += [f"{label} not documented in {page.relative_to(ROOT)}: {n}"
                     for n in missing(names, page)]
    failures += [f"config item not documented in docs/reference/configuration.md: {n}"
                 for n in check_config(REF / "configuration.md")]
    failures += [f"broken link: {p}" for p in broken_links()]
    for line in failures:
        print(line, file=sys.stderr)
    if failures:
        print(f"{len(failures)} documentation problem(s)", file=sys.stderr)
        return 1
    print(f"Docs OK: {len(cli_flags())} flags, {len(tool_names())} tools, "
          f"{len(config_keys())} config keys, {len(env_vars())} env vars, "
          f"{len(doc_files())} files linked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
