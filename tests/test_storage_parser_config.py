from __future__ import annotations

from ctfka.config import Settings, SourceConfig
from ctfka.contracts import FetchedDocument
from ctfka.ingestion import parse_document


def test_markdown_is_preserved_and_heading_beats_metadata():
    markdown = "# Source H1\n\n```c\nif (x < 3) {\n  return;\n}\n```\n\n\nTail &lt;literal&gt;\n"
    parsed = parse_document(
        FetchedDocument("file.md", markdown.encode(), metadata={"title": "file-stem"})
    )
    assert parsed.title == "Source H1"
    assert parsed.content == markdown


def test_html_conversion_keeps_title_table_link_and_code_language():
    html = b'<html><head><title>Source title</title></head><body><main><h1>Visible title</h1><p>Keep <a href="/ref">links</a>.</p><table><tr><th>Name</th><th>Value</th></tr><tr><td>GOT</td><td>PLT</td></tr></table><pre><code class="language-c">int main() { return 0; }</code></pre></main></body></html>'
    parsed = parse_document(FetchedDocument("https://example.test", html, "text/html"))
    assert parsed.title == "Visible title"
    assert "[links](/ref)" in parsed.content
    assert "| Name | Value |" in parsed.content
    assert "```c\nint main() { return 0; }\n```" in parsed.content


def test_configuration_has_no_arbitrary_operational_caps():
    config = SourceConfig(
        adapter="example", concurrency=100_000, max_attempts=100_000, timeout_seconds=100_000
    )
    assert config.concurrency == 100_000
    assert config.max_attempts == 100_000
    assert config.timeout_seconds == 100_000
    assert Settings(workers=100_000).workers == 100_000
