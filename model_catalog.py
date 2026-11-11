"""Curated local model catalog. No network access, downloads, or model imports."""
import json
from pathlib import Path
import platform_support
BASE=Path(__file__).resolve().parent
DEFAULT_MODEL_ID='plamo-31b-mlx4'
CATALOG=json.loads((BASE/'model-catalog.json').read_text(encoding='utf-8'))


def model_entry(model_id=DEFAULT_MODEL_ID):
    if not isinstance(model_id,str) or model_id not in CATALOG:
        raise ValueError('対応する翻訳AIを一覧から選んでください。')
    return dict(CATALOG[model_id],id=model_id)


# Separate from load_enabled: this is not a tested integration, and must never
# enable the same GGUF on Mac or the independent 8B MLX experiment.
EXPERIMENTAL_WINDOWS_MODELS = frozenset(('plamo-8b-gguf-q4km', 'plamo-31b-gguf-q4km'))


def is_experimental(entry):
    return entry['id'] in EXPERIMENTAL_WINDOWS_MODELS and entry['backend'] == 'llama.cpp'


def experimental_enabled(entry):
    return (is_experimental(entry) and platform_support.host() == ('win32', 'x64')
            and ['win32', 'x64'] in entry.get('experimental_hosts', []))


def manifest_for(model_id):
    entry=model_entry(model_id)
    if is_experimental(entry) and not experimental_enabled(entry):
        raise ValueError('この未検証の試用はWindows x64専用です。このパソコンでは読み込めません。')
    if not entry['load_enabled'] and not experimental_enabled(entry):
        raise ValueError('この翻訳AIは追加準備中です。取得と動作確認が終わるまで読み込めません。')
    path=BASE/entry['manifest']
    if not path.is_file():
        raise ValueError('この翻訳AIの検査情報がありません。安全のため読み込みません。')
    manifest=json.loads(path.read_text(encoding='utf-8'))
    if manifest.get('repo')!=entry['repo'] or manifest.get('revision')!=entry['revision']:
        raise ValueError('翻訳AIと検査情報の版が一致しません。安全のため読み込みません。')
    return manifest


def public_models():
    result=[]
    for model_id in CATALOG:
        entry=model_entry(model_id)
        folder=entry.pop('suggested_folder',None)
        if model_id=='plamo-2b-gguf-q4km':
            # Only the bounded, name-and-size-checked preparation cache may be
            # described as prepared 2B. A legacy directory existing proves nothing.
            from runtime_discovery import prepared_model_folder
            entry['suggestion']=prepared_model_folder()
        else:
            suggestion=Path.home()/folder if folder else None
            entry['suggestion']=str(suggestion) if suggestion and suggestion.is_dir() else ''
        entry.pop('manifest',None)
        result.append(entry)
    return result
