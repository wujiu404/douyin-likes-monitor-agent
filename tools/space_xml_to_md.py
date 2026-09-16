# -*- coding: utf-8 -*-
"""
把 WorkBuddy 资料库导出的结构化标记文本，还原成干净的 Markdown。

输入格式形如：
    <Heading id="xxx" level="2">
      标题
    </Heading>
    <Paragraph id="xxx">
      正文，可能含 <Mark bold>加粗</Mark>
    </Paragraph>
    <BulletedList id="xxx">
      条目
    </BulletedList>
    <Code id="xxx" language="python">
      ```python
      code
      ```
    </Code>
    <Table id="xxx" rowHeader>
      <TableRow>...<TableCell><Paragraph>..</Paragraph></TableCell>...</TableRow>
    </Table>

用法：
    python space_xml_to_md.py 输入.txt 输出.md
"""
import re
import sys


OPEN_RE = re.compile(r'^<([A-Za-z]+)([^<>]*)>$')
CLOSE_RE = re.compile(r'^</([A-Za-z]+)>$')
MARK_RE = re.compile(r'<Mark\b[^>]*>(.*?)</Mark>', re.S)

# 导出文件尾部会带一行制表符分隔的元信息，不属于正文
META_LINE_RE = re.compile(r'^KS_[A-Z_]+\t')


def classify(s):
    """判断一行是块级标签、闭合标签还是正文。

    关键：行内标签（如 <Mark bold>字</Mark>）不是块级标签，必须当正文，
    否则贪婪匹配会把它后面的所有内容吞掉。
    """
    if not (s.startswith('<') and s.endswith('>')) or s.count('<') != 1:
        return ('text', s)
    if s.count('>') != 1:
        return ('text', s)
    m = CLOSE_RE.match(s)
    if m:
        return ('close', m.group(1))
    m = OPEN_RE.match(s)
    if m:
        tag, attrs = m.group(1), m.group(2).strip()
        selfclose = attrs.endswith('/')
        if selfclose:
            attrs = attrs[:-1].rstrip()
        return ('open', tag, attrs, selfclose)
    return ('text', s)


class Node:
    __slots__ = ('tag', 'attrs', 'items')

    def __init__(self, tag, attrs):
        self.tag = tag
        self.attrs = attrs
        self.items = []  # Node 或 ('text', raw_line)

    def attr(self, name, default=None):
        m = re.search(r'\b%s(?:="([^"]*)")?' % name, self.attrs or '')
        if not m:
            return default
        return m.group(1) if m.group(1) is not None else True


def parse(lines, i=0, end_tag=None):
    nodes = []
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        if not s or META_LINE_RE.match(s):
            i += 1
            continue
        kind = classify(s)
        if kind[0] == 'close':
            if end_tag and kind[1] == end_tag:
                return nodes, i + 1
            i += 1
            continue
        if kind[0] == 'open':
            _, tag, attrs, selfclose = kind
            if selfclose:
                nodes.append(Node(tag, attrs))
                i += 1
                continue
            children, i = parse(lines, i + 1, tag)
            n = Node(tag, attrs)
            n.items = children
            nodes.append(n)
            continue
        nodes.append(('text', raw))
        i += 1
    return nodes, i


def dedent(lines):
    body = [l for l in lines if l.strip()]
    if not body:
        return []
    pad = min(len(l) - len(l.lstrip(' ')) for l in body)
    return [l[pad:] if l.strip() else '' for l in lines]


def plain_text(node):
    """取一个节点下的纯文本（合并为一行）。"""
    parts = []
    for it in node.items:
        if isinstance(it, tuple):
            parts.append(MARK_RE.sub(r'\1', it[1]).strip())
        elif it.tag == 'Mark':
            parts.append(plain_text(it))
        else:
            parts.append(plain_text(it))
    return ' '.join(p for p in parts if p).strip()


def inline(raw):
    """处理行内标记。"""
    s = MARK_RE.sub(r'**\1**', raw).strip()
    return s


def text_of(node):
    """把一个块级节点的内容渲染为可读文本（保留 Mark 加粗与行尾硬换行）。"""
    chunks = []
    for it in node.items:
        if isinstance(it, tuple):
            t = inline(it[1])
            if t:
                chunks.append(t)
        elif it.tag == 'Mark':
            chunks.append('**' + plain_text(it) + '**')
        elif it.tag == 'Paragraph':
            chunks.append(text_of(it))
        else:
            sub = text_of(it)
            if sub:
                chunks.append(sub)
    out = ''
    for c in chunks:
        if c.endswith('\\'):
            out += c[:-1].rstrip() + '  ' + '\n'
        else:
            out += c + ' '
    return out.strip()


def collect_raw_code(node):
    lines = []
    for it in node.items:
        if isinstance(it, tuple):
            lines.append(it[1])
    return '\n'.join(dedent(lines)).rstrip()


def render_table(node, out):
    rows = [it for it in node.items if isinstance(it, Node) and it.tag == 'TableRow']
    has_header = bool(node.attr('rowHeader'))
    grid = []
    for r in rows:
        cells = [it for it in r.items if isinstance(it, Node) and it.tag == 'TableCell']
        grid.append([plain_text(c).replace('|', '\\|').replace('\n', ' ') for c in cells])
    if not grid:
        return
    width = max(len(r) for r in grid)
    grid = [r + [''] * (width - len(r)) for r in grid]
    head_idx = 0 if has_header else None
    if head_idx is None:
        grid.insert(0, [''] * width)
        head_idx = 0
    out.append('| ' + ' | '.join(grid[head_idx]) + ' |')
    out.append('|' + '---|' * width)
    for r in grid[head_idx + 1:]:
        out.append('| ' + ' | '.join(r) + ' |')
    out.append('')


def render(nodes, out):
    for n in nodes:
        if isinstance(n, tuple):
            t = inline(n[1])
            if t:
                out.append(t)
            continue

        tag = n.tag
        if tag == 'Heading':
            lvl = int(n.attr('level', '1'))
            out.append('')
            out.append('#' * lvl + ' ' + text_of(n))
            out.append('')
        elif tag == 'Paragraph':
            out.extend(text_of(n).split('\n'))
        elif tag == 'BulletedList':
            out.append('- ' + text_of(n))
        elif tag == 'NumberedList':
            out.append('1. ' + text_of(n))
        elif tag == 'BlockQuote':
            inner = []
            render(n.items, inner)
            for line in inner:
                for sub in line.split('\n'):
                    out.append('> ' + sub if sub.strip() else '>')
            out.append('')
        elif tag == 'Divider':
            out.append('')
            out.append('---')
            out.append('')
        elif tag == 'Code':
            out.append('')
            out.append(collect_raw_code(n))
            out.append('')
        elif tag == 'Table':
            render_table(n, out)
        else:
            render(n.items, out)


def main():
    src, dst = sys.argv[1], sys.argv[2]
    with open(src, encoding='utf-8') as f:
        lines = f.read().split('\n')
    nodes, _ = parse(lines)
    out = []
    render(nodes, out)

    # 折叠多余空行
    cleaned = []
    blank = 0
    for line in out:
        if line.strip():
            blank = 0
            cleaned.append(line.rstrip())
        else:
            blank += 1
            if blank <= 1:
                cleaned.append('')
    with open(dst, 'w', encoding='utf-8') as f:
        f.write('\n'.join(cleaned).strip() + '\n')
    print('{} -> {}  ({} 行)'.format(src, dst, len(cleaned)))


if __name__ == '__main__':
    main()
