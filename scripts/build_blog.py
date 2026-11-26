#!/usr/bin/env python3
"""把 docs/ 子模块中的 Markdown 同步为站内博客。

用法：
    python3 scripts/build_blog.py

依赖：
    pip install -r scripts/requirements.txt

行为：
    - 读取 docs/ 下所有 .md（跳过 AGENTS.md），生成 blog/ 静态页面；
    - README.md 作为所在目录的概览页，根目录 README.md 作为博客首页介绍；
    - 按 docs/ 的目录层级生成侧边栏导航，保留相对链接与本地图片；
    - blog/ 为完全生成目录，每次执行都会重建。
"""

from __future__ import annotations

import html
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

try:
    import markdown
except ImportError:  # pragma: no cover - 仅用于给出友好提示
    sys.exit("缺少依赖 markdown，请先运行：pip install -r scripts/requirements.txt")

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
OUT = ROOT / "blog"
SITE_TITLE = "逐梦笔记"
SYNC_DATE = date.today().isoformat()
EXCLUDED_FILES = {"AGENTS.md"}
IMAGE_SUFFIXES = {".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif"}


@dataclass
class Post:
    src_rel: Path          # 相对 docs/
    out_rel: Path          # 相对 blog/
    title: str
    excerpt: str
    body: str              # 已转换并修复链接的 HTML
    has_math: bool
    is_readme: bool
    words: int


@dataclass
class Folder:
    name: str
    rel: Path
    readme: Post | None = None
    posts: list[Post] = field(default_factory=list)
    children: dict[str, "Folder"] = field(default_factory=dict)

    def note_count(self) -> int:
        total = len(self.posts)
        for child in self.children.values():
            total += child.note_count()
        return total


# ---------------------------------------------------------------- Markdown


def slugify_zh(value: str, separator: str) -> str:
    """保留中文的标题锚点，避免 toc 生成空 id。"""
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"[^\w\u4e00-\u9fff]+", separator, value, flags=re.UNICODE)
    return value.strip(separator).lower()


def protect_math(text: str) -> tuple[str, list[str]]:
    """把公式替换成占位符，防止 Markdown 改坏 LaTeX。"""
    store: list[str] = []

    def stash(raw: str) -> str:
        store.append(raw)
        return f"@@MATH{len(store) - 1}@@"

    blocks = re.split(r"(```[\s\S]*?```|~~~[\s\S]*?~~~)", text)
    out: list[str] = []
    for block in blocks:
        if block.startswith("```") or block.startswith("~~~"):
            out.append(block)
            continue
        block = re.sub(r"\$\$(.+?)\$\$", lambda m: stash(m.group(0)), block, flags=re.S)
        block = re.sub(r"\\\[(.+?)\\\]", lambda m: stash(m.group(0)), block, flags=re.S)
        block = re.sub(r"\\\((.+?)\\\)", lambda m: stash(m.group(0)), block, flags=re.S)
        out.append(block)
    return "".join(out), store


def restore_math(text: str, store: list[str]) -> str:
    for i, raw in enumerate(store):
        text = text.replace(f"@@MATH{i}@@", html.escape(raw, quote=False))
    return text


def strip_front_matter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4 :].lstrip("\n")
    return text


def split_title(text: str, fallback: str) -> tuple[str, str]:
    """取出第一个 H1 作为页面标题，并从正文中移除。"""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("# "):
            title = re.sub(r"[*_`]", "", line[2:].strip()).rstrip("#").strip()
            body = "\n".join(lines[:i] + lines[i + 1 :])
            return title or fallback, body
    return fallback, text


def plain_text(md: str) -> str:
    text = re.sub(r"```[\s\S]*?```", " ", md)
    text = re.sub(r"~~~[\s\S]*?~~~", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[#>*_`~|=-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def make_excerpt(md: str) -> str:
    body = re.sub(r"^#\s+.*$", "", md, count=1, flags=re.M)
    for block in re.split(r"\n\s*\n", body):
        cleaned = plain_text(block)
        if cleaned:
            return cleaned[:120] + ("…" if len(cleaned) > 120 else "")
    return ""


def count_words(md: str) -> int:
    text = plain_text(md)
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    words = len(re.findall(r"[A-Za-z0-9_]+", text))
    return cjk + words


def task_lists(text: str) -> str:
    text = re.sub(r"^(\s*)[-*] \[[xX]\] ", r"\1- ☑ ", text, flags=re.M)
    text = re.sub(r"^(\s*)[-*] \[ \] ", r"\1- ☐ ", text, flags=re.M)
    return text


# ---------------------------------------------------------------- 路径与链接


def out_for(src_rel: Path) -> Path:
    if src_rel.name.lower() == "readme.md":
        folder = src_rel.parent
        return (folder / "index.html") if folder != Path(".") else Path("index.html")
    return src_rel.with_suffix(".html")


def url_between(from_out: Path, to_out: Path) -> str:
    rel = os.path.relpath(OUT / to_out, (OUT / from_out).parent).replace(os.sep, "/")
    return quote(rel, safe="/")


def site_url(from_out: Path, target: str) -> str:
    rel = os.path.relpath(ROOT / target, (OUT / from_out).parent).replace(os.sep, "/")
    return quote(rel, safe="/")


def collect_posts() -> tuple[list[Post], dict[Path, Post], str]:
    """读取并渲染所有 Markdown，返回文章列表、源路径索引与首页介绍 HTML。"""
    md_files = sorted(
        p for p in DOCS.rglob("*.md")
        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(DOCS).parts)
    )
    if not md_files:
        sys.exit("docs/ 中没有找到 Markdown，请先运行：git submodule update --init --recursive")

    posts: list[Post] = []
    by_src: dict[Path, Post] = {}
    intro_html = ""
    used_out: set[Path] = set()
    warnings: list[str] = []
    pending: list[tuple[Path, str]] = []

    for path in md_files:
        src_rel = path.relative_to(DOCS)
        raw = strip_front_matter(path.read_text(encoding="utf-8-sig", errors="replace"))
        if src_rel == Path("README.md"):
            _, intro_body = split_title(raw, SITE_TITLE)
            protected, store = protect_math(task_lists(intro_body))
            converter = markdown.Markdown(extensions=["extra", "toc"], extension_configs={"toc": {"slugify": slugify_zh}})
            intro_html = restore_math(converter.convert(protected), store)
            continue
        if src_rel.name in EXCLUDED_FILES:
            continue
        pending.append((src_rel, raw))

    # 先建立输出映射，链接重写时才能解析到目标页
    out_map: dict[Path, Path] = {}
    for src_rel, _ in pending:
        target = out_for(src_rel)
        if target in used_out:
            stem = target.stem
            n = 2
            while target.with_name(f"{stem}-{n}{target.suffix}") in used_out:
                n += 1
            target = target.with_name(f"{stem}-{n}{target.suffix}")
        used_out.add(target)
        out_map[src_rel] = target

    # 第一轮只登记标题等元信息，保证链接重写时所有目标页面都已存在
    for src_rel, raw in pending:
        title, body = split_title(raw, src_rel.stem)
        posts.append(
            Post(
                src_rel=src_rel,
                out_rel=out_map[src_rel],
                title=title,
                excerpt=make_excerpt(body),
                body="",
                has_math=bool(re.search(r"\$\$|\\\(|\\\[", body)),
                is_readme=src_rel.name.lower() == "readme.md",
                words=count_words(body),
            )
        )
        by_src[src_rel] = posts[-1]

    # 第二轮渲染正文并重写链接
    for post, (_, raw) in zip(posts, pending):
        _, body = split_title(raw, post.src_rel.stem)
        body = task_lists(body)
        protected, store = protect_math(body)
        converter = markdown.Markdown(
            extensions=["extra", "toc", "sane_lists"],
            extension_configs={"toc": {"slugify": slugify_zh}},
        )
        rendered = restore_math(converter.convert(protected), store)
        rendered = re.sub(r"<table>([\s\S]*?)</table>", r'<div class="table-wrap"><table>\1</table></div>', rendered)
        post.body = rewrite_links(rendered, post.src_rel, post.out_rel, by_src, warnings)

    if warnings:
        print(f"  ! {len(warnings)} 个本地引用未能解析（原文中的缺失链接或图片）：")
        for item in warnings[:10]:
            print(f"    - {item}")
        if len(warnings) > 10:
            print(f"    - 其余 {len(warnings) - 10} 条已跳过")
    return posts, by_src, intro_html


def rewrite_links(
    rendered: str,
    src_rel: Path,
    out_rel: Path,
    by_src: dict[Path, Post],
    warnings: list[str],
) -> str:
    """把 .md 链接改写到博客页面，并按需复制其它本地资源。"""

    def map_url(url: str) -> str:
        if not url or url.startswith(("http://", "https://", "mailto:", "data:", "javascript:", "#")):
            return url
        parsed = urlsplit(url)
        if not parsed.path:
            return url
        raw_path = unquote(parsed.path)
        target = (DOCS / src_rel.parent / raw_path).resolve()
        try:
            target.relative_to(DOCS.resolve())
        except ValueError:
            return url

        if target.suffix.lower() == ".md":
            try:
                target_rel = target.relative_to(DOCS.resolve())
            except ValueError:
                return url
            post = by_src.get(target_rel)
            if post is None:  # 大小写差异兜底
                lowered = str(target_rel).lower()
                for key, value in by_src.items():
                    if str(key).lower() == lowered:
                        post = value
                        break
            if post is None:
                warnings.append(f"{src_rel} → {raw_path}")
                return url
            new_url = url_between(out_rel, post.out_rel)
            return f"{new_url}#{parsed.fragment}" if parsed.fragment else new_url

        # 指向目录的链接：落到该目录的 README，否则落到目录内第一篇笔记
        if target.is_dir():
            try:
                target_rel = target.relative_to(DOCS.resolve())
            except ValueError:
                return url
            candidate = next(
                (p for key, p in by_src.items() if key.parent == target_rel and p.is_readme),
                None,
            )
            if candidate is None:
                candidate = next(
                    (p for key, p in sorted(by_src.items()) if target_rel in key.parents),
                    None,
                )
            if candidate:
                new_url = url_between(out_rel, candidate.out_rel)
                return f"{new_url}#{parsed.fragment}" if parsed.fragment else new_url
            return url

        if target.is_file():
            dest = OUT / target.relative_to(DOCS.resolve())
            if not dest.exists() and target.suffix.lower() not in IMAGE_SUFFIXES:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, dest)
        elif target.suffix.lower() in IMAGE_SUFFIXES:
            warnings.append(f"{src_rel} → 缺失图片 {raw_path}")
        return url

    rendered = re.sub(r'(<a\s[^>]*?href=")([^"]+)(")', lambda m: m.group(1) + map_url(m.group(2)) + m.group(3), rendered, flags=re.I)
    rendered = re.sub(r'(<img\s[^>]*?src=")([^"]+)(")', lambda m: m.group(1) + map_url(m.group(2)) + m.group(3), rendered, flags=re.I)
    rendered = re.sub(r"<img(?![^>]*\bloading=)", '<img loading="lazy" decoding="async"', rendered, flags=re.I)
    return rendered


# ---------------------------------------------------------------- 目录树


def build_tree(posts: list[Post]) -> Folder:
    root = Folder("", Path("."))
    for post in posts:
        node = root
        for part in post.src_rel.parent.parts:
            node = node.children.setdefault(part, Folder(part, node.rel / part))
        if post.is_readme:
            node.readme = post
        else:
            node.posts.append(post)

    def sort_folder(folder: Folder) -> None:
        folder.posts.sort(key=lambda p: p.title)
        folder.children = dict(sorted(folder.children.items(), key=lambda kv: kv[0]))
        for child in folder.children.values():
            sort_folder(child)

    sort_folder(root)
    return root


def walk_posts(folder: Folder) -> list[Post]:
    ordered: list[Post] = []
    if folder.readme:
        ordered.append(folder.readme)
    ordered.extend(folder.posts)
    for child in folder.children.values():
        ordered.extend(walk_posts(child))
    return ordered


def folder_path_label(folder: Folder) -> str:
    return str(folder.rel).replace(os.sep, " / ")


def render_tree(folder: Folder, current: Post | None) -> str:
    current_out = current.out_rel if current else None
    parts: list[str] = []

    for child in folder.children.values():
        is_open = bool(
            current_out
            and current_out.parts[: len(child.rel.parts)] == child.rel.parts
        )
        count = child.note_count()
        summary = (
            f'<summary><span class="tree-caret"></span>'
            f'<span class="tree-name">{html.escape(child.name)}</span>'
            f'<span class="tree-count">{count}</span></summary>'
        )
        children = render_tree(child, current)
        parts.append(
            f'<details class="tree-folder"{" open" if is_open else ""}>{summary}'
            f'<div class="tree-children">{children}</div></details>'
        )

    if folder.posts:
        if folder.rel == Path("."):
            parts.append('<div class="tree-label">散篇</div>')
        for post in folder.posts:
            active = " active" if current_out == post.out_rel else ""
            href = url_between(current_out, post.out_rel) if current_out else quote(str(post.out_rel).replace(os.sep, "/"))
            parts.append(
                f'<a class="tree-link{active}" href="{href}" title="{html.escape(post.title)}">'
                f'{html.escape(post.title)}</a>'
            )

    return "".join(parts)


def render_sidebar(root: Folder, current: Post | None, from_out: Path, total: int) -> str:
    home_href = url_between(from_out, Path("index.html"))
    return f"""
    <aside class="blog-side">
      <div class="side-head">
        <a class="blog-brand" href="{home_href}">{SITE_TITLE}<span>NOTES</span></a>
        <button class="side-toggle" id="sideToggle" aria-expanded="false" aria-controls="sideBody">目录</button>
      </div>
      <div class="side-body" id="sideBody">
        <div class="tree-search">
          <input id="treeQ" type="search" placeholder="筛选 {total} 篇笔记…" aria-label="筛选笔记">
        </div>
        <nav class="tree" aria-label="博客目录">{render_tree(root, current)}</nav>
      </div>
    </aside>"""


# ---------------------------------------------------------------- 页面模板


def site_nav(from_out: Path) -> str:
    home = site_url(from_out, "index.html")
    knowledge = site_url(from_out, "knowledge.html")
    career = site_url(from_out, "career.html")
    blog = site_url(from_out, "blog/index.html")
    articles = site_url(from_out, "articles/index.html")
    return f"""
<nav class="site-nav">
  <div class="nav-inner">
    <a class="brand" href="{home}"><span class="mark"></span>shiqianzhi<em>知识地图</em></a>
    <button class="menu-btn" id="menuBtn" aria-label="展开导航" aria-expanded="false" aria-controls="navLinks">
      <span></span><span></span><span></span>
    </button>
    <div class="nav-links" id="navLinks">
      <a href="{home}">首页</a>
      <div class="nav-drop">
        <a href="{knowledge}">世界知识体系 <span class="caret"></span></a>
        <div class="drop-panel">
          <a href="{site_url(from_out, 'knowledge.html')}#meta">元认知层 · 我们如何知道</a>
          <a href="{site_url(from_out, 'knowledge.html')}#domains">五大知识域</a>
          <a href="{site_url(from_out, 'knowledge.html')}#cross">交叉与前沿</a>
          <a href="{site_url(from_out, 'knowledge.html')}#axes">三条贯穿轴</a>
          <a href="{site_url(from_out, 'knowledge.html')}#finder">问题定位器</a>
        </div>
      </div>
      <a href="{career}">世界职业体系</a>
      <a href="{blog}" class="active">博客</a>
      <a href="{articles}">文章专栏</a>
      <a class="nav-cta" href="{knowledge}">进入图谱</a>
    </div>
  </div>
</nav>"""


def page_shell(
    from_out: Path,
    title: str,
    description: str,
    body: str,
    has_math: bool = False,
) -> str:
    css_href = url_between(from_out, Path("blog.css"))
    js_href = url_between(from_out, Path("blog.js"))
    code_css = site_url(from_out, "assets/code-highlight.css")
    scroll_css = site_url(from_out, "assets/scrollbar.css")
    hljs_js = site_url(from_out, "assets/highlight.min.js")
    code_js = site_url(from_out, "assets/code-highlight.js")
    math = ""
    if has_math:
        math = """
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js"></script>"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title if title == SITE_TITLE else f"{title} · {SITE_TITLE}")}</title>
<meta name="description" content="{html.escape(description, quote=True)}">
<link rel="preconnect" href="https://miaoda.feishu.cn">
<link rel="stylesheet" href="https://miaoda.feishu.cn/fonts/css2?family=Noto+Serif+SC:wght@400;600;700;900&family=Noto+Sans+SC:wght@300;400;500;700&display=swap">
<link rel="stylesheet" href="{css_href}">
<link rel="stylesheet" href="{code_css}">
<link rel="stylesheet" href="{scroll_css}">{math}
</head>
<body>
{site_nav(from_out)}
{body}
<footer class="blog-foot">
  <div class="wrap foot-inner">
    <span>© 2026 {SITE_TITLE} · 由 <code>scripts/build_blog.py</code> 从 docs/ 自动生成</span>
    <span>内容来源：<a href="https://github.com/ShiQianzhi/note" target="_blank" rel="noopener noreferrer">ShiQianzhi/note</a> · 同步于 {SYNC_DATE}</span>
  </div>
</footer>
<script src="{js_href}"></script>
<script src="{hljs_js}" defer></script>
<script src="{code_js}" defer></script>
</body>
</html>
"""


def render_post(post: Post, root: Folder, ordered: list[Post], total: int) -> str:
    index = ordered.index(post)
    prev_post = ordered[index - 1] if index > 0 else None
    next_post = ordered[index + 1] if index < len(ordered) - 1 else None
    sidebar = render_sidebar(root, post, post.out_rel, total)

    crumbs = [f'<a href="{url_between(post.out_rel, Path("index.html"))}">博客</a>']
    acc = Path(".")
    for part in post.src_rel.parent.parts:
        acc = acc / part
        folder = find_folder(root, acc)
        label = html.escape(part)
        is_current = post.is_readme and acc == post.src_rel.parent
        if folder and folder.readme and not is_current:
            crumb_href = url_between(post.out_rel, folder.readme.out_rel)
            crumbs.append(f'<a href="{crumb_href}">{label}</a>')
        else:
            crumbs.append(f"<span>{label}</span>")
    if not post.is_readme:
        crumbs.append(f"<span>{html.escape(post.title)}</span>")

    pager = ""
    if prev_post or next_post:
        left = (
            f'<a class="pager-link" href="{url_between(post.out_rel, prev_post.out_rel)}">'
            f'<em>上一篇</em><b>{html.escape(prev_post.title)}</b></a>'
            if prev_post else '<span class="pager-link empty"></span>'
        )
        right = (
            f'<a class="pager-link next" href="{url_between(post.out_rel, next_post.out_rel)}">'
            f'<em>下一篇</em><b>{html.escape(next_post.title)}</b></a>'
            if next_post else '<span class="pager-link empty"></span>'
        )
        pager = f'<nav class="post-pager" aria-label="上下篇">{left}{right}</nav>'

    meta = "博客" if post.src_rel.parent == Path(".") else folder_path_label(find_folder(root, post.src_rel.parent) or root)
    word_meta = f"约 {post.words:,} 字" if post.words else ""
    body = f"""
<div class="blog-wrap">
  {sidebar}
  <main class="blog-main">
    <nav class="breadcrumbs" aria-label="面包屑">{"<span>/</span>".join(crumbs)}</nav>
    <article class="post">
      <header class="post-head">
        <div class="post-kicker">{meta}</div>
        <h1>{html.escape(post.title)}</h1>
        <div class="post-meta"><span>{html.escape(str(post.src_rel))}</span>{f"<span>{word_meta}</span>" if word_meta else ""}</div>
      </header>
      <div class="prose">{post.body}</div>
      {pager}
    </article>
  </main>
</div>"""
    return page_shell(
        post.out_rel,
        post.title,
        post.excerpt or f"{SITE_TITLE} · {post.title}",
        body,
        post.has_math,
    )


def find_folder(root: Folder, rel: Path) -> Folder | None:
    node = root
    for part in rel.parts:
        if part in (".", ""):
            continue
        node = node.children.get(part)
        if node is None:
            return None
    return node


def render_home(root: Folder, ordered: list[Post], intro_html: str, total: int) -> str:
    out_rel = Path("index.html")
    sidebar = render_sidebar(root, None, out_rel, total)
    folder_total = len(root.children)

    categories = []
    for child in sorted(root.children.values(), key=lambda f: (-f.note_count(), f.name)):
        link_post = child.readme or (walk_posts(child)[0] if walk_posts(child) else None)
        if not link_post:
            continue
        desc = child.readme.excerpt if child.readme and child.readme.excerpt else f"收录 {child.note_count()} 篇笔记"
        go_text = "进入目录" if child.readme else "开始阅读"
        categories.append(
            f'<a class="cat-card" href="{url_between(out_rel, link_post.out_rel)}">'
            f'<div class="cat-top"><span class="cat-dot"></span><span class="cat-count">{child.note_count()} 篇</span></div>'
            f'<h3>{html.escape(child.name)}</h3>'
            f'<p>{html.escape(desc)}</p>'
            f'<span class="cat-go">{go_text} →</span></a>'
        )

    root_notes = "".join(
        f'<a class="note-row" href="{url_between(out_rel, p.out_rel)}">'
        f'<b>{html.escape(p.title)}</b><span>{html.escape(p.excerpt)}</span></a>'
        for p in root.posts
    )

    body = f"""
<header class="blog-hero">
  <div class="wrap">
    <div class="kicker"><i></i>BLOG · {SITE_TITLE}</div>
    <h1>逐梦笔记</h1>
    <p class="lede">{intro_html or "记录学习、思考与生活。"}</p>
    <div class="hero-stats">
      <div><b>{total}</b><span>篇笔记</span></div>
      <div><b>{folder_total}</b><span>个一级目录</span></div>
      <div><b>{SYNC_DATE}</b><span>最近同步</span></div>
    </div>
  </div>
</header>
<div class="blog-wrap">
  {sidebar}
  <main class="blog-main">
    <div class="home-head">
      <div class="sec-label">按目录浏览</div>
      <p>站点导航由 docs/ 的层级自动生成；左侧可展开全部目录，也可以直接筛选标题。</p>
    </div>
    <div class="cat-grid">{"".join(categories)}</div>
    {f'<div class="home-head"><div class="sec-label">散篇</div></div><div class="note-list">{root_notes}</div>' if root_notes else ""}
  </main>
</div>"""
    return page_shell(
        out_rel,
        SITE_TITLE,
        "逐梦笔记：按目录层级整理的学习笔记与随笔。",
        body,
    )


# ---------------------------------------------------------------- 静态资源


def write_assets() -> None:
    (OUT / "blog.css").write_text(BLOG_CSS, encoding="utf-8")
    (OUT / "blog.js").write_text(BLOG_JS, encoding="utf-8")


def copy_images() -> int:
    count = 0
    for path in DOCS.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.suffix.lower() in IMAGE_SUFFIXES:
            dest = OUT / path.relative_to(DOCS)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            count += 1
    return count


BLOG_CSS = r"""
:root{
  --bg:#0a0e16;--panel:rgba(255,255,255,.045);--line:rgba(255,255,255,.11);--line-soft:rgba(255,255,255,.06);
  --text:#e8ecf4;--dim:#9aa4b8;--dim2:#6d778c;--gold:#d9b25a;
  --serif:"Noto Serif SC",Songti SC,SimSun,serif;
  --sans:"Noto Sans SC","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--text);font-family:var(--sans);line-height:1.75;-webkit-font-smoothing:antialiased;overflow-x:hidden;padding-top:62px}
body::before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
  background:radial-gradient(900px 620px at 8% -10%,rgba(124,156,255,.16),transparent 62%),radial-gradient(760px 520px at 96% 2%,rgba(217,178,90,.1),transparent 60%),radial-gradient(900px 700px at 46% 104%,rgba(79,209,197,.08),transparent 62%)}
body::after{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;opacity:.45;
  background-image:linear-gradient(rgba(255,255,255,.033) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.033) 1px,transparent 1px);
  background-size:76px 76px;mask-image:radial-gradient(circle at 50% 20%,#000 26%,transparent 82%)}
.wrap{max-width:1180px;margin:0 auto;padding:0 26px}
a{color:inherit}

/* 站点导航 */
.site-nav{position:fixed;top:0;left:0;right:0;z-index:100;background:rgba(8,11,18,.74);backdrop-filter:blur(18px) saturate(140%);-webkit-backdrop-filter:blur(18px) saturate(140%);border-bottom:1px solid rgba(255,255,255,.07)}
.nav-inner{max-width:1180px;margin:0 auto;padding:0 26px;height:62px;display:flex;align-items:center;gap:22px}
.brand{display:flex;align-items:center;gap:10px;text-decoration:none;color:#eef2f9;font-family:var(--serif);font-size:15px;letter-spacing:.16em;white-space:nowrap}
.brand .mark{width:18px;height:18px;position:relative;flex:none}
.brand .mark::before{content:"";position:absolute;inset:0;border:1px solid rgba(217,178,90,.7);border-radius:2px;transform:rotate(45deg)}
.brand .mark::after{content:"";position:absolute;inset:6px;background:var(--gold);border-radius:1px}
.brand em{font-style:normal;font-family:var(--sans);font-size:11px;letter-spacing:.24em;color:var(--dim2);border-left:1px solid rgba(255,255,255,.14);margin-left:4px;padding-left:12px}
.nav-links{display:flex;align-items:center;gap:4px;margin-left:auto}
.nav-links > a,.nav-drop > a{position:relative;display:flex;align-items:center;gap:6px;padding:8px 13px;border-radius:100px;font-size:13.5px;color:#aab4c6;text-decoration:none;transition:.18s}
.nav-links > a:hover,.nav-drop > a:hover{color:#fff;background:rgba(255,255,255,.06)}
.nav-links > a.active,.nav-drop > a.active{color:#f2dda9;background:rgba(217,178,90,.12)}
.caret{width:6px;height:6px;border-right:1px solid currentColor;border-bottom:1px solid currentColor;transform:rotate(45deg) translateY(-2px);opacity:.65;transition:.2s}
.nav-drop{position:relative}
.nav-drop:hover .caret{transform:rotate(225deg) translateY(-2px)}
.drop-panel{position:absolute;top:calc(100% + 12px);left:0;width:240px;padding:8px;border-radius:14px;background:rgba(14,19,29,.97);border:1px solid rgba(255,255,255,.1);box-shadow:0 24px 60px -20px #000;opacity:0;visibility:hidden;transform:translateY(-6px);transition:.2s}
.drop-panel::before{content:"";position:absolute;left:0;right:0;top:-12px;height:12px}
.nav-drop:hover .drop-panel,.nav-drop:focus-within .drop-panel{opacity:1;visibility:visible;transform:none}
.drop-panel a{display:block;padding:9px 12px;border-radius:8px;font-size:13px;color:#b9c3d5;text-decoration:none;transition:.15s}
.drop-panel a:hover{background:rgba(255,255,255,.07);color:#fff}
.nav-cta{margin-left:8px;border:1px solid rgba(217,178,90,.42);color:#f0d9a4 !important;background:rgba(217,178,90,.08)}
.nav-cta:hover{background:rgba(217,178,90,.18) !important;border-color:rgba(217,178,90,.7)}
.menu-btn{display:none;flex-direction:column;justify-content:center;gap:5px;width:38px;height:34px;margin-left:auto;padding:0 6px;background:none;border:0;cursor:pointer}
.menu-btn span{display:block;height:1.5px;background:#cbd4e4;border-radius:2px;transition:.22s}
.menu-btn.open span:nth-child(1){transform:translateY(6.5px) rotate(45deg)}
.menu-btn.open span:nth-child(2){opacity:0}
.menu-btn.open span:nth-child(3){transform:translateY(-6.5px) rotate(-45deg)}

/* 博客布局 */
.blog-wrap{max-width:1280px;margin:0 auto;padding:34px 26px 80px;display:grid;grid-template-columns:266px minmax(0,1fr);gap:48px;align-items:start}
.blog-side{position:sticky;top:84px;max-height:calc(100vh - 106px);overflow-y:auto;overscroll-behavior:contain;padding-right:8px}
.blog-brand{display:flex;align-items:baseline;gap:10px;text-decoration:none;color:#f0d9a4;font-family:var(--serif);font-size:17px;letter-spacing:.1em}
.blog-brand span{font-family:var(--sans);font-size:9px;letter-spacing:.26em;color:var(--dim2)}
.side-head{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:0 4px 12px;border-bottom:1px solid var(--line-soft)}
.side-toggle{display:none;align-items:center;justify-content:center;font-family:var(--sans);font-size:12px;color:#f0d9a4;padding:5px 12px;border-radius:100px;border:1px solid rgba(217,178,90,.35);background:rgba(217,178,90,.08);cursor:pointer}
.side-body{padding-top:2px}
.tree-search{margin:14px 0 12px}
.tree-search input{width:100%;background:rgba(255,255,255,.05);border:1px solid var(--line-soft);border-radius:100px;padding:8px 14px;color:var(--text);font-family:var(--sans);font-size:12.5px;outline:none;transition:.2s}
.tree-search input:focus{border-color:rgba(217,178,90,.5);background:rgba(255,255,255,.08)}
.tree-search input::placeholder{color:var(--dim2)}
.tree{font-size:13px}
.tree-folder{margin:1px 0}
.tree-folder > summary{display:flex;align-items:center;gap:7px;padding:7px 8px;border-radius:8px;cursor:pointer;list-style:none;color:#c3ccdb;transition:.16s}
.tree-folder > summary::-webkit-details-marker{display:none}
.tree-folder > summary:hover{background:rgba(255,255,255,.05);color:#fff}
.tree-caret{width:0;height:0;border-left:4px solid var(--dim2);border-top:3.5px solid transparent;border-bottom:3.5px solid transparent;transition:.2s;flex:none}
.tree-folder[open] > summary .tree-caret{transform:rotate(90deg)}
.tree-name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tree-count{font-size:10.5px;color:var(--dim2);background:rgba(255,255,255,.045);border-radius:100px;padding:0 7px;line-height:1.7}
.tree-children{margin-left:9px;padding-left:11px;border-left:1px solid rgba(255,255,255,.07)}
.tree-label{font-size:10px;letter-spacing:.2em;color:var(--dim2);margin:14px 8px 6px}
.tree-link{display:flex;gap:7px;align-items:flex-start;padding:6px 8px;border-radius:7px;color:#a9b4c6;text-decoration:none;line-height:1.5;transition:.16s}
.tree-link::before{content:"";width:4px;height:4px;border-radius:50%;background:rgba(217,178,90,.45);margin-top:8px;flex:none}
.tree-link:hover{background:rgba(255,255,255,.055);color:#fff}
.tree-link.active{background:rgba(217,178,90,.12);color:#f0d9a4}
.tree-link.hidden,.tree-folder.hidden{display:none}
.blog-main{min-width:0;max-width:860px}

/* 面包屑与文章 */
.breadcrumbs{display:flex;flex-wrap:wrap;align-items:center;gap:8px;font-size:12px;color:var(--dim2);margin-bottom:16px}
.breadcrumbs a{color:var(--dim);text-decoration:none}
.breadcrumbs a:hover{color:var(--gold)}
.breadcrumbs > span:last-child{color:#b7c1d2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%}
.post-head{padding-bottom:22px;border-bottom:1px solid var(--line-soft);margin-bottom:26px}
.post-kicker{font-size:11px;letter-spacing:.18em;color:var(--gold);text-transform:uppercase}
.post-head h1{font-family:var(--serif);font-size:clamp(29px,4vw,44px);line-height:1.3;font-weight:700;letter-spacing:.02em;margin-top:14px}
.post-meta{display:flex;flex-wrap:wrap;gap:8px 18px;margin-top:14px;font-size:11.5px;color:var(--dim2);letter-spacing:.04em}

/* 正文排版 */
.prose{font-size:15.5px;line-height:1.95;color:#c3cbd9;overflow-wrap:break-word}
.prose h2{font-family:var(--serif);font-size:clamp(21px,2.4vw,27px);font-weight:700;color:#eef2fa;letter-spacing:.03em;margin:46px 0 16px;padding-top:18px;border-top:1px solid var(--line-soft);scroll-margin-top:90px}
.prose h3{font-family:var(--serif);font-size:20px;font-weight:600;color:#e6ecf7;margin:32px 0 12px;scroll-margin-top:90px}
.prose h4{font-size:16px;color:#dfe6f2;margin:24px 0 10px}
.prose h5,.prose h6{font-size:14.5px;color:#cfd8e7;margin:20px 0 8px}
.prose p{margin:13px 0}
.prose a{color:#e2c484;text-decoration:none;border-bottom:1px solid rgba(217,178,90,.32);transition:.16s}
.prose a:hover{color:#fff;border-color:var(--gold)}
.prose strong{color:#eef2fa;font-weight:700}
.prose em{color:#d6dfee}
.prose ul,.prose ol{margin:13px 0;padding-left:1.55em}
.prose li{margin:6px 0}
.prose li::marker{color:rgba(217,178,90,.65)}
.prose li > ul,.prose li > ol{margin:6px 0}
.prose blockquote{margin:20px 0;padding:12px 18px;border-left:2px solid rgba(217,178,90,.5);background:rgba(217,178,90,.055);border-radius:0 10px 10px 0;color:#b9c3d4}
.prose blockquote p{margin:6px 0}
.prose hr{border:0;border-top:1px solid var(--line-soft);margin:34px 0}
.prose code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;font-size:.86em;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.08);border-radius:5px;padding:1.5px 5px;color:#e8d6a8;word-break:break-word}
.prose pre{margin:18px 0;padding:16px 18px;background:#070b12;border:1px solid var(--line-soft);border-radius:12px;overflow-x:auto;line-height:1.7}
.prose pre code{background:none;border:0;padding:0;color:#c9d4e5;font-size:12.8px;word-break:normal}
.prose img{display:block;max-width:100%;height:auto;margin:22px auto;border-radius:10px;border:1px solid var(--line-soft);background:#0d1220}
.prose .table-wrap{overflow-x:auto;margin:20px 0;border:1px solid var(--line-soft);border-radius:10px}
.prose table{width:100%;border-collapse:collapse;font-size:13.5px;min-width:520px}
.prose th,.prose td{border-bottom:1px solid rgba(255,255,255,.08);border-right:1px solid rgba(255,255,255,.06);padding:9px 12px;text-align:left;vertical-align:top}
.prose th{background:rgba(255,255,255,.05);color:#e6ecf7;font-weight:600;white-space:nowrap}
.prose tr:last-child td{border-bottom:0}
.prose th:last-child,.prose td:last-child{border-right:0}
.prose tr:nth-child(even) td{background:rgba(255,255,255,.016)}
.prose .footnote{font-size:13px;color:var(--dim)}
.prose sup a{border:0}

/* 上下篇 */
.post-pager{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:64px;padding-top:26px;border-top:1px solid var(--line-soft)}
.pager-link{display:flex;flex-direction:column;gap:5px;padding:16px 18px;border:1px solid var(--line-soft);border-radius:12px;text-decoration:none;background:rgba(255,255,255,.025);transition:.2s}
.pager-link:hover{border-color:rgba(217,178,90,.42);background:rgba(217,178,90,.07)}
.pager-link.next{text-align:right}
.pager-link em{font-style:normal;font-size:10.5px;letter-spacing:.16em;color:var(--dim2)}
.pager-link b{font-family:var(--serif);font-size:15px;font-weight:600;color:#e4eaf5;line-height:1.5}
.pager-link.empty{border:0;background:none}

/* 博客首页 */
.blog-hero{padding:66px 0 42px;border-bottom:1px solid var(--line-soft)}
.kicker{display:inline-flex;align-items:center;gap:9px;font-size:11.5px;letter-spacing:.26em;color:var(--gold);text-transform:uppercase;padding:7px 15px;border:1px solid rgba(217,178,90,.32);border-radius:100px;background:rgba(217,178,90,.06)}
.kicker i{width:5px;height:5px;border-radius:50%;background:var(--gold)}
.blog-hero h1{font-family:var(--serif);font-size:clamp(36px,5.4vw,64px);font-weight:900;letter-spacing:.04em;margin-top:22px}
.blog-hero .lede{max-width:720px;margin-top:16px;font-size:16px;color:#b7c1d2;line-height:1.9}
.blog-hero .lede p{margin:6px 0}
.blog-hero .lede blockquote{border:0;padding:0;margin:8px 0;color:var(--dim);background:none;font-size:14px}
.hero-stats{display:flex;flex-wrap:wrap;gap:38px;margin-top:30px}
.hero-stats b{display:block;font-family:var(--serif);font-size:26px;color:#fff;line-height:1.3}
.hero-stats span{font-size:11.5px;letter-spacing:.14em;color:var(--dim2)}
.home-head{display:flex;align-items:baseline;justify-content:space-between;gap:20px;flex-wrap:wrap;margin:6px 0 18px}
.home-head .sec-label{font-size:11px;letter-spacing:.24em;color:var(--gold);text-transform:uppercase}
.home-head p{font-size:12.5px;color:var(--dim2);max-width:460px;line-height:1.8}
.cat-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
.cat-card{display:flex;flex-direction:column;padding:22px 24px;border-radius:16px;border:1px solid var(--line-soft);background:rgba(255,255,255,.028);text-decoration:none;transition:.22s}
.cat-card:hover{border-color:rgba(217,178,90,.4);background:rgba(255,255,255,.055);transform:translateY(-2px)}
.cat-top{display:flex;align-items:center;justify-content:space-between;gap:10px}
.cat-dot{width:7px;height:7px;border-radius:50%;background:var(--gold);box-shadow:0 0 10px rgba(217,178,90,.7)}
.cat-count{font-size:10.5px;color:var(--dim2);letter-spacing:.1em}
.cat-card h3{font-family:var(--serif);font-size:20px;font-weight:600;color:#eaf0fa;margin-top:14px;letter-spacing:.03em}
.cat-card p{font-size:12.5px;color:var(--dim);line-height:1.8;margin-top:8px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.cat-go{margin-top:auto;padding-top:16px;font-size:11.5px;color:#d9b25a;letter-spacing:.08em}
.note-list{display:grid;gap:8px;margin-top:4px}
.note-row{display:flex;flex-direction:column;gap:4px;padding:14px 18px;border-radius:10px;border:1px solid var(--line-soft);background:rgba(255,255,255,.022);text-decoration:none;transition:.2s}
.note-row:hover{border-color:rgba(255,255,255,.18);background:rgba(255,255,255,.05)}
.note-row b{font-family:var(--serif);font-size:15px;color:#e4eaf5;font-weight:600}
.note-row span{font-size:12px;color:var(--dim2);line-height:1.7}

/* 页脚 */
.blog-foot{margin-top:40px;padding:26px 0 54px;border-top:1px solid var(--line-soft);font-size:11.5px;color:var(--dim2)}
.foot-inner{display:flex;justify-content:space-between;gap:18px;flex-wrap:wrap;line-height:1.8}
.foot-inner a{color:var(--dim);text-decoration:none}
.foot-inner a:hover{color:var(--gold)}
.foot-inner code{font-size:11px;color:#b7c1d2}

@media (max-width:900px){
  .blog-wrap{grid-template-columns:minmax(0,1fr);gap:26px;padding:22px 22px 64px}
  .blog-side{position:static;max-height:none;overflow:visible;padding-right:0}
  .side-toggle{display:inline-flex}
  .side-body{display:none}
  .blog-side.open .side-body{display:block}
  .blog-side .tree{max-height:none}
  .cat-grid{grid-template-columns:1fr}
  .post-pager{grid-template-columns:1fr}
  .pager-link.next{text-align:left}
  .blog-hero{padding:46px 0 32px}
}
@media (max-width:860px){
  body{padding-top:58px}
  .nav-inner{padding:0 20px;height:58px}
  .brand em{display:none}
  .menu-btn{display:flex}
  .nav-links{display:none;position:absolute;left:0;right:0;top:58px;flex-direction:column;align-items:stretch;gap:2px;padding:10px 16px 18px;background:rgba(10,14,22,.98);border-bottom:1px solid rgba(255,255,255,.1)}
  .nav-links.open{display:flex}
  .nav-links > a,.nav-drop > a{padding:11px 12px;font-size:14px;border-radius:10px}
  .drop-panel{position:static;width:auto;opacity:1;visibility:visible;transform:none;background:transparent;border:0;box-shadow:none;padding:0 0 4px 10px}
  .drop-panel a{padding:8px 12px;font-size:13px}
  .nav-cta{margin:8px 0 0;justify-content:center}
  .blog-side{max-height:60vh;overflow-y:auto}
}
@media (max-width:520px){
  .blog-wrap{padding:18px 16px 54px}
  .prose{font-size:15px}
  .prose pre{padding:13px 14px}
  .hero-stats{gap:24px}
}
@media (prefers-reduced-motion:reduce){
  html{scroll-behavior:auto}
  *,*::before,*::after{transition:none !important;animation:none !important}
}
"""

BLOG_JS = r"""
const menuBtn = document.getElementById('menuBtn');
const navLinks = document.getElementById('navLinks');
if (menuBtn && navLinks) {
  menuBtn.addEventListener('click', () => {
    const open = navLinks.classList.toggle('open');
    menuBtn.classList.toggle('open', open);
    menuBtn.setAttribute('aria-expanded', String(open));
  });
  navLinks.querySelectorAll('a').forEach(a => a.addEventListener('click', () => {
    navLinks.classList.remove('open');
    menuBtn.classList.remove('open');
    menuBtn.setAttribute('aria-expanded', 'false');
  }));
}

const treeQ = document.getElementById('treeQ');
const sideToggle = document.getElementById('sideToggle');
const blogSide = document.querySelector('.blog-side');
const folderOpenState = new Map();
document.querySelectorAll('.tree-folder').forEach(folder => folderOpenState.set(folder, folder.open));
if (sideToggle && blogSide) {
  sideToggle.addEventListener('click', () => {
    const open = blogSide.classList.toggle('open');
    sideToggle.setAttribute('aria-expanded', String(open));
    sideToggle.textContent = open ? '收起' : '目录';
  });
}
if (treeQ) {
  treeQ.addEventListener('input', () => {
    const kw = treeQ.value.trim().toLowerCase();
    document.querySelectorAll('.tree-link').forEach(a => {
      const hit = !kw || a.textContent.toLowerCase().includes(kw) || (a.title || '').toLowerCase().includes(kw);
      a.classList.toggle('hidden', !hit);
    });
    const folders = [...document.querySelectorAll('.tree-folder')].reverse();
    folders.forEach(folder => {
      const visible = folder.querySelectorAll('.tree-link:not(.hidden)').length > 0;
      folder.classList.toggle('hidden', !visible);
      folder.open = kw ? visible : (folderOpenState.get(folder) || false);
    });
    document.querySelectorAll('.tree-label').forEach(label => {
      const list = label.nextElementSibling;
      if (list && list.classList.contains('tree-link')) {
        label.classList.toggle('hidden', !folderVisibleAfter(label));
      }
    });
    function folderVisibleAfter(label) {
      let node = label.nextElementSibling;
      while (node && node.classList.contains('tree-link')) {
        if (!node.classList.contains('hidden')) return true;
        node = node.nextElementSibling;
      }
      return false;
    }
  });
}

document.addEventListener('DOMContentLoaded', () => {
  if (window.renderMathInElement) {
    renderMathInElement(document.querySelector('.prose') || document.body, {
      delimiters: [
        { left: '$$', right: '$$', display: true },
        { left: '\\(', right: '\\)', display: false },
        { left: '\\[', right: '\\]', display: true }
      ],
      ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code'],
      throwOnError: false
    });
  }
});
"""


# ---------------------------------------------------------------- 主流程


def main() -> None:
    if not DOCS.exists() or not any(DOCS.glob("*.md")):
        sys.exit("docs/ 为空，请先运行：git submodule update --init --recursive")

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    print("→ 读取 Markdown …")
    posts, by_src, intro_html = collect_posts()
    root = build_tree(posts)

    print("→ 复制图片资源 …")
    images = copy_images()
    write_assets()

    ordered: list[Post] = []
    for post in root.posts:
        ordered.append(post)
    for child in root.children.values():
        ordered.extend(walk_posts(child))

    total = len([p for p in posts if not p.is_readme])
    print("→ 生成页面 …")
    for post in posts:
        target = OUT / post.out_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_post(post, root, ordered, total), encoding="utf-8")

    (OUT / "index.html").write_text(render_home(root, ordered, intro_html, total), encoding="utf-8")

    print(
        f"✓ 完成：{len(posts)} 个页面（{total} 篇笔记 + {len(posts) - total} 个目录概览），"
        f"{len(root.children)} 个一级目录，{images} 张图片 → blog/"
    )


if __name__ == "__main__":
    main()
