# 第三者ソフト・モデルの確認事項

この文書はv0.8.0-rc.1公開試用版の第三者部品・外部取得モデルの条件と、確認の限界をまとめています。各ライセンス本文を保持していますが、全面的なライセンス適合保証ではありません。

## PLaMo

Built with PLaMo。モデルは同梱しません。利用者が公式配布元から取得し、自分の用途の利用条件・商用登録要件を確認します。
https://huggingface.co/pfnet/plamo-3-translate-2b-GGUF/blob/4f672106b847200bfdc0f6af6c321cd94eba1aed/LICENSE/ja
https://forms.gle/mTL8tBLrMYXKNZD56

モデルを第三者に提供する場合の契約コピー・表示、翻訳結果を外部公表する際の条件などは公式全文を確認してください。無料配布や任意支援だけで非商用と判断しません。

固定版第5条では、通常の翻訳公表でPLaMo使用表示を省略できる場合と、書籍・論文等の全部又は実質的部分に使用する際に表示が必要な場合を区別しています。さらに生成物の第三者提供・公開時には、その生成物で学習したモデルにPLaMoコミュニティライセンスが適用される旨の表示条件があります。詳しくは初心者ガイドと公式全文を確認してください。私的な保存ファイルへは一律に表示を自動挿入しません。

8B・31Bの対象固定版の日本語ライセンスも2Bとバイト一致を確認しました。モデル本体は同梱せず、利用者自身が公式から取得します。

## llama.cpp

固定版b11938の実行部はこのZIPに同梱せず、利用者の確認後に公式から取得します。再配布する場合はMIT全文と組込依存の通知を別途収集する必要があります。公式ZIP内のLLVM OpenMPライセンスは保持します。
https://github.com/ggml-org/llama.cpp/blob/b11938/LICENSE

## PDF.jsと同梱フォント

PDF.js 6.4.299はApache-2.0。ui/vendor/pdfjs/LICENSE、既存の著作権ヘッダー、CMapとフォントの各通知を保持しています。

LiberationSans 1.07.4はGPLv2＋フォント例外です（OFLではありません）。ui/vendor/pdfjs/standard_fonts/LICENSE_LIBERATIONを保持しています。対応する原ソース一式を、未改変の third_party/font-sources/liberation-1.07.4/fonts-liberation_1.07.4.orig.tar.gz として同梱します。SFD、Makefile、scripts、License.txt、COPYINGを含む原アーカイブ全体を保ち、別版フォントへの置換はしていません。

原ソースはDebian公式アーカイブから取得し、2,937,949 bytesとSHA256 ad98b7498dc2992f7f0868f79b65ce4a720a3acdb63ab3f1f1cb6881117a5406 が同梱.dscの値と一致しました。TTF4種はPDF.js v6.4.299、対応SFD4種は上流固定コミット1b25642191bb57c05d128f6a20d9567e3079c2b8とバイト一致を確認しています。取得先、利用条件、照合記録は third_party/font-sources/liberation-1.07.4/README-ja.md と PROVENANCE.json を確認してください。.dscのPGP署名検証とFontForgeによる再ビルド検証は未実施です。
https://archive.debian.org/debian/pool/main/f/fonts-liberation/fonts-liberation_1.07.4.orig.tar.gz
https://github.com/mozilla/pdf.js/blob/v6.4.299/external/standard_fonts/README.md

Foxit/PDFiumフォント・Adobe CMapの既存通知も同梱先に保持しています。

## PythonとChrome

同梱しません。それぞれの公式配布から利用者が導入します。アプリが自動インストールや保護設定の無効化をすることはありません。

## アプリ独自コード

アプリ独自コードはMIT Licenseです。著作権表示は Copyright (c) 2026 AI研究室。全文はLICENSEに同梱しています。著作権表示と許諾文を残す条件で、利用・改変・再配布・商用利用を許諾します。

このMIT許諾はアプリ独自コードに対するものです。PDF.js・同梱フォント・CMap等の第三者部品や、別途取得するPLaMoモデル・llama.cpp・Python・Chromeには、それぞれ固有の利用条件が適用されます。MITによってそれらの条件を変更したり、PLaMoの商用登録等を免除したりするものではありません。
