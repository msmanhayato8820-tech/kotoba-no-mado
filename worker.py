"""Owned subprocess: official pinned PLaMo MLX path, no text persisted."""
import contextlib
import gc
import json
import signal
import sys
import time
from pathlib import Path

OUT = sys.stdout
CANCEL = {'requested': False}


def request_cancel(*_):
    # Sent by the app when the person presses "Stop" during a document.
    CANCEL['requested'] = True

def emit(event, **values):
    OUT.write('@localtranslate:' + json.dumps(dict(event=event, **values), ensure_ascii=False) + '\n')
    OUT.flush()


BUDGET_RATIO = 0.8  # aim to use at most 80% of the context length


def build_messages(data, history_count=None):
    """Earlier source/translation pairs go first, as the official template expects."""
    history = data.get('history') or []
    if history_count is not None:
        history = history[len(history) - history_count:] if history_count else []
    messages = []
    for pair in history:
        messages.append(dict(role='user', content=pair['source']))
        messages.append(dict(role='assistant', content=pair['translation']))
    messages.append(dict(role='user', content=data['text']))
    return messages


def make_prompt(tokenizer, data, history_count=None):
    from core import validate_language_pair
    validate_language_pair(data['source'], data['target'])
    extra = dict(glossary=data['glossary']) if data.get('glossary') else {}
    if data['source'] != 'auto':  # 'auto': the model detects the source language itself
        extra['source_lang'] = data['source']
    return tokenizer.apply_chat_template(build_messages(data, history_count),
        target_lang=data['target'], add_generation_prompt=True, tokenize=False, **extra)


def output_reserve(text_tokens):
    """Room kept for the translation: 1.5x the source tokens plus a margin."""
    return max(64, int(text_tokens * 1.5) + 32)


def count_tokens(tokenizer, data):
    """Real token counts for each text: (prompt incl. glossary, source text only)."""
    counts = []
    for text in data['items']:
        item = dict(data, text=text, history=[])
        counts.append([len(tokenizer.encode(make_prompt(tokenizer, item, 0), add_special_tokens=False)),
                       len(tokenizer.encode(text, add_special_tokens=False))])
    return counts


def fit_prompt(tokenizer, data, cap):
    """Fit prompt + context + output room inside the budget (80% of the context length).

    Context pairs are dropped oldest first. The translation may still use the whole
    context length, so a slightly longer translation is not cut at the 80% line.
    """
    history = data.get('history') or []
    budget = int(cap * BUDGET_RATIO)
    if data['limit'] == 'auto':
        reserve = data.get('reserve') or output_reserve(len(tokenizer.encode(data['text'], add_special_tokens=False)))
    else:
        reserve = data['limit']
    for count in range(len(history), -1, -1):
        tokens = tokenizer.encode(make_prompt(tokenizer, data, count), add_special_tokens=False)
        if len(tokens) + reserve <= budget:
            if data['limit'] == 'auto':
                # Use the rest of the context length, but stop a runaway repetition:
                # a translation longer than twice the reserved room is almost never right.
                limit = max(64, min(cap - len(tokens), reserve * 2))
            else:
                limit = data['limit']
            return tokens, limit, count
    return None, None, 0


def main():
    # Validate again immediately before running the model's required custom code.
    from core import inspect_model
    from model_catalog import DEFAULT_MODEL_ID
    info = inspect_model(sys.argv[1], sys.argv[2] if len(sys.argv)>2 else DEFAULT_MODEL_ID)
    from core import CONTEXT_TOKEN_CHOICES
    if len(sys.argv) > 3:
        requested = int(sys.argv[3])
        if requested not in CONTEXT_TOKEN_CHOICES:
            emit('error', message='一度に扱える長さを一覧から選び直してください。')
            return
        info['context_token_cap'] = requested
    try:
        import mlx.core as mx
        from mlx_lm import load, stream_generate
        from mlx_lm.sample_utils import make_sampler
        mx.set_cache_limit(256 * 1024 ** 2)
        mx.set_memory_limit(info['allocation_cap_bytes'])
        with contextlib.redirect_stdout(sys.stderr):
            model, tokenizer = load(info['path'], trust_remote_code=True,
                                    tokenizer_config={'trust_remote_code': True}, lazy=True)
            gc.collect()
            mx.eval(model.parameters())
            tokenizer.add_eos_token('<|plamo:tag|>')
    except Exception as e:
        # Do not expose exception values that may contain model input or filesystem data.
        emit('error', message='モデルを読み込めませんでした（' + type(e).__name__ + '）。対応版・空きメモリ・実行環境を確認してください。')
        return
    if hasattr(signal, 'SIGUSR1'):
        signal.signal(signal.SIGUSR1, request_cancel)
    emit('ready')
    for line in sys.stdin:
        try:
            data = json.loads(line)
            started = time.perf_counter()
            if data.get('op') == 'cancel':
                continue  # handled by the signal; the line itself is ignored here
            CANCEL['requested'] = False
            if data.get('op') == 'count':
                with contextlib.redirect_stdout(sys.stderr):
                    counts = count_tokens(tokenizer, data)
                emit('counts', counts=counts)
                continue
            with contextlib.redirect_stdout(sys.stderr):
                tokens, limit, used = fit_prompt(tokenizer, data, info['context_token_cap'])
                if tokens is None:
                    emit('input_error', code='too_long', message='一度に翻訳できる長さ（設定の8割）を超えています。文書として分けて翻訳します。')
                    continue
                parts, last = [], None
                for chunk in stream_generate(model, tokenizer, prompt=tokens, max_tokens=limit,
                                              sampler=make_sampler(temp=0), prefill_step_size=256):
                    parts.append(chunk.text)
                    last = chunk
                    if CANCEL['requested']:
                        break
                result = dict(text=''.join(parts).strip(), seconds=round(time.perf_counter() - started, 2),
                              context_used=used, cancelled=CANCEL['requested'],
                              warning='出力上限に達しました。文章を短くするか、出力上限を増やしてください。' if last is None or last.finish_reason != 'stop' else None)
                mx.clear_cache()
            emit('result', result=result)
        except Exception as e:
            emit('error', message='翻訳を停止しました（' + type(e).__name__ + '）。短い文章でモデルを読み込み直してください。')
            return

if __name__ == '__main__':
    main()
