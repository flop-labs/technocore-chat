"""Regression test for #582: sitemap_xml must XML-escape the public URL."""

from manifest import sitemap_xml


def test_sitemap_xml_escapes_ampersand():
    """An & in CHAT_PUBLIC_URL must become &amp; in the sitemap."""
    xml = sitemap_xml("https://example.com?a=1&b=2")
    assert "&amp;" in xml, "bare & must be escaped as &amp;"
    assert "?a=1&b=2" not in xml, "bare & must not appear in <loc>"
