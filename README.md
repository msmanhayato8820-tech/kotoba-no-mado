# ことばの窓 v0.8.0-rc.1

公開試用版（pre-release）です。PLaMoを使い、パソコンの中で文章を翻訳するアプリです。安定版ではありません。まず付属の架空の例文で試し、重要な文書・個人情報は使わないでください。

## はじめ方

1. 自分のOS用ZIPを新しいフォルダに展開し、START-HERE-ja.htmlを開きます。以前の版は残してください。
2. 通常版CPython 3.12 / 3.13 / 3.14の64bit版とGoogle Chromeを用意します。詳しい対象は初心者ガイドを確認してください。
3. Windows 11 x64はOpen-Windows.cmd、Apple Silicon MacはOpen.commandから起動します。
4. 初めてなら2Bの準備内容を確認し、公式取得元・容量・利用条件を読んで準備します。準備済み2Bは、読み込み時のSHA-256検査に合格すれば再ダウンロードせず使えます。
5. 短文、またはsamples/English-to-Japanese.docxから試します。Microsoft Word本体のインストールは不要です。

モデル本体・llama.cpp・Python・Chromeは同梱しません。各公式配布元と利用条件を確認してください。PLaMoの商用登録等の要件は利用者ごとに確認する必要があります。

## できることと注意

- 53言語間の翻訳、短文の逆翻訳、テキスト・Word（.docx）・文字情報入りPDFの読込み、訳文または対訳のTXT/DOCX保存。
- 元のWordの書式やレイアウトを保持する機能ではありません。表・テキストボックス・段組みなどは、抜け・重複・順番を翻訳前に確認してください。画像だけのPDF、OCR、旧Word（.doc）は対象外です。
- 外部の翻訳APIは使いません。初回準備の取得には通信が必要です。OS全体の通信を止める機能ではなく、すべての通信を検証したという意味でもありません。同期フォルダへの保存や所属先の規則にも注意してください。
- 最初は2B・CPU・長さ4,096をおすすめします。Windows 16GBの開発機では8Bがメモリ保護で停止し、翻訳成功は未確認です。31BのWindows実行も未確認です。EXTERNAL-MODELS-ja.mdを確認してください。

## 確認した範囲

Windows 11（Ryzen 5 8645HS・16GB）とMac mini M6（32GB）の2B・CPUで、短文・付属Wordの翻訳とDOCX保存を確認しました。Windowsでは実Word文書の取込とMicrosoft Word本体での再読込も確認しています。Macは既存CPython 3.12.16 arm64からOpen.command→Chromeで試験しました。

自動回帰はWindows 495件中461成功・34明示skip、Mac 495件中486成功・Windows専用9skip、失敗0です。skipは成功に含めません。VERIFICATION-ja.mdに詳しい範囲があります。

利用者本人のWord文書試用、新規Python導入からの通し操作、今回版での新規モデル取得、Finder/Gatekeeper経路、MacのWord本体表示は未確認です。他機種・任意の文書・翻訳の正確さを保証しません。大切な文章は人が確認してください。

## ライセンス

Built with PLaMo。アプリ独自コードはMIT License、Copyright (c) 2026 AI研究室。LICENSE全文とTHIRD-PARTY-NOTICES.mdを確認してください。PDF.js・フォント・CMap、別途取得するモデル・実行環境にはそれぞれの条件が適用されます。MITはPLaMoの登録・利用条件を置き換えません。

## English

Kotoba no Mado v0.8.0-rc.1 is a public trial (pre-release), not a stable release. It translates text locally with PLaMo. Start with the bundled fictional sample, not sensitive or important documents.

Read START-HERE-ja.html (Japanese). Extract the OS-specific ZIP into a new folder. Use regular 64-bit CPython 3.12, 3.13 or 3.14 and Google Chrome. Run Open-Windows.cmd on Windows 11 x64 or Open.command on an Apple Silicon Mac. Models, llama.cpp, Python and Chrome are not bundled. Review official download sources, license terms and any required PLaMo registration. A prepared 2B cache is reusable only after SHA-256 verification succeeds.

Word (.docx), text and text-based PDF import and TXT/DOCX export are supported. Microsoft Word is not required. Original document formatting/layout is not preserved; check tables, text boxes, missing/duplicate text and reading order. OCR, image-only PDF and legacy .doc are unsupported. Translation does not use an external translation API, but initial preparation downloads require networking. This does not block or verify all system traffic; files saved to cloud-synced folders may be uploaded by other software.

2B CPU translation and Word export were tested on the development Windows and Mac computers. Windows tests also reopened output in Microsoft Word. Automated regression: Windows 461 passed and 34 skipped out of 495; Mac 486 passed and 9 Windows-only skips out of 495, with no failures. Skips are not passes. The user's own Word acceptance trial, a fresh-Python walkthrough, fresh model downloads for this version, Finder/Gatekeeper launch, and native Word display on Mac remain untested. Windows 8B stopped under memory protection on the 16 GB test machine; Windows 31B is untested. Other hardware, arbitrary documents and translation accuracy are not guaranteed.

Original application code: MIT, Copyright (c) 2026 AI研究室. Third-party components and separately obtained models/runtimes retain their own terms. See LICENSE, THIRD-PARTY-NOTICES.md and VERIFICATION-ja.md.
