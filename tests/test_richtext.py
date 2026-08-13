from grok_telegram.richtext import markdown_to_telegram_html, safe_html


def test_bold_italic_code():
    out = markdown_to_telegram_html("Hello **world** and *nice* plus `code`")
    assert "<b>world</b>" in out
    assert "<i>nice</i>" in out
    assert "<code>code</code>" in out


def test_escapes_raw_html():
    out = markdown_to_telegram_html("use <script>alert(1)</script> please")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_fenced_code_block():
    raw = "Before\n```\nline1\nline2\n```\nAfter"
    out = markdown_to_telegram_html(raw)
    assert "<pre>" in out
    assert "line1" in out
    assert "line2" in out


def test_link():
    out = markdown_to_telegram_html("see [docs](https://x.ai/docs) now")
    assert '<a href="https://x.ai/docs">docs</a>' in out


def test_tool_line_blockquote():
    out = markdown_to_telegram_html("> 🔧 run_terminal_command: ls /tmp")
    assert "<blockquote>" in out
    assert "run_terminal_command" in out


def test_heading_and_list():
    raw = "# Title\n- one\n- two"
    out = markdown_to_telegram_html(raw)
    assert "<b>Title</b>" in out
    assert "• one" in out
    assert "• two" in out


def test_status_suffix():
    out = markdown_to_telegram_html("Done.\n\n✅ ($0.0123)")
    assert "<b>✅</b>" in out
    assert "<i>($0.0123)</i>" in out


def test_safe_html_empty():
    assert safe_html("") == ""
