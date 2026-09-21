"""前端页面一致性校验：确保美化后没有破坏任何功能。

检查项：
  1. 原有 DOM id 是否全部保留
  2. JS 里 $() 取的 id 是否都存在
  3. 所有 JS 函数是否完整
  4. onclick 调用的函数是否都已定义
  5. JS 模板用到的 CSS 类是否都在样式里定义
  6. JS 内联样式引用的 CSS 变量是否已定义
  7. 关键交互逻辑未被改动
  8. 标签闭合完整性
  9. 零外部依赖
"""
import re
import sys
from pathlib import Path

HTML = Path(__file__).resolve().parent.parent / "web" / "index.html"
html = HTML.read_text(encoding="utf-8")
css = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
js = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
body = re.search(r"<body>(.*?)</body>", html, re.S).group(1)

failed = []


def check(cond, msg):
    print(("  [OK] " if cond else "  [!!] ") + msg)
    if not cond:
        failed.append(msg)


print("=== 1. 原有 DOM id 是否全部保留 ===")
REQUIRED_IDS = [
    "badges", "cards", "keyword", "statusFilter", "severityFilter",
    "listArea", "modal", "modalContent", "toast",
]
for i in REQUIRED_IDS:
    check('id="%s"' % i in body, 'id="%s"' % i)

print("\n=== 2. JS 里用 $() 取的 id 是否都存在 ===")
used = sorted(set(re.findall(r"\$\('([^']+)'\)", js)))
for i in used:
    check('id="%s"' % i in body, "$('%s') -> 有对应元素" % i)

print("\n=== 3. 所有 JS 函数是否完整 ===")
FUNCS = ["toast", "api", "loadBadges", "loadStats", "loadList", "escapeHtml",
         "showDetail", "closeModal", "resolve", "remove", "scanNow", "seedData", "loadAll"]
for f in FUNCS:
    check(re.search(r"(async )?function %s\s*\(" % f, js) is not None, "function %s()" % f)

print("\n=== 4. onclick 调用的函数是否都已定义 ===")
# 排除 JS 关键字（如 onclick="if(event.target===this)closeModal()" 里的 if）
KEYWORDS = {"if", "for", "while", "return", "event"}
ons = sorted(set(re.findall(r'onclick="(\w+)\(', html + js)) - KEYWORDS)
for f in ons:
    check(re.search(r"(async )?function %s\s*\(" % f, js) is not None, "onclick=%s()" % f)

print("\n=== 5. JS 模板用到的 CSS 类是否都在样式里定义 ===")
js_classes = set()
for m in re.findall(r'class="([^"$]*)"', js):
    js_classes.update(m.split())
for c in sorted(js_classes - {""}):
    if c.startswith("sev-"):
        check("tr.%s" % c in css, ".%s (行严重度色条)" % c)
    else:
        check("." + c in css, "." + c)

print("\n=== 6. JS 内联样式引用的 CSS 变量是否已定义 ===")
for v in sorted(set(re.findall(r"var\((--[\w-]+)\)", js))):
    check("%s:" % v in css, "%s 已定义" % v)

print("\n=== 7. 关键交互逻辑未被改动 ===")
check("fetch(path, options)" in js, "api() 仍是相对路径 fetch")
check("JSON.stringify({ status: 'resolved' })" in js, "resolve() 的 PUT 逻辑")
check("method: 'DELETE'" in js, "remove() 的 DELETE 逻辑")
check("/api/seed" in js and "method: 'POST'" in js, "seedData() 调 /api/seed")
check("/api/scan" in js and "method: 'POST'" in js, "scanNow() 调 /api/scan")
check("confirm(" in js.split("async function seedData")[1], "seedData() 的二次确认仍在")
check("params.append('keyword'" in js, "搜索参数 keyword")
check("params.append('status'" in js, "筛选参数 status")
check("params.append('severity'" in js, "筛选参数 severity")
check("limit: 50" in js, "列表 limit=50")
check("Escape" in js, "ESC 关闭弹窗")

print("\n=== 8. 标签闭合完整性 ===")
for tag in ["html", "head", "body", "style", "script"]:
    o = len(re.findall(r"<%s[ >]" % tag, html))
    c = len(re.findall(r"</%s>" % tag, html))
    check(o == c, "<%s> 开 %d / 闭 %d" % (tag, o, c))

print("\n=== 9. 零外部依赖（断网也能用）===")
ext = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
check(not ext, "没有任何外部 CDN 引用 %s" % (ext if ext else ""))

print("\n" + "=" * 52)
if failed:
    print("  结果：存在 %d 个失败项" % len(failed))
    for m in failed:
        print("    - " + m)
else:
    print("  结果：全部通过，可以部署")
print("=" * 52)
print("\n文件大小：%d 字节 / %d 行" % (len(html.encode("utf-8")), len(html.splitlines())))

sys.exit(1 if failed else 0)
