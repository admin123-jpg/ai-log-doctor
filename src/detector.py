"""
异常检测模块
=================================
作用：判断一段日志里是否包含异常，并给出严重程度。

为什么单独拆一个模块？
  「采集日志」和「判断有没有问题」是两件事，拆开后：
  - 以后想换成 AI 来检测异常，只改这个文件
  - 想加新关键字，也只改这个文件
"""

import re
from dataclasses import dataclass
from typing import Optional

from .config import Config


@dataclass
class Detection:
    """一次检测的结果"""
    hit: bool                 # 是否命中异常
    keyword: str = ""         # 命中的是哪个关键字
    severity: str = "medium"  # 严重程度：low / medium / high
    snippet: str = ""         # 异常上下文（关键字前后几行，方便 AI 分析）


# 严重程度规则：越靠前优先级越高（按顺序匹配，命中就返回）
# 格式：(正则表达式, 严重程度)
SEVERITY_RULES = [
    (r"Out of memory|OOMKilled|OOM|内存溢出", "high"),
    (r"Connection refused|拒绝连接|ECONNREFUSED", "high"),
    (r"Traceback \(most recent call last\)|Exception|Fatal|fatal", "high"),
    (r"panic:|core dumped|segfault", "high"),
    (r"Permission denied|Access denied|权限不足", "high"),
    (r"No such file or directory|文件不存在", "medium"),
    (r"502|503|504|Bad Gateway|Service Unavailable", "medium"),
    (r"Timeout|timed out|timeout|超时", "medium"),
    (r"ERROR|Error|error|FAILED|Failed|failed", "medium"),
    (r"WARN|Warning|warning", "low"),
]


def _build_keyword_pattern(keywords: list) -> re.Pattern:
    """
    把关键字列表编译成一个正则表达式。
    re.escape 的作用是：把关键字里的特殊字符（比如 . * ( ）转义掉，
    避免它们被当成正则语法。

    纯数字关键字要额外加词边界（\\b）：
      502 / 503 / 504 这类 HTTP 状态码如果不设边界，会被日志里
      任何包含这三个数字的随机串误命中 —— 例如时间戳的微秒部分
      "2026-09-03T07:39:35.635045Z" 里就藏着 504。
      实测在真实 MySQL 日志上，这一个问题就造成了上百条误报。
    """
    parts = []
    for k in keywords:
        esc = re.escape(k)
        # 纯数字（HTTP 状态码）：必须前后是边界，才当状态码看
        if k.isdigit():
            esc = r"\b" + esc + r"\b"
        # OOM 这类短词同样容易被 ROOM / ZOOM / BLOOM 误命中
        elif k.isalpha() and k.isupper() and len(k) <= 4:
            esc = r"\b" + esc + r"\b"
        parts.append(esc)
    return re.compile("|".join(parts))


def extract_context(text: str, keyword: str, before: int = 3, after: int = 5) -> str:
    """
    截取关键字前后若干行，作为「异常上下文」。

    为什么要截取？
      一次采集可能有一百行日志，全部丢给 AI 既浪费钱又容易被无关信息干扰。
      只取出错位置附近的几行，AI 分析得更准。
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if keyword.lower() in line.lower():
            start = max(0, i - before)
            end = min(len(lines), i + after + 1)
            return "\n".join(lines[start:end])
    # 没找到就返回最后几行（异常通常在末尾）
    return "\n".join(lines[-10:])


def judge_severity(text: str) -> str:
    """根据日志内容判断严重程度：high / medium / low"""
    for pattern, level in SEVERITY_RULES:
        if re.search(pattern, text):
            return level
    return "medium"


def detect(text: str, keywords: Optional[list] = None) -> Detection:
    """
    检测一段日志是否包含异常。

    参数：
      text      - 日志全文
      keywords  - 要匹配的关键字列表，不传就用 Config.ERROR_KEYWORDS

    返回 Detection 对象，用 .hit 判断是否命中。
    """
    if not text or not text.strip():
        return Detection(hit=False)

    keywords = keywords or Config.ERROR_KEYWORDS
    pattern = _build_keyword_pattern(keywords)
    match = pattern.search(text)

    if not match:
        return Detection(hit=False)

    keyword = match.group(0)
    snippet = extract_context(text, keyword)

    return Detection(
        hit=True,
        keyword=keyword,
        severity=judge_severity(snippet),
        snippet=snippet,
    )


def detect_lines(lines: list, keywords: Optional[list] = None) -> list:
    """
    按行检测：找出所有命中关键字的行。
    返回一个列表，元素形如 {"line_no": 12, "content": "...", "severity": "high"}
    """
    keywords = keywords or Config.ERROR_KEYWORDS
    pattern = _build_keyword_pattern(keywords)
    result = []

    for idx, line in enumerate(lines, start=1):
        match = pattern.search(line)
        if match:
            result.append({
                "line_no": idx,
                "content": line.strip(),
                "keyword": match.group(0),
                "severity": judge_severity(line),
            })
    return result


def split_error_blocks(text: str, keywords: Optional[list] = None,
                       gap: int = 5, before: int = 2, after: int = 6) -> list:
    """
    把一个日志文件切成多个「异常块」。

    为什么要切？
      一个 redis.log 里可能同时存在 MISCONF、fork 失败、maxmemory 三个不同的问题，
      如果只取第一个命中位置分析，就会漏掉后面两个。

    切分逻辑：
      1. 找出所有命中关键字的行号
      2. 行号间隔 <= gap（默认 5 行）的算作同一个异常块
         （这样 Java 的一整段堆栈会被合并成一个块，而不是被切成好几段）
      3. 每个块向外扩展 before/after 行，保留上下文

    返回字符串列表，每个元素是一个异常块的文本。
    """
    lines = text.splitlines()
    keywords = keywords or Config.ERROR_KEYWORDS
    pattern = _build_keyword_pattern(keywords)

    hit_idx = [i for i, line in enumerate(lines) if pattern.search(line)]
    if not hit_idx:
        return []

    # 把相近的命中行归为一组
    groups = [[hit_idx[0]]]
    for idx in hit_idx[1:]:
        if idx - groups[-1][-1] <= gap:
            groups[-1].append(idx)
        else:
            groups.append([idx])

    # 每组向外扩展上下文，拼成一个块
    blocks = []
    for group in groups:
        start = max(0, group[0] - before)
        end = min(len(lines), group[-1] + after + 1)
        blocks.append("\n".join(lines[start:end]))

    return blocks


# 归一化指纹用到的替换规则：(正则, 占位符)
# 目的：把日志里「每次都变」的部分抹掉，只留下稳定的模板。
# 顺序有意义 —— 时间戳必须先于「纯数字」替换，否则会被拆得认不出来。
_FP_PATTERNS = [
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?Z?"), "<TS>"),
    (re.compile(r"\d{2}-[A-Za-z]{3}-\d{4} \d{2}:\d{2}:\d{2}(?:[.,]\d+)?"), "<TS>"),
    (re.compile(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}"), "<TS>"),
    (re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<TS>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<HEX>"),
    (re.compile(r"\b[0-9a-fA-F]{12,}\b"), "<ID>"),
    (re.compile(r"\d+"), "N"),
]


def fingerprint(text: str) -> str:
    """
    把一段日志归一化成「模板指纹」，用于判断两段日志是不是同一个故障。

    为什么不能直接对原文做哈希？
      因为每行日志都带时间戳，原文永远不一样。
      实测：153 条诊断记录按原文去重还剩 151 条，看起来"没有重复"，
      但按归一化模板去重只剩 20 条 —— 真实故障种类被高估了 7 倍。

    做法：把时间戳、十六进制、纯数字这些"每次都变"的部分替换成占位符，
    剩下的就是稳定的"模板"。这正是 ELK 里 fingerprint 处理器的思路。
    """
    s = text
    for pat, rep in _FP_PATTERNS:
        s = pat.sub(rep, s)
    return s.strip()


def split_by_error_patterns(text: str, before: int = 2, after: int = 3,
                            dedup: bool = True) -> list:
    """
    按「错误模式」切分日志：每种不同的错误模式各取一段上下文。

    和 split_error_blocks 的区别（重要）：
      split_error_blocks 按「行间隔」合并，日志里错误密集时会把
      好几个不同的问题合并成一大段，导致只诊断出第一个问题。

      这个函数反过来：拿已知的错误模式去日志里逐个匹配，
      每种模式取第一次命中的位置作为一段，且段与段不重叠。
      这样一个 redis.log 里的 MISCONF、fork 失败、maxmemory、
      主从断开这四个问题都能被分别诊断出来。

    dedup=True 时做「按模板聚合」：
      同一个故障反复出现时只保留一段，并在段落开头标出总次数。
      真实日志里一个故障重复上千次是常态 —— 实测某台机器的 MySQL
      错误日志里，同一个「端口被占用」故障占了全部记录的 132/153。
      如果不去重，面板会被同一条结论刷屏，真正不同的问题反而被淹没。

    返回字符串列表，每个元素是一段异常上下文。
    被聚合掉的重复段落会在首行带一行注释说明出现了多少次。
    """
    # 延迟导入：避免模块加载时的循环依赖
    from .llm import MOCK_RULES

    # 预编译所有错误模式
    compiled = []
    for rule in MOCK_RULES:
        try:
            compiled.append(re.compile(rule[0], re.I))
        except re.error:
            continue

    lines = text.splitlines()
    blocks = []
    seen = {}          # 指纹 -> [出现次数, 在 blocks 中的下标]
    last_end = -1      # 上一个块结束的行号

    # 按「日志行的顺序」扫描，而不是按规则顺序。
    # 如果按规则顺序，排在最前面的泛化规则（比如 Timeout）会先取走中间一段，
    # 把后面更具体的错误行一并覆盖掉，导致漏检。
    # 按行顺序扫描则能保证块的先后与日志一致，且不会跳跃覆盖。
    for i, line in enumerate(lines):
        if i < last_end:                     # 这行已被上一个块包含
            continue
        if not any(regex.search(line) for regex in compiled):
            continue

        start = max(0, i - before)
        end = min(len(lines), i + after + 1)
        block = "\n".join(lines[start:end])
        last_end = end

        if dedup:
            fp = fingerprint(block)
            if fp in seen:
                seen[fp][0] += 1             # 同一个故障，只计数不再重复生成
                continue
            seen[fp] = [1, len(blocks)]

        blocks.append(block)

    # 把「重复次数」回填到每个聚合块的开头
    if dedup:
        for cnt, idx in seen.values():
            if cnt > 1:
                blocks[idx] = (f"# 同类异常在本次日志中共出现 {cnt} 次"
                               f"（已按归一化模板聚合，以下为其中一次的完整上下文）\n"
                               + blocks[idx])

    return blocks
