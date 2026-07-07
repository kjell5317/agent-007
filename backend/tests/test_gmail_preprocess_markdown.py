import base64

from app.services.input.gmail.preprocess import _html_to_markdown, preprocess_message


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def test_html_to_markdown_covers_common_elements():
    html = """
    <html><body>
      <h2>Weekly update</h2>
      <p>Hello <strong>team</strong> and <em>friends</em>.
         Read <a href="https://example.com/doc">the doc</a>.</p>
      <ul><li>first</li><li>second</li></ul>
      <ol><li>one</li><li>two</li></ol>
      <blockquote>a quoted note</blockquote>
      <pre><code>x = 1</code></pre>
    </body></html>
    """
    md = _html_to_markdown(html)
    assert "## Weekly update" in md
    assert "**team**" in md
    assert "*friends*" in md
    assert "[the doc](https://example.com/doc)" in md
    assert "- first" in md
    assert "- second" in md
    assert "1. one" in md
    assert "2. two" in md
    assert "> a quoted note" in md
    assert "```\nx = 1\n```" in md


def test_layout_table_is_flattened_not_a_markdown_grid():
    html = "<table><tr><td>Label</td><td>Value</td></tr></table>"
    md = _html_to_markdown(html)
    assert "Label" in md and "Value" in md
    assert "|" not in md  # no bogus markdown table pipes


def test_bare_anchor_without_http_scheme_keeps_text_only():
    md = _html_to_markdown('<p>see <a href="#anchor">here</a></p>')
    assert "here" in md
    assert "](#anchor)" not in md


def test_preprocess_html_only_message_yields_markdown_and_urls():
    raw = {
        "threadId": "t1",
        "payload": {
            "mimeType": "text/html",
            "headers": [
                {"name": "Subject", "value": "Hi"},
                {"name": "From", "value": "a@b.com"},
            ],
            "body": {"data": _b64url('<p>See <a href="https://x.com/p">the doc</a>.</p>')},
        },
    }
    result = preprocess_message(raw)
    assert "[the doc](https://x.com/p)" in result.body
    assert "https://x.com/p" in result.metadata["urls"]


def test_html_signature_is_stripped_after_markdown_conversion():
    html = (
        "<html><body><p>Real content.</p>"
        "<div>-- <br>Sent from my iPhone</div></body></html>"
    )
    raw = {
        "threadId": "t1",
        "payload": {
            "mimeType": "text/html",
            "headers": [{"name": "Subject", "value": "s"}],
            "body": {"data": _b64url(html)},
        },
    }
    result = preprocess_message(raw)
    assert "Real content." in result.body
    assert "Sent from my iPhone" not in result.body
    assert not result.body.rstrip().endswith("--")


def test_preprocess_prefers_plain_text_over_html():
    raw = {
        "threadId": "t1",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": "Subject", "value": "Hi"}],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64url("just plain text")}},
                {"mimeType": "text/html", "body": {"data": _b64url("<b>bold html</b>")}},
            ],
        },
    }
    result = preprocess_message(raw)
    assert result.body == "just plain text"
    assert "**" not in result.body
