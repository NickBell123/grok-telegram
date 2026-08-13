from pathlib import Path

from grok_telegram.media import (
    MediaCollector,
    classify,
    extract_path_strings,
    is_image_path,
    looks_noise,
    paths_from_tool_result,
    paths_from_tool_use,
    resolve_existing,
)


def test_is_image_path():
    assert is_image_path("/tmp/x.png")
    assert is_image_path("foo.JPG")
    assert not is_image_path("/tmp/x.pdf")


def test_extract_absolute_and_relative_paths():
    text = "Saved to /tmp/out/chart.png and also images/1.jpg for review."
    paths = extract_path_strings(text)
    assert "/tmp/out/chart.png" in paths
    assert "images/1.jpg" in paths


def test_resolve_existing_relative_to_cwd(tmp_path: Path):
    f = tmp_path / "plot.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 20)
    assert resolve_existing("plot.png", str(tmp_path)) == f.resolve()
    assert resolve_existing("missing.png", str(tmp_path)) is None


def test_deny_secretish_names(tmp_path: Path):
    f = tmp_path / ".env"
    f.write_text("SECRET=1")
    assert resolve_existing(str(f), str(tmp_path)) is None


def test_looks_noise_terminal_logs():
    assert looks_noise("/home/nick/.grok/sessions/x/terminal/call-abc.log")
    assert looks_noise("debug.log")
    assert not looks_noise("/home/nick/Desktop/avatar.jpg")


def test_classify_photo_vs_document(tmp_path: Path):
    img = tmp_path / "a.png"
    img.write_bytes(b"x" * 100)
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"%PDF" + b"x" * 100)
    assert classify(img).kind == "photo"
    assert classify(doc).kind == "document"
    assert classify(img, force="document").kind == "document"


def test_paths_from_tool_use_write_and_image():
    assert paths_from_tool_use("write", {"file_path": "/tmp/x.png"}) == ["/tmp/x.png"]
    assert paths_from_tool_use("image_gen", {"prompt": "cat"}) == []


def test_paths_from_tool_result_string_and_blocks():
    assert "/tmp/img.png" in paths_from_tool_result("wrote /tmp/img.png")
    assert "/tmp/img.png" in paths_from_tool_result([{"type": "text", "text": "path=/tmp/img.png"}])


def test_collector_auto_images_only_skips_source_and_logs(tmp_path: Path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"pngdata")
    src = tmp_path / "main.py"
    src.write_text("print(1)\n")
    log = tmp_path / "terminal" / "call-1.log"
    log.parent.mkdir()
    log.write_text("shell noise\n")
    c = MediaCollector(str(tmp_path))  # images_only default
    c.note_tool_use("write", {"file_path": str(img)})
    c.note_tool_use("write", {"file_path": str(src)})
    c.note_text(f"Also see {src} and {log}")
    c.note_tool_result(str(log))
    names = [m.path.name for m in c.candidates()]
    assert names == ["shot.png"]


def test_collector_dedupes_same_path(tmp_path: Path):
    img = tmp_path / "a.jpg"
    img.write_bytes(b"jpg")
    c = MediaCollector(str(tmp_path))
    c.note_text(str(img))
    c.note_tool_result(str(img))
    assert len(c.candidates()) == 1


def test_collector_dedupes_identical_content_copies(tmp_path: Path):
    """Session image + Desktop copy of the same bytes → one attach."""
    a = tmp_path / "images"
    a.mkdir()
    session = a / "2.jpg"
    desktop = tmp_path / "avatar-2.jpg"
    blob = b"\xff\xd8\xff" + b"identical-jpeg-payload" * 20
    session.write_bytes(blob)
    desktop.write_bytes(blob)
    c = MediaCollector(str(tmp_path))
    c.note_text(str(session))
    c.note_text(str(desktop))
    assert len(c.candidates()) == 1
    # First seen wins.
    assert c.candidates()[0].path == session.resolve()


def test_collector_images_only_false_allows_docs(tmp_path: Path):
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-1.4")
    c = MediaCollector(str(tmp_path), images_only=False)
    c.note_text(str(doc))
    assert len(c.candidates()) == 1
    assert c.candidates()[0].kind == "document"
