"""Sphinx configuration for the Alfred documentation."""
project = "Alfred"
author = "Alfred contributors"
copyright = "Alfred contributors"

extensions = [
    "myst_parser",
    "sphinxcontrib.mermaid",
]

source_suffix = {".md": "markdown"}
root_doc = "index"
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

myst_enable_extensions = ["colon_fence", "deflist"]
myst_heading_anchors = 3
myst_fence_as_directive = ["mermaid"]

html_theme = "furo"
html_title = "Alfred"
