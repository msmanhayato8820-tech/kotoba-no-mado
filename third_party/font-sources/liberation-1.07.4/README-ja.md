# Liberation Sans 1.07.4 の対応ソース

このフォルダは、ことばの窓のPDF.js 6.4.299に含まれる LiberationSans-Regular / Bold / Italic / BoldItalic の4フォントについて、対応する原ソースと利用条件を受け取れるようにしたものです。アプリの起動時に読むファイルではありません。通常の利用者は展開・ビルド・インストールする必要はありません。

## 同梱内容

- fonts-liberation_1.07.4.orig.tar.gz: Debian公式アーカイブから取得した、未改変の1.07.4原ソース一式。
- License.txt / COPYING: 原ソース内の例外条項・GPLv2全文を、内容を変えずに取り出したコピー。
- fonts-liberation_1.07.4-11.dsc: 原ソースのサイズとSHA256の照合に使用したDebian記述ファイル。
- PDFJS-UPSTREAM-README.md: PDF.js v6.4.299のフォント説明の未改変コピー。
- PROVENANCE.json: 取得先、各ファイルのハッシュ、フォント4種とソース4種の照合記録。
- SHA256SUMS: このフォルダの各ファイルのSHA256一覧（一覧自身を除く）。

原ソースの中には、16種類のSFD、Makefile、scripts/、README、AUTHORS、ChangeLog、TODO、License.txt、COPYINGが含まれています。アプリが使うSans4種以外も削除せず、上流の原ソース一式を保持しています。Debian固有のパッケージ作成用差分（.debian.tar.xz）は使用・同梱していません。.dscはDebianパッケージ全体の同梱を意味しません。

## 取得先と検証

- 原ソース: https://archive.debian.org/debian/pool/main/f/fonts-liberation/fonts-liberation_1.07.4.orig.tar.gz
- Debian記述ファイル: https://archive.debian.org/debian/pool/main/f/fonts-liberation/fonts-liberation_1.07.4-11.dsc
- 原ソースのサイズ: 2,937,949 bytes
- 原ソースのSHA256: ad98b7498dc2992f7f0868f79b65ce4a720a3acdb63ab3f1f1cb6881117a5406
- PDF.jsの説明: https://github.com/mozilla/pdf.js/blob/v6.4.299/external/standard_fonts/README.md
- 比較対象の上流ソース: https://github.com/liberationfonts/liberation-1.7-fonts/tree/1b25642191bb57c05d128f6a20d9567e3079c2b8

同梱TTF4種はPDF.js v6.4.299の各ファイルとバイト一致し、原ソース内の対応SFD4種は上記の固定コミットとバイト一致しました。原ソースのサイズ・SHA256は同梱.dscに記載された値と一致しています。

GNU General Public License v2とLiberationフォント例外が適用されます。Liberation 2.0以降のSIL Open Font Licenseとは別です。License.txtとCOPYINGを確認してください。アプリ側の既存通知は ui/vendor/pdfjs/standard_fonts/LICENSE_LIBERATION にも残しています。

## ソースを確認・変更する方へ

.tar.gzを対応する展開ソフトで展開すると、liberation-fonts-1.07.4/に原ソースが入っています。ビルド手順と必要なFontForgeは原ソースのREADME・Makefileに記載されています。このアプリのためにフォントをOSへインストールする必要はありません。フォントを変更する際はLicense.txtの条件に加え、PDF.jsの文字幅・グリフ順に関するPDFJS-UPSTREAM-README.mdの注意を確認してください。

## 確認の限界

.dscのPGP署名の検証と、FontForgeを使った再ビルド／TTFのビット単位の再現検証は行っていません。ソース内のスクリプトも実行していません。照合記録は技術的な由来・同梱内容の確認であり、アプリ全体の法的適合性や公開準備完了を保証するものではありません。アプリ独自コードのMIT許諾は、アプリのルートにあるLICENSEを参照してください。本フォントのGPLv2＋フォント例外など、第三者の利用条件は別途適用され、アプリ独自コードのMIT許諾によって変更されません。
