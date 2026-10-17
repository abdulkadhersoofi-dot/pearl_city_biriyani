"""Printing a receipt PDF from a hidden frame (static/js/print.js). Newer
Chrome shows a PDF in a frame the page may not reach into, so print()
there throws a SecurityError - the receipt must then open in a tab (or,
with pop-ups blocked, offer a link) instead of breaking the screen."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PRINT_JS = (ROOT / "app" / "static" / "js" / "print.js").read_text()

STUB = """
var opened = [], notes = [], printed = 0, afterRan = 0, popupsBlocked = %(blocked)s;
function el() { return {children: [], style: {}, setAttribute() {}, addEventListener() {},
  appendChild(c) { this.children.push(c); notes.push(c); }, remove() {}}; }
var frame = {src: "", onload: null, contentWindow: {focus() {},
  get print() { if (%(throws)s) { throw new Error("SecurityError: Blocked a frame from accessing a cross-origin frame"); }
                return function () { printed++; }; }}};
var stack = el();
var document = {getElementById: id => id === "receipt-print-frame" ? frame : null,
  querySelector: () => stack, createElement: el, body: el()};
var window = {open: url => { if (popupsBlocked) return null; opened.push(url); return {}; }};
var setTimeout = () => {};
"""
DRIVE = """
window.ArfaPrint.pdf("/pos/bills/7/receipt.pdf?format=3in", {after: () => { afterRan++; }});
frame.onload();
console.log(JSON.stringify({src: frame.src, printed, opened, notes: notes.length, afterRan}));
"""


def _run(tmp_path, throws, blocked):
    path = tmp_path / "print.js"
    path.write_text(STUB % {"throws": "true" if throws else "false", "blocked": "true" if blocked else "false"}
                    + PRINT_JS + DRIVE)
    res = subprocess.run(["node", str(path)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_prints_from_the_frame_when_the_browser_allows_it(tmp_path):
    out = _run(tmp_path, throws=False, blocked=False)
    assert out == {"src": "/pos/bills/7/receipt.pdf?format=3in", "printed": 1, "opened": [], "notes": 0, "afterRan": 1}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_refused_frame_opens_the_receipt_in_a_tab_instead(tmp_path):
    out = _run(tmp_path, throws=True, blocked=False)
    assert out["printed"] == 0 and out["opened"] == ["/pos/bills/7/receipt.pdf?format=3in"]
    assert out["notes"] == 0 and out["afterRan"] == 1


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_with_pop_ups_blocked_a_link_is_offered(tmp_path):
    out = _run(tmp_path, throws=True, blocked=True)
    assert out["opened"] == [] and out["notes"] == 1 and out["afterRan"] == 1


def test_no_screen_calls_print_on_a_frame_directly():
    """Every PDF print goes through ArfaPrint.pdf (the offline receipt is
    same-page HTML and keeps its own guarded call)."""
    for path in (ROOT / "app" / "templates").rglob("*.html"):
        assert "contentWindow.print" not in path.read_text(), path
    assert "js/print.js" in (ROOT / "app" / "templates" / "base.html").read_text()
