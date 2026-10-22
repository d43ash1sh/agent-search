from __future__ import annotations

import re

# Common mount roots for client-side frameworks (React, Vue, Angular, Next, Nuxt, Svelte)
SPA_MOUNT_PATTERNS = [
    re.compile(
        r"""<div\s+[^>]*id=["']root["'][^>]*>\s*(?:<!--.*?-->)?\s*</div>""", re.IGNORECASE | re.DOTALL
    ),
    re.compile(r"""<div\s+[^>]*id=["']app["'][^>]*>\s*(?:<!--.*?-->)?\s*</div>""", re.IGNORECASE | re.DOTALL),
    re.compile(
        r"""<div\s+[^>]*id=["']__next["'][^>]*>\s*(?:<!--.*?-->)?\s*</div>""", re.IGNORECASE | re.DOTALL
    ),
    re.compile(
        r"""<div\s+[^>]*id=["']__nuxt["'][^>]*>\s*(?:<!--.*?-->)?\s*</div>""", re.IGNORECASE | re.DOTALL
    ),
    re.compile(r"""<app-root\b[^>]*>\s*(?:<!--.*?-->)?\s*</app-root>""", re.IGNORECASE | re.DOTALL),
]

# Client-side noscript warnings indicating that JS hydration is required
JS_REQUIRED_PATTERNS = [
    re.compile(r"you\s+need\s+to\s+enable\s+javascript", re.IGNORECASE),
    re.compile(r"enable\s+javascript\s+to\s+(?:view|run|use)", re.IGNORECASE),
    re.compile(r"javascript\s+is\s+required", re.IGNORECASE),
    re.compile(r"please\s+enable\s+javascript", re.IGNORECASE),
]

SCRIPT_BUNDLE_PATTERN = re.compile(
    r"""<script\b[^>]*(?:src=["'][^"']*(?:main|bundle|chunk|app|vite|webpack|next)[^"']*["']|type=["']module["'])""",
    re.IGNORECASE,
)


def extract_visible_text_length(html: str) -> int:
    """Approximate the count of visible text characters outside tags/scripts."""
    no_script = re.sub(
        r"<(?:script|style|svg|noscript)\b[^>]*>.*?</(?:script|style|svg|noscript)>",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    no_tags = re.sub(r"<[^>]+>", " ", no_script)
    text = " ".join(no_tags.split())
    return len(text)


def is_spa_shell(html: str, min_text_threshold: int = 250) -> bool:
    """Determine whether an HTML response is an unrendered Single-Page Application (SPA) shell."""
    if not html or not html.strip():
        return False

    visible_len = extract_visible_text_length(html)

    # 1. Direct empty SPA mount point (e.g. <div id="root"></div>)
    for pattern in SPA_MOUNT_PATTERNS:
        if pattern.search(html) and visible_len < 1000:
            return True

    # 2. Noscript warning with low visible content
    for pattern in JS_REQUIRED_PATTERNS:
        if pattern.search(html) and visible_len < 600:
            return True

    # 3. Very low visible text (< 250 chars) but has JavaScript client bundles
    return bool(visible_len < min_text_threshold and SCRIPT_BUNDLE_PATTERN.search(html))
