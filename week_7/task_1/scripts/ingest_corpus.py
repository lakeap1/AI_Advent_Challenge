"""Fetch the explicit primary-source allowlist into a private reference corpus.

No crawling, generated summaries, cloud model calls or downloaded content in Git.
The extraction keeps original article sections; versions/licenses remain metadata.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Comment
import httpx


HOSTS = {
    "docs.unity3d.com", "docs.unity.com", "docs.blender.org",
    "learnopengl.com", "google.github.io", "raw.githubusercontent.com",
}
LOCAL_SOURCES = {
    "README.md", "docs/usage.md", "docs/architecture.md", "docs/api.md",
    "corpus/glossary.md",
}


def allowed_url(url: str) -> bool:
    parsed = urlparse(url)
    return (parsed.scheme == "https" and parsed.hostname in HOSTS
            and parsed.port in (None, 443) and not parsed.username and not parsed.password)


def source_path(root: Path, value: str) -> Path:
    root = root.resolve()
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Corpus path escapes the application root")
    relative = path.relative_to(root).as_posix()
    if relative not in LOCAL_SOURCES and not (
        relative.startswith("data/corpus/") and path.suffix == ".md"
    ):
        raise ValueError("Only explicit product docs, glossary and private corpus files are allowed")
    return path


def extract_document(html: str, source: dict) -> str:
    if source.get("format") == "markdown":
        text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
        text = re.sub(r"<!--.*?-->|<meta[^>]*>", "", text, flags=re.S | re.I)
        return re.sub(r"\n{4,}", "\n\n\n", text).strip() + "\n"
    soup = BeautifulSoup(html, "html.parser")
    article = soup.select_one(source.get("selector", "main"))
    if article is None:
        raise ValueError("Declared article selector was not found")
    for node in article.select(
        "nav, header, footer, script, style, noscript, iframe, form, button, "
        ".breadcrumbs, .nextprev, .navigation, .related, .quiz, .question, "
        "#disqus_thread, .ad, .ad-container, .feedback, #_leavefeedback"
    ):
        node.decompose()
    for node in article.find_all(string=lambda text: isinstance(text, Comment)):
        node.extract()
    output = []
    for node in article.find_all(re.compile(r"^(h[1-6]|p|li|pre|table)$")):
        if node.find_parent(["pre", "table"]) is not None:
            continue
        if node.name == "p" and node.find_parent("li") is not None:
            continue
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        if not text:
            continue
        if node.name.startswith("h"):
            output.append("#" * int(node.name[1]) + " " + text)
        elif node.name == "pre":
            output.append("```\n" + node.get_text().strip() + "\n```")
        elif node.name == "li":
            output.append("- " + text)
        elif node.name == "table":
            rows = [" | ".join(re.sub(r"\s+", " ", cell.get_text(" ", strip=True))
                               for cell in row.find_all(["td", "th"]))
                    for row in node.find_all("tr")]
            output.append("\n".join(row for row in rows if row.strip(" |")))
        else:
            output.append(text)
    result = "\n\n".join(output).strip()
    if not result:
        raise ValueError("The extracted article is empty")
    return result + "\n"


def download(url: str) -> tuple[str, str]:
    with httpx.Client(timeout=40, trust_env=False, follow_redirects=False,
                      headers={"User-Agent": "GraphicsLearningReference/1.0 (private educational corpus)"}) as client:
        for _ in range(5):
            if not allowed_url(url):
                raise ValueError("Remote source is outside the primary-source allowlist")
            with client.stream("GET", url) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers["location"])
                    continue
                response.raise_for_status()
                data = bytearray()
                for block in response.iter_bytes():
                    data.extend(block)
                    if len(data) > 6_000_000:
                        raise ValueError("Article exceeds the download limit")
                return data.decode("utf-8"), url
    raise ValueError("Too many source redirects")


def ingest(root: Path, *, refresh=False, only=None, remote_only=False) -> dict:
    root = root.resolve()
    manifest = json.loads((root / "corpus/manifest.json").read_text(encoding="utf-8-sig"))
    sources = manifest["sources"]
    ids = [source["id"] for source in sources]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate source IDs")
    receipt_path = root / "data/corpus/ingestion.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    previous = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.exists() else {}
    old = {row["id"]: row for row in previous.get("sources", [])}

    def one(source):
        base = {key: source.get(key) for key in (
            "id", "url", "title", "domain", "version", "pipeline", "language",
            "license", "license_url", "attribution", "path")}
        path = source_path(root, source["path"])
        if only and source["id"] not in only:
            return old.get(source["id"], {**base, "status": "not_run"})
        if not source.get("download_url"):
            if remote_only:
                return {**base, "status": "local_pending"}
            if not path.exists():
                return {**base, "status": "missing_local"}
            content = path.read_text(encoding="utf-8-sig")
            return {**base, "status": "local", "characters": len(content),
                    "sha256": hashlib.sha256(content.encode()).hexdigest()}
        try:
            old_row = old.get(source["id"], {})
            if path.exists() and not refresh and old_row.get("url") == source["url"]:
                content = path.read_text(encoding="utf-8")
                if hashlib.sha256(content.encode()).hexdigest() == old_row.get("sha256"):
                    return {**old_row, "status": "cached"}
            raw, final_url = download(source["download_url"])
            content = extract_document(raw, source)
            if len(content) < source.get("min_characters", 200):
                raise ValueError("Article has too little substantive text")
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            pending = path.with_suffix(".pending")
            pending.write_text(content, encoding="utf-8")
            pending.replace(path)
            return {**base, "status": "downloaded", "final_url": final_url,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "characters": len(content),
                    "sha256": hashlib.sha256(content.encode()).hexdigest(),
                    "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                    "extraction": "original article sections; navigation removed; no model summarization"}
        except (httpx.HTTPError, ValueError, OSError) as error:
            # Error class/status is enough; do not save potentially sensitive HTTP bodies.
            return {**base, "status": "error", "error_type": type(error).__name__,
                    "http_status": getattr(getattr(error, "response", None), "status_code", None)}

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(one, sources))
    successful = {"downloaded", "cached", "local"}
    checked = [row for row in rows if not (remote_only and row["status"] == "local_pending")]
    receipt = {"version": 1, "updated_at": datetime.now(timezone.utc).isoformat(),
               "complete": all(row["status"] in successful for row in checked),
               "source_count": len(rows), "successful": sum(row["status"] in successful for row in rows),
               "characters": sum(row.get("characters", 0) for row in rows), "sources": rows}
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--only", nargs="+")
    parser.add_argument("--remote-only", action="store_true")
    arguments = parser.parse_args()
    receipt = ingest(Path(__file__).resolve().parents[1], refresh=arguments.refresh,
                     only=arguments.only, remote_only=arguments.remote_only)
    print(json.dumps({key: receipt[key] for key in ("complete", "source_count", "successful", "characters")}))
    for item in receipt["sources"]:
        print(item["id"], item["status"], item.get("http_status", ""))
    raise SystemExit(0 if receipt["complete"] else 1)
