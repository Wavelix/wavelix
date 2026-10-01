"""
统计 GitHub 用户所有仓库中各语言的代码行数（cloc 的 code 行，不含空行/注释），
并生成浅色 / 深色两张 SVG 卡片，用于 profile README（配合 <picture> 跟随 GitHub 主题切换）。

环境变量：
  GH_USER        GitHub 用户名（必填）
  GH_TOKEN       Token，用于提高 API 限额；若要包含私有仓库需用 PAT 并设 INCLUDE_PRIVATE=true
  INCLUDE_PRIVATE  "true" 时通过 /user/repos 获取（需 PAT，带 repo 权限）
  INCLUDE_FORKS    "true" 时包含 fork 仓库（默认不包含）
  INCLUDE_ARCHIVED "false" 时跳过已归档仓库（默认包含）
  EXCLUDE_LANGS  逗号分隔的不统计语言
  EXCLUDE_REPOS  逗号分隔的不统计仓库名
  TOP_N          显示前几种语言，其余合并为 Other（默认 8）
  OUTPUT         浅色卡片输出路径（默认 lang-loc.svg）
  OUTPUT_DARK    深色卡片输出路径（默认 lang-loc-dark.svg）
"""
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import urllib.request
from html import escape

USER = os.environ["GH_USER"]
TOKEN = os.environ.get("GH_TOKEN", "")
INCLUDE_PRIVATE = os.environ.get("INCLUDE_PRIVATE", "false").lower() == "true"
INCLUDE_FORKS = os.environ.get("INCLUDE_FORKS", "false").lower() == "true"
INCLUDE_ARCHIVED = os.environ.get("INCLUDE_ARCHIVED", "true").lower() == "true"
TOP_N = int(os.environ.get("TOP_N", "8"))
OUTPUT = os.environ.get("OUTPUT", "lang-loc.svg")
OUTPUT_DARK = os.environ.get("OUTPUT_DARK", "lang-loc-dark.svg")

DEFAULT_EXCLUDE = "JSON,Markdown,YAML,XML,SVG,Text,TOML,INI,CSV,reStructuredText"
EXCLUDE_LANGS = {s.strip() for s in os.environ.get("EXCLUDE_LANGS", DEFAULT_EXCLUDE).split(",") if s.strip()}
EXCLUDE_REPOS = {s.strip() for s in os.environ.get("EXCLUDE_REPOS", "").split(",") if s.strip()}
EXCLUDE_DIRS = "node_modules,vendor,dist,build,target,.venv,venv,__pycache__,third_party"

# cloc 的语言名 -> 卡片上显示的语言名（把同一种语言的不同 cloc 分类合并成一行）
LANG_ALIASES = {
    "C/C++ Header": "C++",
    "Bourne Shell": "Shell", "Bourne Again Shell": "Shell", "zsh": "Shell",
}

# GitHub linguist 常用语言颜色，未列出的按名字哈希生成
COLORS = {
    "Python": "#3572A5", "JavaScript": "#f1e05a", "TypeScript": "#3178c6",
    "Java": "#b07219", "C": "#555555", "C++": "#f34b7d",
    "C#": "#178600", "Go": "#00ADD8", "Rust": "#dea584", "Ruby": "#701516",
    "PHP": "#4F5D95", "Swift": "#F05138", "Kotlin": "#A97BFF", "Dart": "#00B4AB",
    "HTML": "#e34c26", "CSS": "#663399", "SCSS": "#c6538c", "Vuejs Component": "#41b883",
    "Shell": "#89e051", "Lua": "#000080", "MATLAB": "#e16737", "TeX": "#3D6117",
    "Scala": "#c22d40", "Haskell": "#5e5086", "R": "#198CE7", "Jupyter Notebook": "#DA5B0B",
    "Objective-C": "#438eff", "Perl": "#0298c3", "Elixir": "#6e4a7e", "Zig": "#ec915c",
    "SQL": "#e38c00", "Dockerfile": "#384d54", "make": "#427819", "CMake": "#DA3434",
    "Other": "#9e9e9e",
}

THEMES = {
    "light": {"title": "#2f80ed", "sub": "#6a737d", "text": "#666", "track": "#d1d5da"},
    "dark": {"title": "#58a6ff", "sub": "#8b949e", "text": "#8b949e", "track": "#30363d"},
}


def color_for(lang: str) -> str:
    if lang in COLORS:
        return COLORS[lang]
    h = int(hashlib.md5(lang.encode()).hexdigest()[:6], 16)
    return f"#{h:06x}"


def api(url: str):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "lang-loc"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as r:
        return json.load(r)


def list_repos():
    repos, page = [], 1
    while True:
        if INCLUDE_PRIVATE:
            url = f"https://api.github.com/user/repos?affiliation=owner&per_page=100&page={page}"
        else:
            url = f"https://api.github.com/users/{USER}/repos?type=owner&per_page=100&page={page}"
        batch = api(url)
        if not batch:
            break
        repos.extend(batch)
        page += 1
    return [
        r for r in repos
        if (INCLUDE_FORKS or not r["fork"])
        and (INCLUDE_ARCHIVED or not r.get("archived", False))
        and r["name"] not in EXCLUDE_REPOS
    ]


def clone_url(repo) -> str:
    url = repo["clone_url"]
    if repo.get("private") and TOKEN:
        url = url.replace("https://", f"https://x-access-token:{TOKEN}@")
    return url


def count_repo(path: str) -> dict:
    """返回 {语言: [代码行数, 字节数]}。"""
    out = subprocess.run(
        ["cloc", "--by-file", "--json", "--quiet", f"--exclude-dir={EXCLUDE_DIRS}", path],
        capture_output=True, text=True,
    ).stdout
    if not out.strip():
        return {}
    result: dict = {}
    for fname, v in json.loads(out).items():
        if fname in ("header", "SUM") or not isinstance(v, dict):
            continue
        lang = v.get("language")
        if not lang or lang in EXCLUDE_LANGS:
            continue
        lang = LANG_ALIASES.get(lang, lang)
        try:
            size = os.path.getsize(fname)
        except OSError:
            size = 0
        e = result.setdefault(lang, [0, 0])
        e[0] += v.get("code", 0)
        e[1] += size
    return result


def collect():
    totals: dict = {}
    repo_count = 0
    with tempfile.TemporaryDirectory() as tmp:
        for repo in list_repos():
            dest = os.path.join(tmp, repo["name"])
            print(f"Cloning {repo['full_name']} ...")
            res = subprocess.run(
                ["git", "clone", "--depth", "1", "--quiet", clone_url(repo), dest],
                capture_output=True,
            )
            if res.returncode != 0:
                print("  skip (clone failed)")
                continue
            counted = count_repo(dest)
            if counted:
                repo_count += 1
            for lang, vals in counted.items():
                t = totals.setdefault(lang, [0, 0])
                t[0] += vals[0]
                t[1] += vals[1]
    return totals, repo_count


def sig3(x: float) -> str:
    """保留 3 位有效数字，保留末尾 0（如 2.00、37.7、205）。"""
    if x == 0:
        return "0"
    d = 2 - math.floor(math.log10(abs(x)))
    r = round(x, d)
    d = 2 - math.floor(math.log10(abs(r)))  # 四舍五入后可能进位，如 9.996 -> 10.0
    return f"{r:.{max(d, 0)}f}"


def scaled(x: float, units, base: int = 1000) -> str:
    """按单位缩放后保留 3 位有效数字；四舍五入进位到 1000 时自动换下一个单位。"""
    i = 0
    while i < len(units) - 1 and float(sig3(x)) >= base:
        x /= base
        i += 1
    return f"{sig3(x)}{units[i]}"


def fmt_lines(n: int) -> str:
    return f"{n} lines" if n < 1000 else f"{scaled(n, ['', 'k', 'M', 'B'])} lines"


def fmt_size(n: int) -> str:
    return "0 B" if n == 0 else scaled(n, [" B", " kB", " MB", " GB"])


def render_svg(totals: dict, repo_count: int, theme: dict) -> str:
    items = sorted(totals.items(), key=lambda kv: kv[1][0], reverse=True)
    total_lines = sum(v[0] for _, v in items) or 1
    total_size = sum(v[1] for _, v in items)
    top = [(k, v[0], v[1]) for k, v in items[:TOP_N]]
    rest = items[TOP_N:]
    if rest:
        top.append(("Other", sum(v[0] for _, v in rest), sum(v[1] for _, v in rest)))

    width, pad = 480, 8
    bar_x, bar_w, bar_y = pad, width - 2 * pad, 32
    first_y, row_h = 58, 16
    height = first_y + (len(top) - 1) * row_h + 10
    x_lines, x_size, x_pct = 300, 392, width - pad

    bar, x = [], 0.0
    for lang, n, _ in top:
        w = bar_w * n / total_lines
        bar.append(f'<rect x="{x:.2f}" y="0" width="{w:.2f}" height="7" fill="{color_for(lang)}"/>')
        x += w

    rows = []
    for i, (lang, n, size) in enumerate(top):
        y = first_y + i * row_h
        rows.append(
            f'<g transform="translate(0,{y})">'
            f'<circle cx="{pad + 10}" cy="-3.5" r="4" fill="{color_for(lang)}"/>'
            f'<text x="{pad + 24}" y="0" class="lang">{escape(lang)}</text>'
            f'<text x="{x_lines}" y="0" class="num" text-anchor="end">{fmt_lines(n)}</text>'
            f'<text x="{x_size}" y="0" class="num" text-anchor="end">{fmt_size(size)}</text>'
            f'<text x="{x_pct}" y="0" class="num" text-anchor="end">{100 * n / total_lines:.2f}%</text>'
            f'</g>'
        )

    repos = f"{repo_count} repo" + ("" if repo_count == 1 else "s")
    subtitle = f"{repos} · {fmt_lines(total_lines)} · {fmt_size(total_size)}"

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<style>
  text {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }}
  .title {{ font-size: 16px; font-weight: 600; fill: {theme["title"]}; }}
  .sub {{ font-size: 10px; fill: {theme["sub"]}; }}
  .lang {{ font-size: 10.5px; fill: {theme["text"]}; }}
  .num {{ font-size: 10px; fill: {theme["text"]}; font-variant-numeric: tabular-nums; }}
  .track {{ fill: {theme["track"]}; }}
</style>
<text x="{pad}" y="20" class="title">Most Used Languages</text>
<text x="{width - pad}" y="20" text-anchor="end" class="sub">{escape(subtitle)}</text>
<clipPath id="clip"><rect width="{bar_w}" height="7" rx="3.5"/></clipPath>
<g transform="translate({bar_x},{bar_y})" clip-path="url(#clip)"><rect class="track" width="{bar_w}" height="7"/>{"".join(bar)}</g>
{"".join(rows)}
</svg>'''


if __name__ == "__main__":
    totals, repo_count = collect()
    print(json.dumps(dict(sorted(totals.items(), key=lambda kv: -kv[1][0])), indent=2))
    if not totals:
        # 全部 clone / cloc 失败时不覆盖已有卡片，让 workflow 失败以便发现问题
        sys.exit("No code counted; keeping existing SVGs.")
    for path, theme in ((OUTPUT, THEMES["light"]), (OUTPUT_DARK, THEMES["dark"])):
        with open(path, "w", encoding="utf-8") as f:
            f.write(render_svg(totals, repo_count, theme))
        print(f"Wrote {path}")
