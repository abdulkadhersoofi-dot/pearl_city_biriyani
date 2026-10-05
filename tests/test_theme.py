from app.models.tenant import Tenant
from app.utils.theme import contrast_text_color, tenant_theme_css


def test_theme_css_is_empty_without_a_tenant():
    assert tenant_theme_css(None) == ""


def test_theme_css_uses_the_tenants_own_explicit_colors_and_font():
    tenant = Tenant(primary_color="#8e24aa", background_color="#111111", text_color="#f5f6f4", font_family="serif")
    css = tenant_theme_css(tenant)
    assert "--color-bg: #111111" in css
    assert "--color-page-text: #f5f6f4" in css
    assert "--color-primary: #8e24aa" in css
    assert "Georgia" in css


def test_text_color_is_never_auto_derived_from_background():
    # A tenant who picked a dark background but left text_color at the
    # (dark) default gets exactly that - no automatic contrast override.
    # Readability is their call; the live preview in Settings is what
    # catches this, not silent magic here.
    tenant = Tenant(primary_color="#1f7a4d", background_color="#111111", text_color="#1f2a24", font_family="system")
    css = tenant_theme_css(tenant)
    assert "--color-page-text: #1f2a24" in css


def test_unknown_font_key_falls_back_to_system_stack():
    tenant = Tenant(primary_color="#1f7a4d", background_color="#f6f7f5", text_color="#1f2a24", font_family="comic-sans")
    css = tenant_theme_css(tenant)
    assert "-apple-system" in css


def test_a_corrupted_hex_color_falls_back_to_the_default_instead_of_breaking_the_style_tag():
    tenant = Tenant(
        primary_color="not-a-color",
        background_color="</style><script>",
        text_color="javascript:alert(1)",
        font_family="system",
    )
    css = tenant_theme_css(tenant)
    assert "<script>" not in css
    assert "--color-primary: #1f7a4d" in css
    assert "--color-bg: #f6f7f5" in css
    assert "--color-page-text: #1f2a24" in css


def test_contrast_text_color_picks_light_text_for_dark_backgrounds():
    assert contrast_text_color("#111111") == "#f5f6f4"
    assert contrast_text_color("#f6f7f5") == "#1f2a24"
