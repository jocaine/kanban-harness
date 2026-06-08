"""Karpathy LLM Wiki engine for kanban_harness.

Implements the full Karpathy LLM Wiki pattern:
- raw/   <- immutable source material (LLM reads, never modifies)
- wiki/  <- compiled knowledge pages (LLM maintains, cross-references)
- Three operations: Ingest, Query (via prompt injection), Lint
- index.md as table-format global catalog
- log.md as append-only operation log
"""

import os
import re
import logging
from datetime import date as _date
from pathlib import Path

logger = logging.getLogger("kh.core.wiki")

_WIKI_BASE = os.getenv(
    "KH_WIKI_BASE",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data",
        "wikis",
    ),
)

_WIKI_TOKEN_BUDGET = int(os.getenv("KH_WIKI_TOKEN_BUDGET", "60000"))

_TOPIC_DIRS = ["research", "product", "arch"]

_TOPIC_DESCRIPTIONS = {
    "research": "调研结论、行业分析、技术对比",
    "product": "产品决策、已否决方向、用户偏好",
    "arch": "架构决策、技术栈、模块设计",
}


def get_wiki_base(project_id: int) -> Path:
    """Return wiki root directory for a project."""
    return Path(_WIKI_BASE) / f"project_{project_id}"


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Split YAML frontmatter from body."""
    m = re.match(r"^---\n(.*?)\n---\n?(.*)", content, re.DOTALL)
    if not m:
        return {}, content
    meta = {}
    for line in m.group(1).split("\n"):
        line = line.strip()
        if ":" in line:
            key, val = line.split(":", 1)
            val = val.strip()
            if val.startswith("[") and val.endswith("]"):
                val = [t.strip() for t in val[1:-1].split(",") if t.strip()]
            meta[key.strip()] = val
    return meta, m.group(2)


def _render_frontmatter(meta: dict) -> str:
    """Render a frontmatter dict to YAML block string."""
    lines = ["---"]
    for key, val in meta.items():
        if isinstance(val, list):
            lines.append(f"{key}: [{', '.join(val)}]")
        else:
            lines.append(f"{key}: {val}")
    lines.append("---")
    return "\n".join(lines)


def _update_frontmatter_date(content: str) -> str:
    """Update or add the updated field in YAML frontmatter."""
    today = _date.today().isoformat()
    m = re.match(r"^(---\n)(.*?)(\n---)", content, re.DOTALL)
    if m:
        fm = m.group(2)
        if re.search(r"^updated:", fm, re.MULTILINE):
            fm = re.sub(r"^updated:.*", f"updated: {today}", fm, flags=re.MULTILINE)
        else:
            fm += f"\nupdated: {today}"
        return content[:m.start(1)] + "---\n" + fm + "\n---" + content[m.end(3):]
    return f"---\nupdated: {today}\n---\n\n{content}"


def _extract_summary(body: str) -> str:
    """Extract first meaningful line from body as summary."""
    for line in body.strip().split("\n"):
        line = line.strip()
        if line.startswith("#"):
            continue
        if line and not line.startswith(">") and not line.startswith("|"):
            return line[:80]
    return "(no summary)"


def _validate_page_path(project_id: int, page_path: str) -> str:
    """Validate and normalize a wiki page path. Returns relative file path."""
    name = page_path.strip().removesuffix(".md")
    if not name:
        raise ValueError("page_path cannot be empty")
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+(/[a-zA-Z0-9_\-]+)?", name):
        raise ValueError(
            f"invalid page_path: {page_path!r}. "
            "Use 'subdir/filename' format (letters, digits, underscore, dash only)."
        )
    base = get_wiki_base(project_id)
    relative = name + ".md"
    resolved = (base / relative).resolve()
    base_resolved = base.resolve()
    if not str(resolved).startswith(str(base_resolved) + os.sep) and resolved != base_resolved:
        raise ValueError(f"page_path out of bounds: {page_path!r}")
    return relative


def _validate_raw_path(project_id: int, raw_path: str) -> str:
    """Validate a raw file path. Returns relative path under raw/."""
    name = raw_path.strip().removesuffix(".md")
    if not name:
        raise ValueError("raw_path cannot be empty")
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+/[a-zA-Z0-9_\-]+", name):
        raise ValueError(f"invalid raw_path: {raw_path!r}. Use 'topic/filename' format.")
    base = get_wiki_base(project_id)
    relative = f"raw/{name}.md"
    resolved = (base / relative).resolve()
    base_resolved = base.resolve()
    if not str(resolved).startswith(str(base_resolved) + os.sep):
        raise ValueError(f"raw_path out of bounds: {raw_path!r}")
    return relative


def _append_log(project_id: int, op_type: str, title: str, details: list[str] | None = None) -> None:
    """Append a structured entry to log.md."""
    base = get_wiki_base(project_id)
    log_path = base / "log.md"
    today = _date.today().isoformat()
    lines = [f"\n## [{today}] {op_type} | {title}"]
    if details:
        for d in details:
            lines.append(f"- {d}")
    entry = "\n".join(lines) + "\n"
    if log_path.is_file():
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(entry)
    else:
        header = f"---\ntype: meta\nupdated: {today}\ntags: [changelog]\n---\n\n# Log\n"
        log_path.write_text(header + entry, encoding="utf-8")


_SCHEMA_CONTENT = """---
type: meta
updated: {today}
tags: [schema, governance]
---

# SCHEMA — LLM Wiki

## Structure

raw/    <- immutable source material
wiki/   <- compiled knowledge (research/, product/, arch/)
index   <- table-format global catalog

## Wiki Topics

| Dir | Content | type |
|-----|---------|------|
| research/ | Research conclusions | research |
| product/ | Product decisions | product |
| arch/ | Architecture records | arch |

## Article Frontmatter

```yaml
---
type: research|arch|product
updated: YYYY-MM-DD
tags: [tag1, tag2]
sources: Author1, YYYY-MM-DD; Author2, YYYY-MM-DD
raw: [source](../../raw/topic/file.md)
source_card: KH-xxx
---
```

## Raw File Format

```markdown
# Title

> Source: URL or origin
> Collected: YYYY-MM-DD
> Published: YYYY-MM-DD or Unknown

Original content here.
```

## Rules

1. Ingest: raw -> wiki compilation
2. Writeback: every decision must go back to wiki
3. Wiki before RAG: < 80k tokens read directly
4. Index maintenance: rebuild after ingest/lint
5. Log append: record every operation
6. Cross-references: maintain See Also links
7. Conflict annotation: mark disagreements with source attribution
"""

_INDEX_TEMPLATE = """---
type: meta
updated: {today}
tags: [index]
---

# Knowledge Base Index

## research

{research_desc}

| Article | Summary | Updated |
|---------|---------|---------|

## product

{product_desc}

| Article | Summary | Updated |
|---------|---------|---------|

## arch

{arch_desc}

| Article | Summary | Updated |
|---------|---------|---------|
"""


def ensure_wiki_structure(project_id: int) -> Path:
    """Create wiki directory structure if needed."""
    base = get_wiki_base(project_id)
    if base.exists():
        (base / "raw").mkdir(exist_ok=True)
        return base
    base.mkdir(parents=True, exist_ok=True)
    for d in _TOPIC_DIRS:
        (base / d).mkdir(exist_ok=True)
    (base / "raw").mkdir(exist_ok=True)
    today = _date.today().isoformat()
    schema = _SCHEMA_CONTENT.replace("{today}", today)
    (base / "SCHEMA.md").write_text(schema, encoding="utf-8")
    index = _INDEX_TEMPLATE.format(
        today=today,
        research_desc=_TOPIC_DESCRIPTIONS["research"],
        product_desc=_TOPIC_DESCRIPTIONS["product"],
        arch_desc=_TOPIC_DESCRIPTIONS["arch"],
    )
    (base / "index.md").write_text(index, encoding="utf-8")
    log_content = (
        f"---\ntype: meta\nupdated: {today}\ntags: [changelog]\n---\n\n"
        f"# Log\n\n## [{today}] init | Wiki initialized\n"
    )
    (base / "log.md").write_text(log_content, encoding="utf-8")
    logger.info("[WIKI] 已初始化项目_%d 的 wiki 结构", project_id)
    return base


def write_wiki_page(project_id: int, page_path: str, content: str, log_message: str = "") -> str:
    """Write a wiki page. Returns the relative path written."""
    ensure_wiki_structure(project_id)
    relative = _validate_page_path(project_id, page_path)
    base = get_wiki_base(project_id)
    full_path = base / relative
    full_path.parent.mkdir(parents=True, exist_ok=True)
    updated_content = _update_frontmatter_date(content)
    full_path.write_text(updated_content, encoding="utf-8")
    msg = log_message or f"Updated {page_path}"
    _append_log(project_id, "write", msg)
    logger.info("[WIKI] 已写入 %s, 项目_%d", page_path, project_id)
    return relative


def read_wiki_page(project_id: int, page_path: str) -> str:
    """Read a wiki page. Returns empty string if not found."""
    try:
        relative = _validate_page_path(project_id, page_path)
    except ValueError:
        return ""
    base = get_wiki_base(project_id)
    full_path = base / relative
    if full_path.is_file():
        return full_path.read_text(encoding="utf-8")
    return ""


def list_wiki_pages(project_id: int, subdir: str | None = None) -> list[dict]:
    """List wiki pages with metadata."""
    base = get_wiki_base(project_id)
    if not base.exists():
        return []
    results = []
    scan_dirs = [subdir] if subdir else _TOPIC_DIRS
    for d in scan_dirs:
        dir_path = base / d
        if not dir_path.is_dir():
            continue
        for fname in sorted(dir_path.iterdir()):
            if not fname.name.endswith(".md"):
                continue
            content = fname.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(content)
            title_match = re.search(r"^#\s+(.+)", body, re.MULTILINE)
            title = title_match.group(1).strip() if title_match else fname.stem
            results.append({
                "page": f"{d}/{fname.stem}",
                "subdir": d,
                "title": title,
                "type": meta.get("type", ""),
                "updated": meta.get("updated", ""),
                "tags": meta.get("tags", []),
                "sources": meta.get("sources", ""),
                "raw": meta.get("raw", ""),
                "source_card": meta.get("source_card", ""),
                "summary": _extract_summary(body),
            })
    return results


def get_recent_pages(project_id: int, limit: int = 5) -> list[dict]:
    """Get the most recently updated wiki pages."""
    all_pages = list_wiki_pages(project_id)
    all_pages.sort(key=lambda p: p.get("updated", ""), reverse=True)
    return all_pages[:limit]


def write_raw(project_id: int, topic: str, slug: str, content: str,
              source_url: str = "", published: str = "Unknown") -> str:
    """Write a raw source file. Immutable - never overwrites."""
    ensure_wiki_structure(project_id)
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+", topic):
        raise ValueError(f"invalid topic: {topic!r}")
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+", slug):
        raise ValueError(f"invalid slug: {slug!r}")
    base = get_wiki_base(project_id)
    topic_dir = base / "raw" / topic
    topic_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{slug}.md"
    full_path = topic_dir / filename
    counter = 2
    while full_path.exists():
        filename = f"{slug}-{counter}.md"
        full_path = topic_dir / filename
        counter += 1
    today = _date.today().isoformat()
    has_title = bool(re.search(r"^#\s+", content, re.MULTILINE))
    has_meta = bool(re.search(r"^>\s*(Source|Collected):", content, re.MULTILINE))
    if not has_title and not has_meta:
        title_line = f"# {slug.replace('-', ' ').title()}\n\n"
        meta_lines = []
        if source_url:
            meta_lines.append(f"> Source: {source_url}")
        meta_lines.append(f"> Collected: {today}")
        meta_lines.append(f"> Published: {published}")
        content = title_line + "\n".join(meta_lines) + "\n\n" + content
    elif has_title and not has_meta:
        meta_lines = []
        if source_url:
            meta_lines.append(f"> Source: {source_url}")
        meta_lines.append(f"> Collected: {today}")
        meta_lines.append(f"> Published: {published}")
        idx = content.index("\n") if "\n" in content else len(content)
        content = content[:idx] + "\n\n" + "\n".join(meta_lines) + "\n" + content[idx:]
    full_path.write_text(content, encoding="utf-8")
    relative = f"raw/{topic}/{filename}"
    _append_log(project_id, "raw", f"Collected {topic}/{filename}")
    logger.info("[WIKI] 已写入原始文件 %s, 项目_%d", relative, project_id)
    return relative


def read_raw(project_id: int, raw_path: str) -> str:
    """Read a raw file. raw_path like 'topic/filename'."""
    try:
        relative = _validate_raw_path(project_id, raw_path)
    except ValueError:
        return ""
    base = get_wiki_base(project_id)
    full_path = base / relative
    if full_path.is_file():
        return full_path.read_text(encoding="utf-8")
    return ""


def list_raw(project_id: int, topic: str | None = None) -> list[dict]:
    """List raw files with metadata."""
    base = get_wiki_base(project_id)
    raw_dir = base / "raw"
    if not raw_dir.exists():
        return []
    results = []
    if topic:
        scan_dirs = [raw_dir / topic] if (raw_dir / topic).is_dir() else []
    else:
        scan_dirs = [d for d in sorted(raw_dir.iterdir()) if d.is_dir()]
    for topic_dir in scan_dirs:
        topic_name = topic_dir.name
        for fname in sorted(topic_dir.iterdir()):
            if not fname.name.endswith(".md"):
                continue
            content_head = fname.read_text(encoding="utf-8")[:512]
            title_match = re.search(r"^#\s+(.+)", content_head, re.MULTILINE)
            title = title_match.group(1).strip() if title_match else fname.stem
            source_match = re.search(r"^>\s*Source:\s*(.+)", content_head, re.MULTILINE)
            source = source_match.group(1).strip() if source_match else ""
            collected_match = re.search(r"^>\s*Collected:\s*(.+)", content_head, re.MULTILINE)
            collected = collected_match.group(1).strip() if collected_match else ""
            results.append({
                "path": f"{topic_name}/{fname.stem}",
                "topic": topic_name,
                "title": title,
                "source": source,
                "collected": collected,
                "size": fname.stat().st_size,
            })
    return results


def update_index(project_id: int) -> str:
    """Rebuild index.md from actual wiki pages."""
    ensure_wiki_structure(project_id)
    base = get_wiki_base(project_id)
    today = _date.today().isoformat()
    all_pages = list_wiki_pages(project_id)
    lines = [
        "---", "type: meta", f"updated: {today}", "tags: [index]", "---",
        "", "# Knowledge Base Index",
    ]
    for topic in _TOPIC_DIRS:
        topic_pages = [p for p in all_pages if p["subdir"] == topic]
        desc = _TOPIC_DESCRIPTIONS.get(topic, "")
        lines.append(f"\n## {topic}\n")
        lines.append(f"{desc}\n")
        lines.append("| Article | Summary | Updated |")
        lines.append("|---------|---------|---------|")
        if topic_pages:
            for p in sorted(topic_pages, key=lambda x: x.get("updated", ""), reverse=True):
                link = f"[{p['title']}]({p['page']}.md)"
                summary = p.get("summary", "(no summary)")
                updated = p.get("updated", "")
                lines.append(f"| {link} | {summary} | {updated} |")
        else:
            lines.append("| _(empty)_ | | |")
    content = "\n".join(lines) + "\n"
    (base / "index.md").write_text(content, encoding="utf-8")
    _append_log(project_id, "index", f"Rebuilt index ({len(all_pages)} articles)")
    return content


def prepare_ingest(project_id: int, raw_path: str) -> dict:
    """Prepare context for an ingest operation."""
    raw_content = read_raw(project_id, raw_path)
    if not raw_content:
        return {"error": f"Raw file not found: {raw_path}"}
    parts = raw_path.split("/")
    topic = parts[0] if len(parts) >= 2 else ""
    all_pages = list_wiki_pages(project_id)
    related = [p for p in all_pages if p["subdir"] == topic]
    base = get_wiki_base(project_id)
    index_path = base / "index.md"
    index_content = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""
    return {
        "raw_path": raw_path,
        "raw_content": raw_content,
        "related_pages": related,
        "index": index_content,
        "all_topics": [p["page"] for p in all_pages],
    }


def lint_wiki(project_id: int) -> dict:
    """Run lint checks on the wiki."""
    base = get_wiki_base(project_id)
    if not base.exists():
        return {"fixed": [], "reported": []}
    fixed = []
    reported = []
    all_pages = list_wiki_pages(project_id)
    page_files = {p["page"] for p in all_pages}

    # 1. Index consistency
    index_path = base / "index.md"
    if index_path.is_file():
        index_content = index_path.read_text(encoding="utf-8")
        index_links = set(re.findall(r"\[.*?\]\(([^)]+)\.md\)", index_content))
        for p in all_pages:
            if p["page"] not in index_links:
                fixed.append(f"index: added missing entry for {p['page']}")
        for link in index_links:
            if link not in page_files:
                reported.append(f"index: [{link}] points to nonexistent file")
        if fixed:
            update_index(project_id)

    # 2. Internal links
    link_pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    for p in all_pages:
        content = read_wiki_page(project_id, p["page"])
        if not content:
            continue
        _, body = _parse_frontmatter(content)
        for match in link_pattern.finditer(body):
            link_text, link_target = match.group(1), match.group(2)
            if link_target.startswith("http") or link_target.startswith("#"):
                continue
            if "../../raw/" in link_target:
                continue
            page_dir = base / p["subdir"]
            resolved = (page_dir / link_target).resolve()
            if not resolved.is_file():
                target_name = Path(link_target).stem + ".md"
                candidates = list(base.rglob(target_name))
                candidates = [c for c in candidates if "raw" not in str(c.relative_to(base))]
                if len(candidates) == 1:
                    new_rel = os.path.relpath(candidates[0], page_dir)
                    content = content.replace(f"]({link_target})", f"]({new_rel})")
                    (base / f"{p['page']}.md").write_text(content, encoding="utf-8")
                    fixed.append(f"{p['page']}: fixed link [{link_text}] -> {new_rel}")
                elif len(candidates) == 0:
                    reported.append(f"{p['page']}: broken link [{link_text}]({link_target})")
                else:
                    reported.append(f"{p['page']}: ambiguous link [{link_text}]({link_target})")

    # 3. Raw references
    for p in all_pages:
        content = read_wiki_page(project_id, p["page"])
        if not content:
            continue
        meta, _ = _parse_frontmatter(content)
        raw_field = meta.get("raw", "")
        if not raw_field or not isinstance(raw_field, str):
            continue
        raw_links = re.findall(r"\[.*?\]\(([^)]+)\)", raw_field)
        for raw_link in raw_links:
            page_dir = base / p["subdir"]
            resolved = (page_dir / raw_link).resolve()
            if not resolved.is_file():
                reported.append(f"{p['page']}: raw ref not found: {raw_link}")

    # 4. Orphan pages
    inbound: dict[str, int] = {p["page"]: 0 for p in all_pages}
    for p in all_pages:
        content = read_wiki_page(project_id, p["page"])
        if not content:
            continue
        _, body = _parse_frontmatter(content)
        for match in link_pattern.finditer(body):
            link_target = match.group(2)
            if link_target.startswith("http") or link_target.startswith("#"):
                continue
            target_stem = Path(link_target).stem
            for cp in page_files:
                if cp.endswith(f"/{target_stem}"):
                    inbound[cp] = inbound.get(cp, 0) + 1
                    break
    for page, count in inbound.items():
        if count == 0:
            reported.append(f"orphan: {page} has no inbound links")

    total = len(fixed) + len(reported)
    _append_log(project_id, "lint", f"{total} issues found, {len(fixed)} auto-fixed")
    return {"fixed": fixed, "reported": reported}


def get_wiki_for_prompt(project_id: int) -> str:
    """Build wiki context for agent prompt injection. Karpathy rule 3."""
    base = get_wiki_base(project_id)
    if not base.exists():
        return ""
    index_path = base / "index.md"
    index_content = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""
    all_pages = list_wiki_pages(project_id)
    if not all_pages and not index_content:
        return ""
    bodies = []
    total_len = 0
    for p in all_pages:
        body = read_wiki_page(project_id, p["page"])
        if body:
            bodies.append((p, body))
            total_len += len(body)
    sections = ["\n## Wiki 知识库\n"]
    if total_len <= _WIKI_TOKEN_BUDGET:
        if index_content:
            sections.append("### 目录\n")
            _, index_body = _parse_frontmatter(index_content)
            sections.append(index_body.strip())
        for p, body in bodies:
            _, page_body = _parse_frontmatter(body)
            sections.append(f"\n### {p['title']} ({p['page']})\n")
            sections.append(page_body.strip())
    else:
        if index_content:
            sections.append("### 目录\n")
            _, index_body = _parse_frontmatter(index_content)
            sections.append(index_body.strip())
        sections.append(
            f"\n> Wiki 内容已超出注入预算({total_len}/{_WIKI_TOKEN_BUDGET}字符)，"
            "仅注入目录。Agent 可通过工具按需读取具体页面。"
        )
    return "\n".join(sections)
