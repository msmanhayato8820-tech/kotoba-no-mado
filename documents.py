"""Read text from user-chosen files in memory only. Standard library only.

Nothing is written to disk: the browser sends the file bytes to this local
process, the text is extracted from memory and returned to the same page.
"""
import io
import re
import zipfile
import xml.etree.ElementTree as ET

MAX_FILE_BYTES = 20 * 1024 * 1024          # 20 MB per file
MAX_DOCX_XML_BYTES = 60 * 1024 * 1024      # guard against zip bombs
MAX_TEXT_CHARS = 200_000                   # about a long paper or a short book
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
MC = '{http://schemas.openxmlformats.org/markup-compatibility/2006}'


class DocumentError(ValueError):
    pass


def detect_kind(name, data):
    lower = (name or '').lower()
    if data[:5] == b'%PDF-':
        return 'pdf'
    if data[:8] == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
        return 'doc'  # OLE2: old Word (.doc) and other old Office files
    if data[:4] == b'PK\x03\x04':
        return 'docx' if lower.endswith(('.docx', '.docm')) or _looks_like_docx(data) else 'zip'
    if data[:3] == b'\xff\xd8\xff' or data[:8] == b'\x89PNG\r\n\x1a\n' or data[:4] in (b'GIF8', b'II*\x00', b'MM\x00*') or data[8:12] in (b'WEBP', b'heic'):
        return 'image'
    if lower.endswith(('.txt', '.text', '.md', '.csv', '.tsv')) or not lower or '.' not in lower:
        return 'text'
    if lower.endswith(('.rtf', '.odt', '.pages', '.xlsx', '.pptx')):
        return 'unsupported'
    return 'text'


def _looks_like_docx(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return 'word/document.xml' in z.namelist()
    except zipfile.BadZipFile:
        return False


def decode_text(data):
    if data.startswith(b'\xef\xbb\xbf'):
        return data[3:].decode('utf-8'), 'UTF-8'
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16'), 'UTF-16'
    if b'\x00' in data[:4096]:
        raise DocumentError('文字のファイルとして読めません。テキスト、Word（.docx）のファイルを選んでください。')
    for encoding, label in (('utf-8', 'UTF-8'), ('cp932', 'Shift_JIS')):
        try:
            return data.decode(encoding), label
        except UnicodeDecodeError:
            continue
    raise DocumentError('文字コードを判別できません。UTF-8で保存し直してください。')


def _on(element):
    """Word on/off property: None when absent."""
    return None if element is None else element.get(W + 'val') not in ('0', 'false', 'off')


def _styles(xml):
    """styleId -> (numbered, hidden) after following basedOn; plus docDefaults hidden and the default paragraph style."""
    if not xml or b'<!DOCTYPE' in xml[:2048] or b'<!ENTITY' in xml[:4096]:
        return {}, None, None
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return {}, None, None
    raw, default_paragraph = {}, None
    for style in root.iter(W + 'style'):
        sid = style.get(W + 'styleId')
        if not sid:
            continue
        based = style.find(W + 'basedOn')
        number = style.find(W + 'pPr/' + W + 'numPr/' + W + 'numId')
        numbered = None if number is None else number.get(W + 'val') != '0'
        raw[sid] = (based.get(W + 'val') if based is not None else None, numbered, _on(style.find(W + 'rPr/' + W + 'vanish')))
        if style.get(W + 'type') == 'paragraph' and style.get(W + 'default') in ('1', 'true'):
            default_paragraph = sid

    def resolve(sid, index):
        seen = set()
        while sid in raw and sid not in seen:
            seen.add(sid)
            value = raw[sid][index]
            if value is not None:
                return value
            sid = raw[sid][0]
        return None
    styles = {sid: (resolve(sid, 1), resolve(sid, 2)) for sid in raw}
    return styles, _on(root.find(W + 'docDefaults/' + W + 'rPrDefault/' + W + 'rPr/' + W + 'vanish')), default_paragraph


# Namespaces whose textual drawing/Word containers this reader can traverse.
# This is not a claim to implement their visual layout or all drawing objects.
_MC_TEXT_NAMESPACES = {
    W[1:-1],
    'http://schemas.microsoft.com/office/word/2010/wordprocessingShape',
    'http://schemas.microsoft.com/office/word/2010/wordprocessingGroup',
    'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing',
    'http://schemas.openxmlformats.org/drawingml/2006/main',
    'urn:schemas-microsoft-com:vml',
}


def _compatible_document(xml):
    """Resolve in-scope Requires prefixes, then keep exactly one MC branch.

    Normalize before walking or counting, so skipped alternatives are neither
    duplicate text nor false missing-text/formatting warnings. If the supported
    branch yields no w:t but its fallback does, preserve that fallback instead.
    Every active AlternateContent gets a comparison warning: text extraction is
    not an implementation of Word's visual compatibility/rendering behavior.
    """
    scope, pending, frames, eligible = {}, [], [], set()
    parser = ET.iterparse(io.BytesIO(xml), events=('start-ns', 'start', 'end'))
    for event, item in parser:
        if event == 'start-ns':
            pending.append(item)
        elif event == 'start':
            prior = []
            for prefix, uri in pending:
                prior.append((prefix, scope.get(prefix)))
                scope[prefix] = uri
            frames.append(prior)
            pending = []
            if item.tag == MC + 'Choice':
                required = item.get('Requires', '').split()
                if required and all(scope.get(prefix) in _MC_TEXT_NAMESPACES for prefix in required):
                    eligible.add(item)
        else:
            for prefix, old in reversed(frames.pop()):
                if old is None:
                    scope.pop(prefix, None)
                else:
                    scope[prefix] = old
    root = parser.root

    def readable(node):
        return any((t.text or '').strip() for t in node.iter(W + 't'))

    def normalize(parent):
        count, replacement = 0, []
        for child in parent:
            if child.tag != MC + 'AlternateContent':
                count += normalize(child)
                replacement.append(child)
                continue
            count += 1
            fallback = next((c for c in child if c.tag == MC + 'Fallback'), None)
            selected = next((c for c in child if c.tag == MC + 'Choice' and c in eligible), fallback)
            if selected is None:
                continue  # no supported representation; disclosed by MC warning
            nested = normalize(selected)
            if not readable(selected) and fallback is not None and selected is not fallback:
                fallback_nested = normalize(fallback)
                if readable(fallback):
                    selected, nested = fallback, fallback_nested
            count += nested
            replacement.extend(list(selected))
        parent[:] = replacement
        return count

    body = root.find(W + 'body')
    return root, normalize(body) if body is not None else 0


def _run_text(element, seen=None):
    parts = []

    def visit(node, top=False):
        tag = node.tag
        if tag == W + 'p' and not top:
            # A paragraph inside a text box: keep it on its own line.
            parts.append('\n')
            for child in node:
                visit(child)
            parts.append('\n')
            return
        if tag == W + 't' and node.text:
            if seen is not None:
                if node in seen:
                    return
                seen.add(node)
            parts.append(node.text)
        elif tag == W + 'tab':
            parts.append('\t')
        elif tag in (W + 'br', W + 'cr'):
            parts.append('\n')
        elif tag == W + 'noBreakHyphen':
            parts.append('-')
        for child in node:
            visit(child)

    visit(element, True)
    return ''.join(parts)


def read_docx(data):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise DocumentError('Wordファイルを開けません。ファイルが壊れていないか確認してください。') from None
    with z:
        names = z.namelist()
        if 'word/document.xml' not in names:
            raise DocumentError('Word（.docx）の本文が見つかりません。')
        if sum(info.file_size for info in z.infolist()) > MAX_DOCX_XML_BYTES * 4:
            raise DocumentError('ファイルの展開後の容量が大きすぎます。文書を分けてください。')
        info = z.getinfo('word/document.xml')
        if info.file_size > MAX_DOCX_XML_BYTES:
            raise DocumentError('Wordの本文が大きすぎます。文書を分けてください。')
        try:
            xml = z.read('word/document.xml')
        except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError):
            raise DocumentError('Wordファイルを開けません。ファイルが壊れていないか、パスワードが付いていないか確認してください。') from None
        has_images = any(n.startswith('word/media/') for n in names)
        has_notes = any(n in names for n in ('word/footnotes.xml', 'word/endnotes.xml'))
        has_headers = any(re.match(r'word/(header|footer)\d*\.xml$', n) for n in names)
        has_comments = 'word/comments.xml' in names
        styles_xml = b''
        if 'word/styles.xml' in names and z.getinfo('word/styles.xml').file_size <= MAX_DOCX_XML_BYTES:
            try:
                styles_xml = z.read('word/styles.xml')
            except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError):
                styles_xml = b''
    # Word XML has no DTD; ElementTree does not resolve external entities.
    if b'<!DOCTYPE' in xml[:2048] or b'<!ENTITY' in xml[:4096]:
        raise DocumentError('安全のため、この形式のWordファイルは読み込みません。')
    root, alternate_count = _compatible_document(xml)
    body = root.find(W + 'body')
    if body is None:
        raise DocumentError('Word（.docx）の本文が見つかりません。')
    seen = set()
    wrappers = {W + name for name in ('sdt', 'sdtContent', 'customXml', 'smartTag', 'ins', 'moveTo')}

    def children_of_kind(parent, tag):
        # Descend through containers, but never enter a nested table/row/cell twice.
        for child in parent:
            if child.tag == tag:
                yield child
            elif child.tag == W + 'sdt':
                content = child.find(W + 'sdtContent')
                if content is not None:
                    yield from children_of_kind(content, tag)
            elif child.tag in wrappers:
                yield from children_of_kind(child, tag)

    def walk(parent):
        blocks = []
        for child in parent:
            if child.tag == W + 'p':
                blocks.append(_run_text(child, seen))
            elif child.tag == W + 'tbl':
                for row in children_of_kind(child, W + 'tr'):
                    cells = [' '.join(walk(cell)).strip() for cell in children_of_kind(row, W + 'tc')]
                    blocks.append(' | '.join(cells))
            elif child.tag == W + 'sdt':
                content = child.find(W + 'sdtContent')
                if content is not None:
                    blocks.extend(walk(content))
            elif child.tag in wrappers:
                blocks.extend(walk(child))
        return blocks
    blocks = walk(body)
    # Count unread original XML nodes, never formatting inserted by this reader.
    missing = sum(len(re.sub(r'\s', '', n.text or '')) for n in body.iter(W + 't') if n not in seen)
    counts = dict(controls=sum(1 for _ in body.iter(W + 'sdt')))
    tables = sum(1 for _ in body.iter(W + 'tbl'))
    textboxes = sum(1 for _ in body.iter(W + 'txbxContent'))
    # Automatic list/clause numbers are generated by Word and are not in the text,
    # whether set on the paragraph or by its style (e.g. numbered headings).
    styles, default_hidden, default_paragraph = _styles(styles_xml)
    numbered, hidden_by_style = 0, False
    for paragraph in body.iter(W + 'p'):
        style_tag = paragraph.find(W + 'pPr/' + W + 'pStyle')
        style_id = style_tag.get(W + 'val') if style_tag is not None else default_paragraph
        style_numbered, style_hidden = styles.get(style_id, (None, None))
        number = paragraph.find(W + 'pPr/' + W + 'numPr')
        number_id = number.find(W + 'numId') if number is not None else None
        if number_id is not None:
            numbered += number_id.get(W + 'val') != '0'
        elif number is not None or style_numbered:
            numbered += 1
        for run in paragraph.findall(W + 'r'):
            if _on(run.find(W + 'rPr/' + W + 'vanish')) is not None or not any(n.text for n in run.iter(W + 't')):
                continue  # direct setting is handled below; empty runs show nothing
            character = run.find(W + 'rPr/' + W + 'rStyle')
            value = styles.get(character.get(W + 'val'), (None, None))[1] if character is not None else None
            for candidate in (value, style_hidden, default_hidden):
                if candidate is not None:
                    hidden_by_style = hidden_by_style or candidate
                    break
    hidden = any(_on(r.find(W + 'rPr/' + W + 'vanish'))
                 and any(n.text for n in r.iter(W + 't')) for r in body.iter(W + 'r'))
    hidden = hidden or hidden_by_style
    notes = []
    if alternate_count:
        notes.append(('alternate', alternate_count))
    if re.search(r'\n[ \t]*\n[ \t]*\n', '\n'.join(blocks)):
        notes.append(('blank', 0))
    if hidden:
        notes.append(('hidden', 0))
    if numbered:
        notes.append(('numbering', numbered))
    if counts['controls']:
        notes.append(('controls', counts['controls']))
    if missing:
        notes.append(('missing', missing))
    if tables:
        notes.append(('table', tables))
    if textboxes:
        notes.append(('textbox', textboxes))
    if has_images:
        notes.append(('images', 0))
    if has_notes:
        notes.append(('footnotes', 0))
    if has_headers:
        notes.append(('headers', 0))
    if has_comments:
        notes.append(('comments', 0))
    return '\n'.join(blocks), notes


WARNINGS = {
    'alternate': '互換表示のための代替内容が{n}か所あります。表示内容を完全には再現できない場合があります。本文の抜けや重複を元のWordと見比べてください。',
    'controls': 'コンテンツコントロール（入力欄などの枠）が{n}か所あります。中の文字も読み込みましたが、順番や抜けを確認してください。',
    'missing': '読み込めなかった可能性のある文字があります（約{n}文字）。元のWordと見比べ、抜けている部分は貼り付けて補ってください。',
    'table': '表が{n}個あります。表は1行ずつ「 | 」区切りで読み込みました。セルの結合や読む順番がずれることがあります。',
    'textbox': 'テキストボックスなどの枠内の文字が{n}か所あります。本文の途中に入ったり、読み込めなかったりすることがあります。',
    'hidden': '隠し文字（Wordで非表示に設定された文字。スタイルで非表示にしたものを含みます）が含まれています。隠し文字も読み込んでいます。翻訳や保存に含めたくない場合は、下の文章から削除してください。',
    'blank': '連続した空行は、1行の空行にまとめて読み込みました。保存するWordでも空行は1行になります。',
    'numbering': '自動の番号や行頭記号が付いた段落が{n}か所あります。見出しのスタイルで付く番号も含みます。「1.」「第1条」「・」などWordが自動で付ける番号・記号は読み込まれません。必要なら下の文章に書き足し、番号への参照がずれていないか元のWordと見比べてください。',
    'images': '画像が含まれています。画像の中の文字は読み込みません。',
    'footnotes': '脚注・文末脚注は読み込んでいません。',
    'headers': 'ヘッダー・フッターは読み込んでいません。',
    'comments': 'コメント（校閲のメモ）は読み込んでいません。',
}

REFUSALS = {
    'pdf': 'PDFは画面（ブラウザ）の中で読み込みます。ページを再読み込みしてから、もう一度PDFを選んでください。',
    'doc': '古い形式のWord（.doc）には対応していません。Wordで開き、「名前を付けて保存」で「Word文書（.docx）」を選んでから読み込んでください。',
    'image': '画像ファイルは対象外です。画像の中の文字を読み取る機能（OCR）はありません。',
    'zip': 'このZIP形式のファイルには対応していません。テキストかWord（.docx）を選んでください。',
    'unsupported': 'この形式には対応していません。テキスト（.txt）かWord（.docx）で保存し直してください。',
}


def extract(name, data):
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise DocumentError('ファイルが空です。')
    if len(data) > MAX_FILE_BYTES:
        raise DocumentError('ファイルが大きすぎます（20 MBまで）。文書を分けてください。')
    kind = detect_kind(name, bytes(data))
    if kind in REFUSALS:
        raise DocumentError(REFUSALS[kind])
    warnings = []
    if kind == 'docx':
        text, notes = read_docx(bytes(data))
        warnings = [WARNINGS[key].format(n=n) for key, n in notes]
        detail = 'Word (.docx)'
    else:
        text, encoding = decode_text(bytes(data))
        detail = 'テキスト（' + encoding + '）'
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\x00', '')
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    if '<|plamo:' in text:
        text = text.replace('<|plamo:', '<| plamo:')
        warnings.append('翻訳AIの制御用の記号に似た文字列があったため、間に空白を入れました。')
    if not text:
        raise DocumentError('文字を読み取れませんでした。画像だけの文書は対象外です。')
    if len(text) > MAX_TEXT_CHARS:
        raise DocumentError('文章が長すぎます（20万文字まで）。文書を分けてください。')
    return dict(kind=kind, detail=detail, text=text, chars=len(text), warnings=warnings)


SENTENCE_END = re.compile(r'(?<=[。．！？!?])|(?<=[.;:])\s+')


def _split_long(paragraph, limit):
    pieces, current = [], ''
    for sentence in [s for s in SENTENCE_END.split(paragraph) if s]:
        while len(sentence) > limit:  # no sentence boundary: cut as a last resort
            if current:
                pieces.append(current); current = ''
            pieces.append(sentence[:limit]); sentence = sentence[limit:]
        if current and len(current) + len(sentence) + 1 > limit:
            pieces.append(current); current = ''
        current = (current + (' ' if current and not current.endswith((' ', '\n')) and re.match(r'[A-Za-z0-9]', sentence) else '') + sentence) if current else sentence
    if current:
        pieces.append(current)
    return pieces


def split_segments(text, limit):
    """Split into translation units no longer than `limit` characters.

    Returns [{'text': str, 'after': str | None}], where `after` is the original
    separator kept to rebuild paragraph breaks after translation. None means the
    paragraph continues: join with a space, or nothing for ja/zh targets.
    """
    if type(limit) is not int or not 100 <= limit <= 2000:
        raise ValueError('一度に翻訳する長さを選び直してください。')
    parts = re.split(r'(\n+)', text)
    segments, buffer, buffer_after = [], '', ''
    def flush():
        nonlocal buffer, buffer_after
        if buffer.strip():
            segments.append(dict(text=buffer, after=buffer_after))
        elif segments:
            segments[-1]['after'] += buffer + buffer_after
        buffer, buffer_after = '', ''
    for index in range(0, len(parts), 2):
        paragraph = parts[index]
        separator = parts[index + 1] if index + 1 < len(parts) else ''
        if not paragraph.strip():
            if buffer:
                buffer_after += separator
            elif segments:
                segments[-1]['after'] += paragraph + separator
            continue
        if len(paragraph) > limit:
            flush()
            pieces = _split_long(paragraph, limit)
            for i, piece in enumerate(pieces):
                segments.append(dict(text=piece, after=separator if i == len(pieces) - 1 else None))
            continue
        # Short paragraphs: one per segment, unless they are tiny (list items, headings).
        if buffer and len(buffer) + len(buffer_after) + len(paragraph) <= min(limit, 200):
            buffer += buffer_after + paragraph
            buffer_after = separator
        else:
            flush()
            buffer, buffer_after = paragraph, separator
    flush()
    return segments


NO_SPACE_TARGETS = ('ja', 'zh-hans', 'zh-hant')


def joiner(after, target):
    if after is not None:
        return after
    return '' if target in NO_SPACE_TARGETS else ' '
