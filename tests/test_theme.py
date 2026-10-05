from app.models.tenant import Tenant
from app.utils.theme import tenant_theme_css


def test_theme_css_is_empty_without_a_tenant():
    assert tenant_theme_css(None) == ""


def test_dark_background_gets_light_page_text_for_contrast():
    tenant = Tenant(primary_color="#8e24aa", background_color="#111111", font_family="serif")
    css = tenant_theme_css(tenant)
    assert "--color-bg: #111111" in css
    assert "--color-page-text: #f5f6f4" in css
    assert "--color-primary: #8e24aa" in css
    assert "Georgia" in css


def test_light_background_keeps_dark_page_text():
    tenant = Tenant(primary_color="#1f7a4d", background_color="#f6f7f5", font_family="system")
    css = tenant_theme_css(tenant)
    assert "--color-page-text: #1f2a24" in css


def test_unknown_font_key_falls_back_to_system_stack():
    tenant = Tenant(primary_color="#1f7a4d", background_color="#f6f7f5", font_family="comic-sans")
    css = tenant_theme_css(tenant)
    assert "-apple-system" in css


def test_a_corrupted_hex_color_falls_back_to_the_default_instead_of_breaking_the_style_tag():
    tenant = Tenant(primary_color="not-a-color", background_color="</style><script>", font_family="system")
    css = tenant_theme_css(tenant)
    assert "<script>" not in css
    assert "--color-primary: #1f7a4d" in css
    assert "--color-bg: #f6f7f5" in css
