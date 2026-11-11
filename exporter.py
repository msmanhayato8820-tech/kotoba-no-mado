"""Build .txt / .docx files from translation results, in memory only.

The bytes go back to the same local page, which hands them to the browser's
own download. Nothing is uploaded, and the app itself writes no file.
"""
import io
import re
import zipfile
from xml.sax.saxutils import escape

MAX_EXPORT_CHARS = 1_200_000
# Characters XML 1.0 cannot carry (control codes other than tab/newline).
INVALID_XML = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]')
LAYOUTS = ('translation', 'pairs')


def _clean(value):
    if not isinstance(value, str):
        raise ValueError('保存する内容が正しくありません。')
    return INVALID_XML.sub('', value.replace('\r\n', '\n').replace('\r', '\n'))


def normalise(items, layout):
    if layout not in LAYOUTS:
        raise ValueError('保存する内容を選び直してください。')
    if not isinstance(items, list) or not items or len(items) > 5000:
        raise ValueError('保存する翻訳結果がありません。')
    rows, total = [], 0
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('保存する内容が正しくありません。')
        source = _clean(item.get('source', ''))
        translation = _clean(item.get('translation', ''))
        after = _clean(item.get('after', '\n\n'))
        total += len(source) + len(translation) + len(after)
        rows.append((source, translation, after))
    if total > MAX_EXPORT_CHARS:
        raise ValueError('保存する文章が長すぎます。')
    return rows


def build_text(items, layout):
    rows = normalise(items, layout)
    if layout == 'translation':
        body = ''.join(t + a for _, t, a in rows).rstrip('\n') + '\n'
    else:
        body = '\n\n'.join('【原文】\n' + s + '\n【訳文】\n' + t for s, t, _ in rows) + '\n'
    # UTF-8 with BOM: also opens correctly in older Windows editors.
    return ('﻿' + body.replace('\n', '\r\n')).encode('utf-8')


def _paragraph(text, style=None):
    props = '<w:pPr><w:pStyle w:val="%s"/></w:pPr>' % style if style else ''
    runs = []
    for i, line in enumerate(text.split('\n')):
        if i:
            runs.append('<w:r><w:br/></w:r>')
        for j, piece in enumerate(line.split('\t')):
            if j:
                runs.append('<w:r><w:tab/></w:r>')
            if piece:
                runs.append('<w:r><w:t xml:space="preserve">%s</w:t></w:r>' % escape(piece))
    return '<w:p>%s%s</w:p>' % (props, ''.join(runs))


def build_docx(items, layout, title='翻訳結果'):
    rows = normalise(items, layout)
    paragraphs = []
    if layout == 'translation':
        text = ''.join(t + a for _, t, a in rows).strip('\n')
        # One Word paragraph per line: a .docx is read with a single newline
        # between its paragraphs, so splitting only at blank lines would save
        # the whole document as one paragraph with manual line breaks.
        # A blank line becomes one empty paragraph, as in the original Word file.
        for block in re.sub(r'\n{3,}', '\n\n', text).split('\n'):
            paragraphs.append(_paragraph(block))
    else:
        for source, translation, _ in rows:
            paragraphs.append(_paragraph('原文', 'Label'))
            paragraphs.append(_paragraph(source))
            paragraphs.append(_paragraph('訳文', 'Label'))
            paragraphs.append(_paragraph(translation))
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                + ''.join(paragraphs) +
                '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
                '</w:body></w:document>')
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
              '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Yu Gothic"/><w:sz w:val="22"/><w:lang w:val="en-US" w:eastAsia="ja-JP"/></w:rPr></w:rPrDefault>'
              '<w:pPrDefault><w:pPr><w:spacing w:after="160" w:line="300" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
              '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
              '<w:style w:type="paragraph" w:styleId="Label"><w:name w:val="Label"/><w:basedOn w:val="Normal"/><w:rPr><w:b/><w:color w:val="426E57"/><w:sz w:val="18"/></w:rPr></w:style>'
              '</w:styles>')
    files = {
        '[Content_Types].xml': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/><Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/></Types>',
        '_rels/.rels': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/></Relationships>',
        'word/_rels/document.xml.rels': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
        # No author, computer or user name is written into the file.
        'docProps/core.xml': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>%s</dc:title></cp:coreProperties>' % escape(_clean(title)[:100]),
        'word/document.xml': document,
        'word/styles.xml': styles,
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, content.encode('utf-8'))
    return buffer.getvalue()
