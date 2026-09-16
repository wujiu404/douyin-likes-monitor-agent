# -*- coding: utf-8 -*-
"""
把 docs/ 下的 Markdown 文档打包成一个自包含的单文件 HTML，便于离线阅读与迁移。

用法：
    python build_docs_html.py <docs目录> <输出html>
"""
import os
import re
import sys

import markdown

DOCS = [
    ('README.md', '总览与迁移说明'),
    ('01-技术方案-修订版.md', '技术方案 · 修订版'),
    ('02-方案设计-修订版.md', '方案设计 · 修订版'),
    ('03-架构修订说明.md', '架构修订说明'),
    ('04-存储与告警渠道选型.md', '存储与告警渠道选型'),
    ('05-面试题达成对照.md', '面试题达成对照'),
    ('06-项目框架与技术实现.md', '项目框架与技术实现'),
    ('07-面试讲述流程.md', '面试讲述流程'),
    ('08-演示操作流程.md', '演示操作流程'),
    ('09-功能验证清单.md', '功能验证清单'),
    ('10-迁移部署手册.md', '迁移部署手册'),
]

CSS = """
:root {
  --bg: #ffffff; --fg: #1f2328; --muted: #656d76; --line: #d8dee4;
  --accent: #0969da; --soft: #f6f8fa; --warn: #9a6700;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
               "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  font-size: 15px; line-height: 1.75;
}
.layout { display: flex; align-items: flex-start; }
nav {
  position: sticky; top: 0; flex: 0 0 268px; height: 100vh; overflow-y: auto;
  padding: 26px 18px; border-right: 1px solid var(--line); background: var(--soft);
  font-size: 13.5px;
}
nav .brand { font-weight: 700; font-size: 15px; margin-bottom: 4px; }
nav .sub { color: var(--muted); font-size: 12px; margin-bottom: 18px; }
nav a { display: block; color: var(--fg); text-decoration: none; padding: 3px 8px;
        border-radius: 5px; border-left: 2px solid transparent; }
nav a:hover { background: #eaeef2; color: var(--accent); }
nav a.doc { font-weight: 600; margin-top: 14px; border-left-color: var(--accent); }
nav a.h3 { padding-left: 20px; color: var(--muted); font-size: 12.8px; }
main { flex: 1 1 auto; min-width: 0; padding: 38px 46px 90px; max-width: 980px; }
section.doc { padding-bottom: 40px; margin-bottom: 40px; border-bottom: 3px double var(--line); }
section.doc:last-child { border-bottom: none; }
h1 { font-size: 25px; margin: 8px 0 18px; padding-bottom: 10px; border-bottom: 1px solid var(--line); }
h2 { font-size: 20px; margin: 34px 0 12px; padding-bottom: 6px; border-bottom: 1px solid var(--line); }
h3 { font-size: 16.5px; margin: 26px 0 10px; }
h4 { font-size: 15px; margin: 20px 0 8px; color: var(--muted); }
p { margin: 10px 0; }
a { color: var(--accent); }
ul, ol { padding-left: 24px; margin: 10px 0; }
li { margin: 4px 0; }
code { background: #eff1f3; padding: 2px 5px; border-radius: 4px;
       font-family: "Cascadia Mono", Consolas, "Courier New", monospace; font-size: 13px; }
pre { background: var(--soft); border: 1px solid var(--line); border-radius: 8px;
      padding: 14px 16px; overflow-x: auto; line-height: 1.5; }
pre code { background: none; padding: 0; font-size: 12.8px; white-space: pre; }
blockquote { margin: 14px 0; padding: 10px 16px; border-left: 4px solid var(--accent);
             background: #f0f6ff; color: #24292f; border-radius: 0 6px 6px 0; }
blockquote p { margin: 5px 0; }
table { border-collapse: collapse; width: 100%; margin: 14px 0; font-size: 13.6px; display: block; overflow-x: auto; }
th, td { border: 1px solid var(--line); padding: 7px 11px; text-align: left; vertical-align: top; }
th { background: var(--soft); font-weight: 600; white-space: nowrap; }
tr:nth-child(even) td { background: #fbfcfd; }
hr { border: none; border-top: 1px solid var(--line); margin: 26px 0; }
@media print {
  nav { display: none; }
  main { padding: 0; max-width: none; }
  section.doc { page-break-after: always; border-bottom: none; }
  pre, table { page-break-inside: avoid; }
}
@media (max-width: 900px) {
  .layout { flex-direction: column; }
  nav { position: static; height: auto; flex: none; width: 100%; border-right: none; border-bottom: 1px solid var(--line); }
  main { padding: 22px 18px 60px; }
}
"""


def strip_frontmatter(text):
    if text.startswith('---'):
        m = re.match(r'^---\s*\n.*?\n---\s*\n', text, re.S)
        if m:
            return text[m.end():]
    return text


def slug(text, idx):
    return 'doc-%d' % idx


def build(docs_dir, out_path):
    md = markdown.Markdown(
        extensions=['tables', 'fenced_code', 'toc', 'sane_lists', 'attr_list'],
        extension_configs={'toc': {'toc_depth': '2-3', 'anchorlink': False}},
    )

    parts = []
    navs = []
    for i, (fname, title) in enumerate(DOCS, 1):
        path = os.path.join(docs_dir, fname)
        if not os.path.exists(path):
            continue
        with open(path, encoding='utf-8') as f:
            body = strip_frontmatter(f.read())

        md.reset()
        html = md.convert(body)
        toc = md.toc

        sid = slug(title, i)
        parts.append('<section class="doc" id="%s">\n%s\n</section>' % (sid, html))

        items = []

        def walk(tokens):
            for t in tokens:
                items.append((t['id'], re.sub(r'<[^>]+>', '', t['name']), t['level']))
                walk(t.get('children', []))

        walk(md.toc_tokens)
        navs.append((title, sid, items))

    # 侧栏
    nav_html = ['<div class="brand">抖音点赞监控 Agent · 文档包</div>',
                '<div class="sub">离线自包含 · 可直接打印为 PDF</div>']
    for title, sid, items in navs:
        nav_html.append('<a class="doc" href="#%s">%s</a>' % (sid, title))
        for hid, text, level in items:
            if level > 3:
                continue
            cls = 'h3' if level == 3 else ''
            nav_html.append('<a class="%s" href="#%s">%s</a>' % (cls, hid, text))

    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>抖音点赞监控 Agent · 文档包</title>
<style>%s</style>
</head>
<body>
<div class="layout">
<nav>%s</nav>
<main>%s</main>
</div>
</body>
</html>
""" % (CSS, '\n'.join(nav_html), '\n'.join(parts))

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print('已生成 %s (%d 字节, %d 篇)' % (out_path, len(html.encode('utf-8')), len(parts)))


if __name__ == '__main__':
    build(sys.argv[1], sys.argv[2])
