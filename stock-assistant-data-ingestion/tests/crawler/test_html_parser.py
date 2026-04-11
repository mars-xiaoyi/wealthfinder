from app.crawler.html_parser import extract_body_css, extract_body_auto


# ---------------------------------------------------------------------------
# extract_body_auto
# ---------------------------------------------------------------------------

def test_extract_body_auto_returns_text():
    html = """
    <html><body>
        <article><p>This is the main article body with enough content for extraction.</p></article>
        <nav>Navigation links</nav>
    </body></html>
    """
    result = extract_body_auto(html)
    assert result is not None
    assert "main article body" in result


def test_extract_body_auto_returns_none_when_empty():
    result = extract_body_auto("")
    assert result is None


def test_extract_body_auto_returns_none_when_no_content():
    html = "<html><body></body></html>"
    result = extract_body_auto(html)
    assert result is None


# ---------------------------------------------------------------------------
# extract_body_css
# ---------------------------------------------------------------------------

def test_extract_body_css_returns_matched_text():
    html = '<html><body><div class="newscon">Article content here</div></body></html>'
    result = extract_body_css(html, "[class*='newscon']")
    assert result == "Article content here"


def test_extract_body_css_returns_none_when_no_match():
    html = "<html><body><div>No match</div></body></html>"
    result = extract_body_css(html, ".nonexistent")
    assert result is None


def test_extract_body_css_returns_none_when_element_empty():
    html = '<html><body><div class="newscon">   </div></body></html>'
    result = extract_body_css(html, "[class*='newscon']")
    assert result is None
