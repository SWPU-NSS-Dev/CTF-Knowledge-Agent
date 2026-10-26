from pathlib import Path


def index_html() -> str:
    return (Path(__file__).parent / "index.html").read_text(encoding="utf-8")
