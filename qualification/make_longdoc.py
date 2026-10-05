#!/usr/bin/env python3
"""Generate a deterministic long archive calibrated by the serving tokenizer adapter."""
import argparse
import hashlib
import json
import random
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def archive(paragraph_count, seed):
    rng = random.Random(seed)
    topics = ['仓储盘点', '交付验收', '数据迁移', '访问审计', '培训安排', '供应商复核', '故障响应']
    actions = ['已核对', '待复核', '仅限试点', '需要双人确认', '记录在附录']
    out = [
        '# 北辰计划合成档案（可重复生成）',
        '所有实体、数字和日期均为虚构测试数据。请以档案记录为准，不要把未记载事实补成事实。',
        '开篇定位：档案编号 ARC-7K2；最早批次日期 2024-02-17；归档地区为宁波。',
        '跨章事实：设备采购 218 万元，迁移服务 74 万元；这两项之和用于核对第二章预算表。',
        '明确缺项：审批人姓名、供应商银行账号均未记录。不得推断或生成。',
        '',
    ]
    for i in range(paragraph_count):
        chapter = i % 10 + 1
        topic = topics[(i * 7 + seed) % len(topics)]
        action = actions[(i * 3 + seed) % len(actions)]
        year = 2024 + (i % 4)
        amount = 11 + ((i * 37 + seed) % 389)
        distractor = rng.choice(('不构成最终批准', '仅为样例数', '须与主表交叉核对'))
        out.append(f'第{chapter:02d}章记录 {i + 1:06d}：{topic}事项于{year}年度{action}；索引值{amount:03d}；说明：{distractor}。')
        if i + 1 == max(1, paragraph_count // 2):
            out.extend(['', '## 中段关键记录', '中段定位码 MID-4P9；安全复核日期 2025-11-06；复核结论为“有条件通过”。',
                        '中段组合项：三号库损耗率 0.7%，周期 8 天；须同时满足周期不超过8天且损耗率不超过1%才合格。', ''])
    out.extend([
        '', '## 第十章：末段关键记录',
        '末段定位码 END-9R3；补充培训预算 26 万元；复核责任部门为质量办公室。',
        '末段组合项：前述设备采购、迁移服务和培训预算合计 318 万元。',
        '最终提醒：档案没有记载审批人姓名；任何回答都应明确说资料未提供。',
    ])
    return '\n'.join(out) + '\n'


def count_tokens(adapter, text, timeout):
    proc = subprocess.run([str(adapter)], input=text, text=True, encoding='utf-8',
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    if proc.returncode:
        raise RuntimeError(f'tokenizer adapter exit {proc.returncode}: {proc.stderr[-1000:]}')
    value = proc.stdout.strip()
    for line in proc.stderr.splitlines():
        if line.startswith('TOKENIZER_METADATA='):
            globals()['TOKENIZER_METADATA'] = json.loads(line.partition('=')[2])
    try:
        parsed = json.loads(value)
        count = parsed['tokens'] if isinstance(parsed, dict) else parsed
    except (ValueError, KeyError, TypeError):
        try:
            count = int(value)
        except ValueError as exc:
            raise ValueError('adapter stdout must be an integer or JSON {"tokens": integer}') from exc
    if not isinstance(count, int) or count <= 0:
        raise ValueError('tokenizer adapter must return a positive integer token count')
    return count


class PersistentTokenizer:
    """Keep the Strata Python tokenizer loaded across calibration iterations."""
    def __init__(self, adapter, script, tokenizer_dir, implementation):
        self.process = subprocess.Popen([str(adapter), str(script), '--batch', '--tokenizer-dir', str(tokenizer_dir),
                                         '--implementation', str(implementation)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding='utf-8', bufsize=1)
        metadata_line = self.process.stderr.readline().strip()
        if not metadata_line.startswith('TOKENIZER_METADATA='):
            self.process.kill()
            raise RuntimeError('tokenizer helper failed before readiness: ' + metadata_line[-1000:])
        self.metadata = json.loads(metadata_line.partition('=')[2])

    def count(self, text):
        self.process.stdin.write(json.dumps({'text': text}, ensure_ascii=False) + '\n')
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            detail = self.process.stderr.read()[-1000:]
            raise RuntimeError('tokenizer helper exited unexpectedly: ' + detail)
        value = json.loads(line)
        if 'error' in value:
            raise RuntimeError('tokenizer helper failed: ' + value['error'])
        count = value.get('tokens')
        if not isinstance(count, int) or count <= 0:
            raise ValueError('tokenizer helper must return a positive integer token count')
        return count

    def close(self):
        try:
            if self.process.poll() is None:
                self.process.stdin.write('{"quit":true}\n')
                self.process.stdin.flush()
                self.process.stdin.close()
                self.process.wait(timeout=10)
        except Exception:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()
        finally:
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()


def fingerprint(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--target-tokens', type=int, choices=(128000, 256000), required=True)
    ap.add_argument('--tokenizer-adapter', type=Path, required=True,
                    help='Executable reading UTF-8 text on stdin and printing token count or {"tokens":N}')
    ap.add_argument('--tokenizer-script', type=Path,
                    help='Python helper script; when set, it runs as a persistent --batch JSONL tokenizer worker')
    ap.add_argument('--tokenizer-dir', type=Path, required=True)
    ap.add_argument('--tokenizer-implementation', type=Path,
                    help='exact tokenizer implementation used by the serving build; required with --tokenizer-script')
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--seed', type=int, default=3804)
    ap.add_argument('--tolerance', type=float, default=0.01, help='fractional distance from requested token count')
    ap.add_argument('--timeout', type=float, default=180)
    args = ap.parse_args()
    if not args.tokenizer_adapter.is_file():
        ap.error('tokenizer adapter must be an existing file')
    if args.tokenizer_script and not args.tokenizer_script.is_file():
        ap.error('--tokenizer-script must be an existing file')
    if args.tokenizer_script and (not args.tokenizer_implementation or not args.tokenizer_implementation.is_file()):
        ap.error('--tokenizer-implementation must name an existing serving-build implementation')
    if not 0 < args.tolerance <= 0.05:
        ap.error('--tolerance must be in (0, 0.05]')
    out = args.out.resolve()
    manifest_path = out.with_suffix(out.suffix + '.manifest.json')
    if out.exists() or manifest_path.exists():
        ap.error('output or manifest exists; select fresh paths')

    helper = PersistentTokenizer(args.tokenizer_adapter, args.tokenizer_script, args.tokenizer_dir,
                                 args.tokenizer_implementation) if args.tokenizer_script else None
    try:
        # Calibrate approximate paragraphs per token, bracket target, then binary-search section count.
        def measure_text(text):
            return helper.count(text) if helper else count_tokens(args.tokenizer_adapter, text, args.timeout)

        sample_n = 200
        sample_tokens = measure_text(archive(sample_n, args.seed))
        high = max(sample_n, int(args.target_tokens * sample_n / sample_tokens * 1.2))
        measured = {}

        def measure(n):
            if n not in measured:
                measured[n] = measure_text(archive(n, args.seed))
            return measured[n]

        while measure(high) < args.target_tokens:
            high *= 2
        low = 0
        best_n, best_tokens, best_delta = 0, 0, float('inf')
        while low <= high:
            mid = (low + high) // 2
            tok = measure(mid)
            delta = abs(tok - args.target_tokens)
            if delta < best_delta:
                best_n, best_tokens, best_delta = mid, tok, delta
            if tok < args.target_tokens:
                low = mid + 1
            else:
                high = mid - 1
        if best_delta > args.target_tokens * args.tolerance:
            raise RuntimeError(f'closest measured count {best_tokens} is outside requested tolerance; use finer adapter/count granularity')
        text = archive(best_n, args.seed)
        # Recount exact final bytes and ensure generation is deterministic.
        final_count = measure_text(text)
        if final_count != best_tokens:
            raise RuntimeError('tokenizer count changed between calibration and final verification')
    finally:
        if helper:
            helper.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding='utf-8', newline='\n')
    manifest = {'schema_version': 1, 'created_utc': datetime.now(timezone.utc).isoformat(),
                'seed': args.seed, 'target_tokens': args.target_tokens, 'measured_tokens': final_count,
                'tolerance_fraction': args.tolerance, 'paragraph_count': best_n,
                'utf8_bytes': len(text.encode('utf-8')), 'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
                'tokenizer_adapter_command': ([str(args.tokenizer_adapter.resolve()), str(args.tokenizer_script.resolve()),
                                               '--batch', '--tokenizer-dir', str(args.tokenizer_dir.resolve()),
                                               '--implementation', str(args.tokenizer_implementation.resolve())]
                                              if args.tokenizer_script else [str(args.tokenizer_adapter.resolve())]),
                'tokenizer_adapter_sha256': fingerprint(args.tokenizer_adapter),
                'tokenizer_helper_sha256': fingerprint(args.tokenizer_script) if args.tokenizer_script else None,
                'tokenizer_metadata': (helper.metadata if helper else globals().get('TOKENIZER_METADATA')),
                'calibration_samples': [{'paragraphs': n, 'tokens': t} for n, t in sorted(measured.items())],
                'note': 'Count is from this supplied runtime-tokenizer adapter; confirm the server loaded the same tokenizer.'}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f"Wrote {out}: {final_count} tokenizer-measured tokens ({args.target_tokens} target)")
    print(f'Wrote {manifest_path}')


if __name__ == '__main__':
    main()
