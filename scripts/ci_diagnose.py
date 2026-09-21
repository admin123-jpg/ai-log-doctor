"""
CI/CD 流水线日志诊断脚本
=================================
作用：GitHub Actions 流水线失败时调用它，对失败日志做 AI 诊断，
     输出一份 Markdown 报告，供后续自动评论到 PR。

用法：
    python scripts/ci_diagnose.py --log ci-failed.log --out diagnosis.md

这个脚本是「CI/CD + AI」结合的关键：
流水线不再只是报红，而是能告诉你「为什么红、怎么修」。
"""

import argparse
import sys
from pathlib import Path

# 让脚本能 import 到 src 包（从项目根目录运行）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pipeline import analyze_text  # noqa: E402


def render_markdown(result, log_text: str, run_url: str = "") -> str:
    """把诊断结果渲染成 Markdown（用于 PR 评论）"""
    lines = ["## AI 流水线失败诊断", ""]

    if run_url:
        lines.append(f"流水线运行记录：{run_url}")
        lines.append("")

    if not result:
        lines.append("未在日志中检测到已知异常关键字，请人工查看完整日志。")
        lines.append("")
        lines.append("<details><summary>查看日志片段</summary>")
        lines.append("")
        lines.append("```")
        lines.append(log_text[-2000:])
        lines.append("```")
        lines.append("</details>")
        return "\n".join(lines)

    severity_map = {"high": "高危", "medium": "中危", "low": "低危"}
    lines.append(f"**严重程度**：{severity_map.get(result.get('severity'), result.get('severity'))}　　"
                 f"**命中关键字**：`{result.get('matched_keyword', '-')}`")
    lines.append("")
    lines.append("### 根因分析")
    lines.append("")
    lines.append(result.get("root_cause", "（未给出）"))
    lines.append("")
    lines.append("### 处置建议")
    lines.append("")
    for i, s in enumerate(result.get("suggestions", []), 1):
        lines.append(f"{i}. {s}")
    lines.append("")

    refs = result.get("used_references", [])
    if refs:
        lines.append("### 知识库参考来源（RAG 检索）")
        lines.append("")
        for r in refs:
            lines.append(f"- `{r['source']}` · {r['title']}（相似度 {r['score']}）")
        lines.append("")

    lines.append("<details><summary>查看异常日志上下文</summary>")
    lines.append("")
    lines.append("```")
    lines.append(result.get("raw_log", "")[:2000])
    lines.append("```")
    lines.append("</details>")
    lines.append("")
    lines.append("---")
    lines.append("🤖 本评论由 AI 容器日志医生自动生成，仅供参考。")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="CI 日志 AI 诊断")
    parser.add_argument("--log", required=True, help="失败日志文件路径")
    parser.add_argument("--out", default="diagnosis.md", help="输出 Markdown 文件路径")
    parser.add_argument("--run-url", default="", help="流水线运行链接（写进报告里）")
    args = parser.parse_args()

    log_path = Path(args.log)
    if not log_path.exists():
        print(f"日志文件不存在：{log_path}")
        return 1

    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    if not log_text.strip():
        print("日志为空，跳过诊断")
        return 0

    result = analyze_text(
        text=log_text, source="ci", service="github-actions", save=False
    )

    md = render_markdown(result, log_text, args.run_url)
    Path(args.out).write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
