"""Parse a release page HTML -> structured .parse.json.

Strategy: capture a structured view of every known field AND a `raw` block
with the page HTML hash, sluggish text dumps of every <h3>-delimited section,
plus an `unparsed` list for anything we don't recognize. The goal is that we
never need to re-fetch — re-parsing is always offline.

The page hash field on each download is the hash of the .7z archive (not the
ISO inside). Algorithm is inferred from length: 40 hex = sha1, 128 hex = sha512.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Optional

from selectolax.parser import HTMLParser, Node

from . import BASE_URL


PARSER_VERSION = 1
HEX_RE = re.compile(r"^[0-9a-f]+$")


def _text(n: Optional[Node]) -> str:
    if n is None:
        return ""
    return n.text(separator=" ", strip=True)


def _abs(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return BASE_URL + url
    return url


def _hash_alg(h: str) -> Optional[str]:
    if not h or not HEX_RE.match(h):
        return None
    return {32: "md5", 40: "sha1", 64: "sha256", 128: "sha512"}.get(len(h))


@dataclass
class DownloadRow:
    download_url: str          # /download/<uuid>  (resolved later)
    download_id: str           # the uuid alone
    filename: str              # from title attr of <a>
    display_name: str          # the visible link text
    media_kind: Optional[str]  # CD / 3.5 Floppy / Archive / DVD / ...
    version: str
    language: str
    architecture: Optional[str]
    file_size_text: str        # e.g. "400.67MB"
    archive_hash: Optional[str]
    archive_hash_alg: Optional[str]  # sha1 / sha512 / md5 / sha256 / None
    download_count: Optional[int]
    tags: list[str]            # prerelease, upgrade, etc.


@dataclass
class CommentSystem:
    provider: str              # "vanilla"
    identifier: Optional[str]  # e.g. "windows-95/osr-2"
    forum_url: Optional[str]
    category_id: Optional[str]


@dataclass
class ReleaseParse:
    product_slug: str
    release_slug: str
    url: str
    title: str
    subtitle: str               # the release name displayed under H1
    description_html: str
    description_text: str
    info: dict[str, str]        # the sidebar dl: {"Product type": "OS", "Vendor": "Microsoft", ...}
    info_links: dict[str, list[dict[str, str]]]  # same keys, links inside
    available_releases: list[dict[str, str]]    # [{slug, name, current}]
    screenshots: list[dict[str, str]]            # [{src, alt, title, screenshot_page}]
    screenshot_gallery_url: Optional[str]
    release_notes_html: str
    release_notes_text: str
    installation_html: str
    installation_text: str
    serials: list[str]
    downloads: list[dict[str, Any]]
    comments: CommentSystem
    page_meta: dict[str, str]   # og:* + <title>
    raw: dict[str, Any]
    parser_version: int = PARSER_VERSION


# ──────────────────────────────────────────────────────────────────────────


def _parse_info_sidebar(tree: HTMLParser) -> tuple[dict[str, str], dict[str, list[dict[str, str]]]]:
    info: dict[str, str] = {}
    links: dict[str, list[dict[str, str]]] = {}
    body = tree.css_first("#infoSheetBody")
    if body is None:
        return info, links
    cur_key: Optional[str] = None
    for child in body.iter():
        tag = (child.tag or "").lower()
        if tag == "dt":
            cur_key = _text(child)
            if cur_key:
                info.setdefault(cur_key, "")
                links.setdefault(cur_key, [])
        elif tag == "dd" and cur_key:
            info[cur_key] = (info[cur_key] + " " + _text(child)).strip()
            for a in child.css("a[href]"):
                links[cur_key].append(
                    {"text": _text(a), "href": _abs(a.attributes.get("href") or "")}
                )
    return info, links


def _parse_available_releases(tree: HTMLParser, product_slug: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    nav = tree.css_first("#releasesList")
    if nav is None:
        return out
    for a in nav.css("a[href]"):
        href = a.attributes.get("href") or ""
        m = re.match(rf"^/product/{re.escape(product_slug)}/([a-z0-9][a-z0-9\-]*)/?$", href)
        if not m:
            continue
        klass = a.attributes.get("class") or ""
        out.append({
            "slug": m.group(1),
            "name": _text(a),
            "current": "active" in klass,
        })
    return out


def _parse_screenshots(tree: HTMLParser) -> tuple[list[dict[str, str]], Optional[str]]:
    shots: list[dict[str, str]] = []
    panel = tree.css_first("#screenshotPanel")
    gallery_url: Optional[str] = None
    # The <h3><a href="/screenshot/..."> precedes the panel.
    for h in tree.css("h3"):
        a = h.css_first("a[href^='/screenshot/']")
        if a:
            gallery_url = _abs(a.attributes.get("href") or "")
            break
    if panel is None:
        return shots, gallery_url
    seen: set[str] = set()
    for item in panel.css(".carousel-item"):
        img = item.css_first("img")
        a = item.css_first("a[href]")
        if img is None:
            continue
        src = img.attributes.get("data-src") or img.attributes.get("src") or ""
        if not src or src in seen:
            continue
        seen.add(src)
        shots.append({
            "src": _abs(src),
            "alt": img.attributes.get("alt") or "",
            "title": img.attributes.get("title") or "",
            "screenshot_page": _abs(a.attributes.get("href") or "") if a else "",
        })
    return shots, gallery_url


def _parse_serials(tree: HTMLParser) -> list[str]:
    ul = tree.css_first("#serialsList")
    if ul is None:
        return []
    return [_text(li) for li in ul.css("li") if _text(li)]


def _parse_downloads(tree: HTMLParser) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    table = tree.css_first("#downloadsTable")
    if table is None:
        return out
    for tr in table.css("tbody > tr"):
        tds = tr.css("td")
        if len(tds) < 6:
            continue
        td_name, td_ver, td_lang, td_arch, td_size, td_dl = tds[:6]

        a = td_name.css_first("a[href]")
        href = a.attributes.get("href") or "" if a else ""
        m = re.match(r"^/download/([0-9a-f\-]+)/?$", href)
        download_id = m.group(1) if m else ""
        filename = a.attributes.get("title") if a else None

        media_img = td_name.css_first("img[title]")
        media_kind = media_img.attributes.get("title") if media_img else None

        arch_img = td_arch.css_first("img[title]")
        architecture = arch_img.attributes.get("title") if arch_img else _text(td_arch) or None

        # Version cell can have badges (prerelease, upgrade). Empty badges are
        # template placeholders styled via JS — only count badges with text.
        tags: list[str] = []
        for span in td_ver.css("span.badge"):
            if not _text(span):
                continue
            klass = span.attributes.get("class") or ""
            for t in klass.split():
                if t.startswith("downloadTag"):
                    tags.append(t.removeprefix("downloadTag").lower())
        version = _text(td_ver)

        size_span = td_size.css_first("span[title]")
        archive_hash = size_span.attributes.get("title").strip().lower() if size_span else None
        if archive_hash and not HEX_RE.match(archive_hash):
            archive_hash = None

        try:
            dl_count: Optional[int] = int(_text(td_dl))
        except ValueError:
            dl_count = None

        out.append(asdict(DownloadRow(
            download_url=_abs(href),
            download_id=download_id,
            filename=filename or "",
            display_name=_text(a) if a else _text(td_name),
            media_kind=media_kind,
            version=version,
            language=_text(td_lang),
            architecture=architecture,
            file_size_text=_text(td_size),
            archive_hash=archive_hash,
            archive_hash_alg=_hash_alg(archive_hash or ""),
            download_count=dl_count,
            tags=tags,
        )))
    return out


def _parse_h3_section(tree: HTMLParser, label: str) -> tuple[str, str]:
    """Find a sibling-collected block after <h3>label</h3> up to next <h3>."""
    for h3 in tree.css("h3"):
        if _text(h3).lower() != label.lower():
            continue
        html_parts: list[str] = []
        text_parts: list[str] = []
        node = h3.next
        while node is not None:
            tag = (node.tag or "").lower()
            if tag == "h3":
                break
            html_parts.append(node.html or "")
            txt = node.text(separator=" ", strip=True) if hasattr(node, "text") else ""
            if txt:
                text_parts.append(txt)
            node = node.next
        return "".join(html_parts).strip(), "\n".join(text_parts).strip()
    return "", ""


def _parse_comments(tree: HTMLParser) -> CommentSystem:
    if tree.css_first("#vanilla-comments") is None:
        return CommentSystem(provider="none", identifier=None, forum_url=None, category_id=None)
    identifier = forum_url = category_id = None
    for s in tree.css("script"):
        body = s.text() or ""
        if "vanilla_identifier" not in body:
            continue
        if (m := re.search(r'vanilla_identifier\s*=\s*"([^"]+)"', body)):
            identifier = m.group(1)
        if (m := re.search(r'vanilla_forum_url\s*=\s*"([^"]+)"', body)):
            forum_url = m.group(1)
        if (m := re.search(r'vanilla_category_id\s*=\s*\'([^\']+)\'', body)):
            category_id = m.group(1)
    return CommentSystem(provider="vanilla", identifier=identifier,
                         forum_url=forum_url, category_id=category_id)


def _parse_og_meta(tree: HTMLParser) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in tree.css("meta[property^='og:']"):
        prop = m.attributes.get("property") or ""
        content = m.attributes.get("content") or ""
        if prop:
            out[prop] = content
    t = tree.css_first("title")
    if t:
        out["title"] = _text(t)
    return out


def _description(tree: HTMLParser) -> tuple[str, str]:
    col = tree.css_first("#descriptionColumn")
    if col is None:
        return "", ""
    # Description is the <p> tags before the first <h3>.
    html_parts: list[str] = []
    text_parts: list[str] = []
    for child in col.iter():
        if (child.tag or "").lower() == "h3":
            break
        html_parts.append(child.html or "")
        txt = _text(child)
        if txt and (child.tag or "").lower() == "p":
            text_parts.append(txt)
    return "".join(html_parts).strip(), "\n\n".join(text_parts).strip()


def _title_subtitle(tree: HTMLParser) -> tuple[str, str]:
    h1 = tree.css_first("#descriptionColumn h1") or tree.css_first("h1")
    if h1 is None:
        return "", ""
    small = h1.css_first("small")
    sub = _text(small) if small else ""
    if small is not None:
        small.decompose()
    return _text(h1), sub


# ──────────────────────────────────────────────────────────────────────────


def parse_release_html(
    product_slug: str,
    release_slug: str,
    url: str,
    html_bytes: bytes,
) -> ReleaseParse:
    tree = HTMLParser(html_bytes)

    info, info_links = _parse_info_sidebar(tree)
    available = _parse_available_releases(tree, product_slug)
    shots, gallery = _parse_screenshots(tree)
    serials = _parse_serials(tree)
    downloads = _parse_downloads(tree)
    rn_html, rn_text = _parse_h3_section(tree, "Release notes")
    inst_html, inst_text = _parse_h3_section(tree, "Installation instructions")
    comments = _parse_comments(tree)
    og = _parse_og_meta(tree)
    desc_html, desc_text = _description(tree)
    title, subtitle = _title_subtitle(tree)

    raw = {
        "html_sha256": hashlib.sha256(html_bytes).hexdigest(),
        "html_bytes": len(html_bytes),
        "unparsed": [],  # populated below
    }

    # Sanity: flag any <h3> sections we didn't extract by name.
    known_h3 = {"screenshots", "release notes", "installation instructions",
                "downloads", "comments"}
    for h3 in tree.css("h3"):
        label = _text(h3).lower()
        if not label or label in known_h3:
            continue
        # Skip the screenshots H3 which contains an <a>; matched as exact text variant
        if label.startswith("screenshot"):
            continue
        section_html, section_text = _parse_h3_section(tree, _text(h3))
        raw["unparsed"].append({
            "section": _text(h3),
            "text": section_text[:4000],
            "html": section_html[:8000],
        })

    return ReleaseParse(
        product_slug=product_slug,
        release_slug=release_slug,
        url=url,
        title=title,
        subtitle=subtitle,
        description_html=desc_html,
        description_text=desc_text,
        info=info,
        info_links=info_links,
        available_releases=available,
        screenshots=shots,
        screenshot_gallery_url=gallery,
        release_notes_html=rn_html,
        release_notes_text=rn_text,
        installation_html=inst_html,
        installation_text=inst_text,
        serials=serials,
        downloads=downloads,
        comments=comments,
        page_meta=og,
        raw=raw,
    )


def parse_release_file(
    product_slug: str,
    release_slug: str,
    url: str,
    html_path: Path,
    out_path: Path,
) -> ReleaseParse:
    parse = parse_release_html(product_slug, release_slug, url, html_path.read_bytes())
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".part")
    tmp.write_text(json.dumps(asdict(parse), indent=2, sort_keys=True, default=str))
    tmp.replace(out_path)
    return parse
