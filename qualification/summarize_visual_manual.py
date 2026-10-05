#!/usr/bin/env python3
"""Build a metadata-only user-review matrix for the frozen pelican visual cases.

Reads existing manifests, requests, and result metadata only. It never renders,
opens, grades, rewrites, or executes model artifacts, and performs no requests.
The generated HTML links to original artifacts and raw response/reasoning files.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
DEFAULT_MATRIX = HERE / "visual_manual_native_comparison_20261004"
DEFAULT_BOUNDED_MATRIX = HERE / "visual_manual_comparison_20261004"
DEFAULT_LEGACY_NONE = HERE / "visual_manual_runs" / "strata-20261004"
ARMS = ("baseline", "candidate")
LEVELS = {
    "none": {"runner_effort": "none", "template_effort": "none"},
    "low": {"runner_effort": "low", "template_effort": "low"},
    "medium": {"runner_effort": "medium", "template_effort": "medium"},
    # The collector exposes `high`; the report uses the requested xhigh template label.
    "high": {"runner_effort": "high", "template_effort": "xhigh"},
}
CASES = (
    {"case_id": "visual-01", "artifact_type": "svg"},
)
REQUEST_EFFORT_FIELDS = {"reasoning_effort", "reasoning_budget_tokens"}


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_existing(path: Path | None):
    return path if path is not None and path.is_file() else None


def _find_request(case_dir: Path, case_id: str, legacy: bool) -> Path | None:
    path = case_dir / f"{case_id}.request.json" if legacy else case_dir / "request.json"
    return _safe_existing(path)


def _find_result(case_dir: Path, case_id: str, legacy: bool, manifest: dict):
    if not legacy:
        path = case_dir / "result.json"
        if path.is_file():
            return _read_json(path), path
        summary = case_dir.parent / "results.jsonl"
        if summary.is_file():
            for line in summary.read_text(encoding="utf-8-sig").splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if value.get("case_id") == case_id:
                    return value, summary
        return None, None

    response_path = case_dir / f"{case_id}.response.json"
    if not response_path.is_file():
        return None, None
    response = _read_json(response_path)
    choices = response.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") or {}
    # Legacy response.json is retained as the original response artifact.
    return {
        "case_id": case_id, "http_status": 200 if response.get("done", True) else None,
        "failure": response.get("error"), "finish_reason": choice.get("finish_reason") or response.get("finish_reason"),
        "completion_state": "natural" if (choice.get("finish_reason") or response.get("finish_reason")) == "stop" else "unknown",
        "usage": response.get("usage"), "wall_seconds": response.get("wall_seconds"),
        "first_content_seconds": response.get("first_answer_seconds"),
        "stream_observation": {"answer_characters": len(message.get("content") or ""),
                               "reasoning_characters": len(message.get("reasoning_content") or "")},
        "artifact_type": next((c.get("artifact_type") for c in (manifest.get("cases") or [])
                                if c.get("id") == case_id), None),
        "review_status": "user_manual_pending"}, response_path


def _artifact_paths(case_dir: Path, case_id: str, artifact_type: str, legacy: bool,
                    result: dict | None, result_path: Path | None):
    candidates = []
    if result and result.get("artifact_path"):
        candidates.append(Path(result["artifact_path"]))
    if legacy:
        candidates.append(case_dir / f"{case_id}.{artifact_type}")
        raw_text = case_dir / f"{case_id}.raw.txt"
        reasoning = None
        response = case_dir / f"{case_id}.response.json"
        request = case_dir / f"{case_id}.request.json"
        log = case_dir / f"{case_id}.engine.log"
    else:
        candidates.append(case_dir / f"model_output.{artifact_type}")
        raw_text = case_dir / "raw_response.txt"
        reasoning = case_dir / "reasoning.txt"
        response = case_dir / "response.sse"
        request = case_dir / "request.json"
        log = case_dir / "log_interval.jsonl"
    artifact = next((p for p in candidates if p.is_file()), None)
    return {"artifact": artifact, "raw_text": _safe_existing(raw_text),
            "reasoning": _safe_existing(reasoning), "raw_response": _safe_existing(response),
            "request": _safe_existing(request), "engine_trace": _safe_existing(log),
            "result": _safe_existing(result_path)}


def _request_record(path: Path | None):
    if path is None:
        return None
    request = _read_json(path)
    messages = request.get("messages") or []
    user_messages = [item.get("content") for item in messages
                     if isinstance(item, dict) and item.get("role") == "user"]
    normalized = {key: value for key, value in request.items() if key not in REQUEST_EFFORT_FIELDS}
    # Include all other request fields and exact prompt bytes in the comparand.
    normalized["__exact_user_messages__"] = user_messages
    canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    prompts = [p for p in user_messages if isinstance(p, str)]
    return {"path": str(path.resolve()), "sha256": _sha(path.read_bytes()),
            "normalized_sha256": _sha(canonical), "user_prompt_sha256": [_sha(p.encode("utf-8")) for p in prompts],
            "model": request.get("model"), "reasoning_effort": request.get("reasoning_effort"),
            "reasoning_budget_tokens": request.get("reasoning_budget_tokens"),
            "max_tokens": request.get("max_tokens"), "temperature": request.get("temperature"),
            "seed": request.get("seed"), "stream": request.get("stream")}


def _status(result: dict | None, result_path: Path | None, case_dir: Path, manifest: dict):
    run_state = str(manifest.get("status") or manifest.get("run_status") or "").casefold()
    if manifest.get("user_cancelled") or manifest.get("cancelled_by_user") or run_state in ("user_cancelled", "cancelled_by_user"):
        return "user_cancelled_partial"
    if result is None:
        return "partial_request_only" if (case_dir / "request.json").is_file() or list(case_dir.glob("*.request.json")) else "pending"
    if (result.get("user_cancelled") or result.get("cancelled_by_user") or
            str(result.get("status", "")).casefold() in ("user_cancelled", "cancelled_by_user")):
        return "user_cancelled_partial"
    http_status = result.get("http_status")
    finish = result.get("finish_reason")
    complete = (result.get("failure") is None and
                (http_status is None or 200 <= int(http_status) < 300) and
                result.get("completion_state") == "natural" and finish == "stop")
    if complete:
        return "captured_user_review_pending"
    if result.get("failure") or (http_status is not None and not 200 <= int(http_status) < 300):
        return "request_failed_or_http_error"
    return "partial_or_non_natural_finish"


def build_matrix(matrix_root: Path = DEFAULT_MATRIX,
                 legacy_none: Path = DEFAULT_LEGACY_NONE,
                 completed_animation_archive: Path | None = None) -> dict:
    matrix_root = Path(matrix_root).resolve()
    legacy_none = Path(legacy_none).resolve()
    rows, request_groups, run_manifests = [], {}, {}
    expected = len(ARMS) * len(LEVELS) * len(CASES)
    for arm in ARMS:
        for level, level_spec in LEVELS.items():
            run_dir = matrix_root / arm / level
            # The candidate's none reference is an already-completed run. Reuse
            # its original artifacts in place; do not copy or rewrite them.
            legacy = arm == "candidate" and level == "none"
            if legacy:
                run_dir = legacy_none
            manifest_path = run_dir / "manifest.json"
            manifest = _read_json(manifest_path) if manifest_path.is_file() else {}
            slot = f"{arm}/{level}"
            if manifest:
                run_manifests[slot] = {"path": str(manifest_path.resolve()),
                                       "run_id": manifest.get("run_id"),
                                       "arm_in_manifest": manifest.get("arm"),
                                       "requested_effort": manifest.get("requested_effort"),
                                       "budget": manifest.get("reasoning_budget_tokens",
                                                               (manifest.get("request_defaults") or {}).get("reasoning_budget_tokens")),
                                       "review_status": manifest.get("review_status") or manifest.get("quality_status"),
                                       "runtime_run_id": ((manifest.get("runtime") or {}).get("run_id") or
                                                          ((manifest.get("runtime_evidence") or {}).get("manifest") or {}).get("run_id"))}
            for case in CASES:
                case_id = case["case_id"]
                case_dir = run_dir if legacy else run_dir / case_id
                req_path = _find_request(case_dir, case_id, legacy)
                result, result_path = _find_result(case_dir, case_id, legacy, manifest)
                artifacts = _artifact_paths(case_dir, case_id, case["artifact_type"], legacy,
                                            result, result_path)
                request = _request_record(req_path)
                if request:
                    request_groups.setdefault(case_id, []).append({"slot": slot, **request})
                usage = (result or {}).get("usage") or {}
                observations = (result or {}).get("stream_observation") or {}
                manifest_config = (((manifest.get("runtime") or {}).get("runtime_config") or {}).get("configuration") or
                                   ((manifest.get("runtime_config") or {}).get("configuration") or {}))
                reported_budget = (result or {}).get("requested_reasoning_budget_tokens")
                if reported_budget is None:
                    reported_budget = (request or {}).get("reasoning_budget_tokens")
                if reported_budget is None:
                    reported_budget = manifest.get("reasoning_budget_tokens",
                                                   (manifest.get("request_defaults") or {}).get("reasoning_budget_tokens",
                                                   manifest_config.get("reasoning_budget_tokens")))
                if reported_budget == -1:
                    budget_label = "-1 (unlimited)"
                elif reported_budget is None and level == "none":
                    budget_label = "omitted / none"
                elif reported_budget is None:
                    budget_label = "not explicit"
                else:
                    budget_label = str(reported_budget)
                row = {
                    "arm": arm, "level": level, "template_effort": level_spec["template_effort"],
                    "requested_reasoning_budget_tokens": reported_budget,
                    "budget_label": budget_label, "run_id": manifest.get("run_id"),
                    "case_id": case_id, "artifact_type": case["artifact_type"],
                    "status": _status(result, result_path, case_dir, manifest),
                    "user_quality_judgement": "pending",
                    "request": request,
                    "requested_effort": (result or {}).get("requested_effort") or
                                        (request or {}).get("reasoning_effort") or
                                        manifest.get("requested_effort") or level_spec["runner_effort"],
                    "http_status": (result or {}).get("http_status"),
                    "failure": (result or {}).get("failure"),
                    "completion_state": (result or {}).get("completion_state"),
                    "finish_reason": (result or {}).get("finish_reason"),
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "cached_tokens_reported_untrusted": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
                    "answer_characters": observations.get("answer_characters"),
                    "reasoning_characters": observations.get("reasoning_characters"),
                    "wall_seconds": (result or {}).get("wall_seconds"),
                    "first_answer_seconds": (result or {}).get("first_content_seconds",
                                             (result or {}).get("first_answer_seconds")),
                    "artifacts": {key: str(value.resolve()) if value else None
                                  for key, value in artifacts.items()},
                    "evidence_source": "reused_existing_candidate_none" if legacy else "native_matrix",
                    "reused_evidence_reference": str(legacy_none) if legacy else None,
                }
                rows.append(row)

    parity = {}
    for case_id in (case["case_id"] for case in CASES):
        observed = request_groups.get(case_id, [])
        hashes = {item["normalized_sha256"] for item in observed}
        prompt_hash_sets = {tuple(item["user_prompt_sha256"]) for item in observed}
        parity[case_id] = {
            "expected_slots": len(ARMS) * len(LEVELS),
            "observed_requests": len(observed),
            "same_request_except_effort_and_budget": len(hashes) <= 1 and len(observed) > 1,
            "all_observed_user_prompts_exact": len(prompt_hash_sets) <= 1 and len(observed) > 1,
            "normalized_request_sha256_values": sorted(hashes),
            "prompt_sha256_values": [list(value) for value in sorted(prompt_hash_sets)],
            "matrix_complete": len(observed) == len(ARMS) * len(LEVELS),
            "note": "Only reasoning_effort and reasoning_budget_tokens are excluded from request equality; exact user messages and all other request fields must match.",
        }

    return {
        "schema_version": 1,
        "report_kind": "visual_manual_effort_matrix",
        "matrix_root": str(matrix_root), "legacy_none_source": str(legacy_none),
        "arms": list(ARMS), "levels": LEVELS,
        "cases": list(CASES), "expected_slots": expected,
        "captured_results": sum(row["status"] == "captured_user_review_pending" for row in rows),
        "partial_or_failed_results": sum(row["status"] in ("partial_request_only", "partial_or_non_natural_finish",
                                                               "request_failed_or_http_error") for row in rows),
        "user_cancelled_partial_results": sum(row["status"] == "user_cancelled_partial" for row in rows),
        "pending_results": sum(row["status"] == "pending" for row in rows),
        "pending_user_quality_judgement": True,
        "speed_claims_allowed": False,
        "experiment_scope": "Primary matrix contains only visual-01 SVG across both services and four effort levels. The candidate/none slot reuses the existing exact candidate none run by reference, without copying or changing originals; other slots come from the NATIVE matrix root. Budget values are read from each exact request/run manifest; -1 means unlimited. Bounded thinking-budget runs remain supplementary and never fill primary slots.",
        "reused_evidence": [{"slot": "candidate/none", "source": str(legacy_none),
                             "copied_or_modified": False,
                             "note": "Existing candidate NONE reference reused; no reasoning budget required for effort=none."}],
        "supplementary_bounded_visuals": {"matrix_root": str(DEFAULT_BOUNDED_MATRIX.resolve()),
                                          "legacy_none_source": str(legacy_none),
                                          "note": "Prior bounded SVG and animation diagnostic; excluded from the NATIVE primary matrix."},
        "completed_animation_archive": str(Path(completed_animation_archive).resolve()) if completed_animation_archive else None,
        "accounting_caveat": "Do not use cached_tokens or API response timings for speed claims in this diagnostic: the service reports continuation-prefix reuse as OpenAI cached_tokens and exposes final-generation-stage timings for multi-stage requests. HTTP wall and first-answer timings are retained as observations, not winning-speed evidence.",
        "request_parity": parity,
        "run_manifests": run_manifests,
        "rows": rows,
    }


def _link(value: str | None, output_parent: Path, label: str) -> str:
    if not value:
        return "—"
    target = Path(value)
    try:
        relative = os.path.relpath(target, output_parent).replace(os.sep, "/")
    except ValueError:
        relative = str(target)
    href = quote(relative, safe="/.:_-~")
    return f'<a href="{html.escape(href, quote=True)}">{html.escape(label)}</a>'


def render_html(report: dict, output_path: Path) -> str:
    out_parent = output_path.resolve().parent
    lines = ["<!doctype html>", '<html lang="zh-CN"><meta charset="utf-8">',
             "<title>Pelican visuals · manual comparison matrix</title>",
             "<style>body{font:14px system-ui;margin:2rem;color:#222}table{border-collapse:collapse;width:100%;margin:1rem 0}th,td{border:1px solid #bbb;padding:.45rem;vertical-align:top}th{background:#eee}code{word-break:break-all}.warning{background:#fff4d5;padding:.8rem}</style>",
             "<body><h1>Pelican visuals · manual comparison matrix</h1>",
             '<p class="warning"><b>诊断用途：</b>本轮不作速度优胜判断。API cached_tokens 在 reasoning-budget 多阶段请求中混入 continuation prefix reuse；响应 timing 只代表最后 GEN 阶段。HTTP 总耗时和首个正文时间仅列作观测。</p>',
             "<p>主矩阵仅包含 visual-01 SVG，覆盖 baseline/candidate × none/low/medium/high。candidate/none 复用既有 NONE 原始证据路径，不复制或修改；其余档位来自 NATIVE 矩阵目录。原始 prompt 必须逐字一致；请求配平只允许 reasoning_effort 和 reasoning_budget_tokens 不同。预算来自请求/manifest 原值；-1 表示无限；none 不要求显式预算。高档模板显示为 xhigh（collector 请求标签 high）。所有质量判断仍待用户人工完成。</p>",
             "<h2>已观测请求配平</h2><ul>"]
    for case_id, value in report["request_parity"].items():
        lines.append(f"<li>{html.escape(case_id)}：观测 {value['observed_requests']}/{value['expected_slots']} 份请求；"
                     f"已观测请求（排除 effort/budget）一致：{value['same_request_except_effort_and_budget']}；"
                     f"prompt逐字一致：{value['all_observed_user_prompts_exact']}；全矩阵完整：{value['matrix_complete']}。</li>")
    supplement = report["supplementary_bounded_visuals"]
    lines += ["</ul><h2>补充材料（不进入主矩阵）</h2><ul>",
              f"<li>{_link(str(Path(supplement['matrix_root']) / 'index.html'), out_parent, '旧 bounded visual 对照索引')}</li>",
              f"<li>{_link(str(Path(supplement['legacy_none_source']) / 'index.html'), out_parent, 'candidate/none 原始索引（主矩阵复用来源）')}</li>"]
    if report.get("completed_animation_archive"):
        lines.append(f"<li>{_link(report['completed_animation_archive'], out_parent, '已完成动画归档')}</li>")
    lines += ["</ul><h2>逐服务 × effort × SVG 记录</h2>",
              "<table><thead><tr><th>服务</th><th>档位 / budget</th><th>用例</th><th>采集状态</th><th>请求 / prompt</th><th>HTTP / finish</th><th>tokens / chars</th><th>wall / 首个正文</th><th>原始证据与产物</th><th>质量判断</th></tr></thead><tbody>"]
    for row in report["rows"]:
        effort = f"{row['level']} / {row['template_effort']} · budget={row['budget_label']}"
        request = row.get("request") or {}
        parity = "—"
        if request:
            parity = (f"effort={html.escape(str(request.get('reasoning_effort')))}"
                      f" budget={html.escape(str(request.get('reasoning_budget_tokens')))}<br>"
                      f"prompt={html.escape(','.join(request.get('user_prompt_sha256', [])))}<br>"
                      f"request¹={html.escape(request['normalized_sha256'][:12])}")
        usage = f"prompt {row.get('prompt_tokens') or '—'} / completion {row.get('completion_tokens') or '—'}"
        chars = f"answer {row.get('answer_characters') or '—'} / reasoning {row.get('reasoning_characters') or '—'} chars"
        wall = f"{row.get('wall_seconds') if row.get('wall_seconds') is not None else '—'} s"
        first = f"{row.get('first_answer_seconds') if row.get('first_answer_seconds') is not None else '—'} s"
        finish = f"HTTP {row.get('http_status') if row.get('http_status') is not None else '—'}<br>"
        finish += f"{html.escape(str(row.get('completion_state') or '—'))} / {html.escape(str(row.get('finish_reason') or '—'))}"
        links = row["artifacts"]
        link_items = [
            _link(links.get("artifact"), out_parent, "原始 SVG/HTML"),
            _link(links.get("raw_text"), out_parent, "原始正文"),
            _link(links.get("reasoning"), out_parent, "原始 reasoning"),
            _link(links.get("raw_response"), out_parent, "原始响应/事件（可能含 reasoning）"),
            _link(links.get("request"), out_parent, "request.json"),
            _link(links.get("engine_trace"), out_parent, "engine 日志区间"),
            _link(links.get("result"), out_parent, "结果 metadata"),
        ]
        evidence = "<br>".join(item for item in link_items if item != "—") or "—"
        status = html.escape(row["status"])
        if row.get("reused_evidence_reference"):
            status += "<br>复用既有 NONE 原始证据（链接引用，未复制/修改）"
        if row.get("failure"):
            status += "<br>" + html.escape(str(row["failure"]))
        lines.append("<tr>" +
                     f"<td>{html.escape(row['arm'])}<br><code>{html.escape(str(row.get('run_id') or 'pending'))}</code></td>" +
                     f"<td>{html.escape(effort)}<br>requested={html.escape(str(row.get('requested_effort')))}</td>" +
                     f"<td>{html.escape(row['case_id'])} ({html.escape(row['artifact_type'])})</td>" +
                     f"<td>{status}</td><td>{parity}</td><td>{finish}</td>" +
                     f"<td>{html.escape(usage)}<br>{html.escape(chars)}</td>" +
                     f"<td>{html.escape(wall)}<br>first answer {html.escape(first)}</td>" +
                     f"<td>{evidence}</td><td>用户人工判断待进行</td></tr>")
    lines += ["</tbody></table>",
              "<p>¹ 请求哈希仅比较非 effort/budget 字段；不得把服务标签当成运行模式证明。cached_tokens 与服务响应 timing 保留在原始响应中，但本报告不将其用于速度结论。</p>",
              "<p>本报告生成器仅汇总现存 metadata 并链接原始文件；不渲染模型产物，也不进行自动质量评分。</p></body></html>"]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix-root", type=Path, default=DEFAULT_MATRIX,
                        help="NATIVE arm/effort matrix root; default visual_manual_native_comparison_20261004")
    parser.add_argument("--legacy-none", type=Path, default=DEFAULT_LEGACY_NONE,
                        help="existing candidate NONE evidence reused by reference in the primary matrix")
    parser.add_argument("--completed-animation-archive", type=Path,
                        help="optional existing completed-animation archive to link as supplementary material")
    parser.add_argument("--out", type=Path, required=True, help="new HTML report path; JSON is written beside it")
    args = parser.parse_args(argv)
    json_path = args.out.with_suffix(".json")
    if args.out.exists() or json_path.exists():
        parser.error("output already exists; choose a new report path")
    report = build_matrix(args.matrix_root, args.legacy_none, args.completed_animation_archive)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_html(report, args.out), encoding="utf-8")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("wrote", args.out.resolve())
    print("wrote", json_path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
