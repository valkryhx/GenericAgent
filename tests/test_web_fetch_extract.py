import sys
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.tools.web_fetch.extract import extract_content  # noqa: E402
from genericagent.tools.web_fetch.models import FetchError  # noqa: E402


class WebFetchExtractTest(unittest.TestCase):
    def test_html_is_converted_to_markdown_and_scripts_are_removed(self):
        html = b"""
        <html><head><style>.x{}</style><script>alert(1)</script></head>
        <body><main><h1>Title</h1><p>Hello <strong>world</strong>.</p></main></body></html>
        """
        result = extract_content(html, "text/html; charset=utf-8")

        self.assertIn("# Title", result.text)
        self.assertIn("Hello **world**", result.text)
        self.assertNotIn("alert(1)", result.text)
        self.assertNotIn(".x{}", result.text)
        self.assertEqual("text/markdown", result.content_type)

    def test_plain_text_is_returned_without_markdown_conversion(self):
        result = extract_content(b"line one\nline two", "text/plain; charset=utf-8")
        self.assertEqual("line one\nline two", result.text)

    def test_markdown_is_returned_unchanged(self):
        result = extract_content(b"# Heading\nbody", "text/markdown; charset=utf-8")
        self.assertEqual("# Heading\nbody", result.text)

    def test_json_is_pretty_printed(self):
        result = extract_content(b'{"b":2,"a":1}', "application/json")
        self.assertIn('"a": 1', result.text)
        self.assertIn('"b": 2', result.text)

    def test_offset_is_applied_after_extraction(self):
        result = extract_content(
            b"<html><body><main><p>abcdef</p></main></body></html>",
            "text/html; charset=utf-8",
            offset=3,
        )
        self.assertTrue(result.text.startswith("def"))
        self.assertEqual(3, result.offset)

    def test_negative_offset_is_rejected(self):
        with self.assertRaises(FetchError) as raised:
            extract_content(b"text", "text/plain", offset=-1)
        self.assertEqual("URL_INVALID", raised.exception.code)

    def test_invalid_charset_has_stable_error(self):
        with self.assertRaises(FetchError) as raised:
            extract_content(b"\xff", "text/plain; charset=utf-8")
        self.assertEqual("DECODE_FAILED", raised.exception.code)

    def test_binary_content_is_not_returned_as_text(self):
        result = extract_content(b"\x89PNG\r\n", "image/png")
        self.assertIsNone(result.text)
        self.assertEqual("image/png", result.content_type)
        self.assertTrue(result.binary)

    def test_empty_html_is_marked_as_empty_content(self):
        result = extract_content(b"<html><body></body></html>", "text/html; charset=utf-8")

        self.assertTrue(result.empty_content)
        self.assertEqual("", result.text)
        self.assertEqual(0, result.text_length)

    def test_navigation_only_html_is_marked_as_navigation_only(self):
        html = b"""
        <html><body>
          <nav><a href="/one">One</a><a href="/two">Two</a><a href="/three">Three</a></nav>
          <header><a href="/login">Login</a></header>
          <footer><a href="/privacy">Privacy</a></footer>
        </body></html>
        """
        result = extract_content(html, "text/html; charset=utf-8")

        self.assertTrue(result.empty_content)
        self.assertGreaterEqual(result.link_text_length, result.text_length)

    def test_short_real_text_is_not_marked_empty(self):
        result = extract_content(
            b"<html><body><article><p>Short but real status update.</p></article></body></html>",
            "text/html; charset=utf-8",
        )

        self.assertFalse(result.empty_content)
        self.assertFalse(result.navigation_only)
        self.assertIn("Short but real status update.", result.text)

    def test_main_content_wins_over_repeated_navigation(self):
        html = b"""
        <html><body>
          <nav>
            <a href="/platform">Platform</a><a href="/solutions">Solutions</a>
            <a href="/docs">Docs</a><a href="/pricing">Pricing</a>
          </nav>
          <main><article>
            <h1>Project title</h1>
            <p>This is the substantive project description.</p>
            <p>It contains multiple sentences and useful details for readers.</p>
          </article></main>
          <footer><a href="/terms">Terms</a><a href="/privacy">Privacy</a></footer>
        </body></html>
        """
        result = extract_content(html, "text/html; charset=utf-8")

        self.assertIn("# Project title", result.text)
        self.assertIn("substantive project description", result.text)
        self.assertNotIn("[Platform](", result.text)
        self.assertNotIn("[Terms](", result.text)

    def test_link_dense_landing_page_prefers_text_blocks(self):
        html = b"""
        <html><body>
          <div id="links">
            <a href="/a">Link A</a><a href="/b">Link B</a>
            <a href="/c">Link C</a><a href="/d">Link D</a>
          </div>
          <div id="body">
            <p>Actual article paragraph with enough natural language text.</p>
            <p>A second paragraph adds more substantive information.</p>
          </div>
        </body></html>
        """
        result = extract_content(html, "text/html; charset=utf-8")

        self.assertIn("Actual article paragraph", result.text)
        self.assertNotIn("[Link A](", result.text)

    def test_javascript_redirect_shell_is_empty_content(self):
        html = b'<html><body><script>location.replace(location.href.replace("https://","http://"));</script></body></html>'
        result = extract_content(html, "text/html; charset=utf-8")

        self.assertTrue(result.empty_content)

    def test_short_footer_legal_block_is_marked_navigation_only(self):
        html = b"""
        <html><body><div>
          <p><a href="/about">About us</a></p>
          <p><a href="/terms">Terms of Service</a></p>
          <p>Internet content licence 11220180008</p>
          <p>Copyright 2026 Example</p>
        </div></body></html>
        """
        result = extract_content(html, "text/html; charset=utf-8")

        self.assertTrue(result.navigation_only)

    def test_long_prose_with_one_link_is_not_navigation_only(self):
        html = b"""
        <html><body><article>
          <p>This release contains a substantive explanation of the change,
             why it matters, and how users should migrate safely.</p>
          <p>Read the <a href="/guide">migration guide</a> before upgrading.
             The guide covers compatibility, rollback, and verification steps.</p>
        </article></body></html>
        """
        result = extract_content(html, "text/html; charset=utf-8")

        self.assertFalse(result.navigation_only)
        self.assertIn("substantive explanation", result.text)

    def test_link_list_page_is_not_discarded(self):
        html = b"""
        <html><body><main><h1>Release notes</h1><ul>
          <li><a href="/release/3">Version 3</a> fixes startup and migration</li>
          <li><a href="/release/2">Version 2</a> adds offline support</li>
          <li><a href="/release/1">Version 1</a> initial public release</li>
        </ul></main></body></html>
        """
        result = extract_content(html, "text/html; charset=utf-8")

        self.assertFalse(result.navigation_only)
        self.assertIn("Version 3", result.text)
        self.assertIn("offline support", result.text)

    def test_long_code_page_is_not_treated_as_empty(self):
        html = b"""
        <html><body><main><h1>Protocol notes</h1>
          <pre>GET /v1/items HTTP/1.1
Authorization: Bearer token
Accept: application/json</pre>
          <p>The response contains a JSON object with the requested items.</p>
        </main></body></html>
        """
        result = extract_content(html, "text/html; charset=utf-8")

        self.assertFalse(result.empty_content)
        self.assertFalse(result.navigation_only)
        self.assertIn("GET /v1/items", result.text)

    def test_article_keeps_header_and_removes_global_menu(self):
        html = b"""
        <html><body>
          <header class="global"><a href="/login">Login</a></header>
          <main><article>
            <header><h1>Article title</h1><p>Article lede paragraph.</p></header>
            <p>Main article body with substantive text for readers.</p>
          </article></main>
        </body></html>
        """
        result = extract_content(html, "text/html; charset=utf-8")

        self.assertIn("Article title", result.text)
        self.assertIn("Article lede paragraph", result.text)
        self.assertNotIn("[Login]", result.text)

    def test_document_title_in_header_is_not_discarded(self):
        html = b"""
        <html><body><header><h1>Documentation topic</h1>
          <p>This page explains the protocol and its safety boundaries.</p>
        </header></body></html>
        """
        result = extract_content(html, "text/html; charset=utf-8")

        self.assertFalse(result.empty_content)
        self.assertIn("Documentation topic", result.text)
        self.assertIn("protocol", result.text)

    def test_large_document_cleanup_stays_within_practical_budget(self):
        paragraphs = "".join(
            f"<div class='menu'><a href='/m{i}'>Menu {i}</a></div>"
            for i in range(1800)
        )
        article = "".join(
            f"<p>Paragraph {i} contains substantive documentation text for readers.</p>"
            for i in range(2200)
        )
        html = (
            "<html><body>" + paragraphs + "<main><article><h1>Large document</h1>"
            + article + "</article></main></body></html>"
        ).encode("utf-8")
        started = time.monotonic()
        result = extract_content(html, "text/html; charset=utf-8")
        elapsed = time.monotonic() - started

        self.assertFalse(result.empty_content)
        self.assertIn("Large document", result.text)
        self.assertLess(elapsed, 6.0)


if __name__ == "__main__":
    unittest.main()
