#!/usr/bin/env python3
"""Count with the same ST.Tokenizer implementation and extracted tokenizer used by Strata serve."""
import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def load_tokenizer(tokenizer_dir, implementation):
    tokenizer_dir = Path(tokenizer_dir)
    required = [tokenizer_dir / 'vocab.json', tokenizer_dir / 'merges.txt', tokenizer_dir / 'token_type.json']
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError('missing Strata tokenizer data: ' + ', '.join(missing))
    implementation = Path(implementation)
    if not implementation.is_file():
        raise FileNotFoundError(f'Strata tokenizer implementation not found: {implementation}')
    spec = importlib.util.spec_from_file_location('strata_tokenizer', implementation)
    if spec is None or spec.loader is None:
        raise ImportError(f'cannot load tokenizer implementation from {implementation}')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    vocab = json.loads(required[0].read_text(encoding='utf-8'))
    tokens = [None] * len(vocab)
    for token, index in vocab.items():
        if not isinstance(index, int) or index < 0 or index >= len(tokens):
            raise ValueError('vocabulary IDs must be contiguous integer indexes')
        tokens[index] = token
    if any(token is None for token in tokens):
        raise ValueError('vocabulary has missing token IDs')
    merges = required[1].read_text(encoding='utf-8').split('\n')
    types = json.loads(required[2].read_text(encoding='utf-8'))
    tokenizer = module.Tokenizer(tokens, merges, types)
    metadata = {
        'tokenizer_dir': str(tokenizer_dir.resolve()),
        'implementation': str(implementation.resolve()),
        'implementation_sha256': file_sha(implementation),
        'files': {path.name: {'bytes': path.stat().st_size, 'sha256': file_sha(path)} for path in required},
        'vocab_entries': len(tokens),
        'count_method': 'ST.Tokenizer.encode(text, parse_special=False)',
    }
    return tokenizer, metadata


def main():
    # Popen's parent-side encoding does not configure this Python child's stdio.
    # Windows may otherwise decode UTF-8 request bytes with the active legacy code page.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='strict')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tokenizer-dir', type=Path, required=True)
    parser.add_argument('--implementation', type=Path, required=True,
                        help='Path to the implementation used by the exact serving build')
    parser.add_argument('--batch', action='store_true', help='persistent JSONL protocol for make_longdoc.py')
    args = parser.parse_args()
    tokenizer, metadata = load_tokenizer(args.tokenizer_dir, args.implementation)
    print('TOKENIZER_METADATA=' + json.dumps(metadata, ensure_ascii=False), file=sys.stderr, flush=True)
    if args.batch:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
                if request.get('quit'):
                    break
                text = request['text']
                if not isinstance(text, str):
                    raise TypeError('text must be a string')
                result = {'tokens': len(tokenizer.encode(text, parse_special=False))}
            except Exception as exc:
                result = {'error': f'{type(exc).__name__}: {exc}'}
            print(json.dumps(result, ensure_ascii=False), flush=True)
    else:
        text = sys.stdin.buffer.read().decode('utf-8')
        print(json.dumps({'tokens': len(tokenizer.encode(text, parse_special=False))}))


if __name__ == '__main__':
    main()
