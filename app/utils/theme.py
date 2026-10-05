"""Per-tenant site theme (buttons, background, font) - applied as a CSS
custom-property override on top of app.css for every page a Client Admin
or Staff user sees, and on that tenant's own branded login page.

Font choices are a fixed, curated set of OS-available stacks rather than
free text or a CDN font, to keep this self-hosted app's offline-friendly
design intact and to rule out any CSS-injection surface in the stored
value.
"""

import re

HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

FONT_FAMILY_CHOICES = [
    ("system", "Default (system sans-serif)"),
    ("serif", "Serif"),
    ("rounded", "Rounded sans-serif"),
    ("mono", "Monospace"),
]

FONT_FAMILY_STACKS = {
    "system": '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif',
    "serif": 'Georgia, "Times New Roman", Times, serif',
    "rounded": 'Verdana, "Trebuchet MS", Tahoma, sans-serif',
    "mono": '"Courier New", Courier, monospace',
}

DEFAULT_PRIMARY_COLOR = "#1f7a4d"
DEFAULT_BACKGROUND_COLOR = "#f6f7f5"
DEFAULT_FONT_FAMILY = "system"

# Matches app.css's own :root defaults for --color-text / a light
# equivalent - picked per-tenant by contrast against their own
# --color-bg, never exposed as a separate setting.
DARK_PAGE_TEXT = "#1f2a24"
LIGHT_PAGE_TEXT = "#f5f6f4"


def _safe_hex(value, fallback):
    return value if value and HEX_COLOR_RE.match(value) else fallback


def _contrast_text_color(background_hex: str) -> str:
    r, g, b = int(background_hex[1:3], 16), int(background_hex[3:5], 16), int(background_hex[5:7], 16)
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return DARK_PAGE_TEXT if luminance > 0.5 else LIGHT_PAGE_TEXT


def tenant_theme_css(tenant) -> str:
    """CSS text (no <style> tags) overriding buttons/background/font for
    this tenant, or "" if there's no tenant (platform pages keep app.css
    as-is). Page text color is derived from the background automatically
    for contrast - cards/inputs stay white regardless, see app.css."""
    if not tenant:
        return ""
    primary = _safe_hex(tenant.primary_color, DEFAULT_PRIMARY_COLOR)
    background = _safe_hex(tenant.background_color, DEFAULT_BACKGROUND_COLOR)
    font_stack = FONT_FAMILY_STACKS.get(tenant.font_family, FONT_FAMILY_STACKS[DEFAULT_FONT_FAMILY])
    page_text = _contrast_text_color(background)
    return (
        f":root {{ --color-primary: {primary}; --color-primary-dark: {primary}; "
        f"--color-bg: {background}; --color-page-text: {page_text}; }}"
        f" body {{ font-family: {font_stack}; }}"
    )
