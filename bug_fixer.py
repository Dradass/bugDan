import html, json, os, re, shutil, subprocess, sys
from pathlib import Path

from tfs import add_workitem_comment, area_team, create_pull_request, find_base_branch, load_workitem, parse_team

MODEL = "claude-opus-5"
SLUG_MODEL = "claude-haiku-4-5"  # Модель для постфикса имени ветки по сути бага

BUG_BLOCK = """Ты работаешь в репозитории {repo} (ветка {branch}).
Описание бага:
---
{bug}
---
"""

RULES_BLOCK = """Правила написания кода, которые нужно соблюдать при исправлении:
---
{rules}
---
"""

FIX_TASK = """Задача:
1. Найди в коде причину бага.
2. Внеси исправление прямо в файлы репозитория. Изменения должны быть минимальными
   и в стиле окружающего кода. Не создавай вспомогательных файлов, не делай коммитов.
3. В ответе кратко опиши: причину бага, что изменено (файлы), риски и что проверить тестами.
   Этот текст станет описанием коммита, поэтому пиши без markdown-заголовков."""


def git(*args, cwd, input: str | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, input=input, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def check_repo(path: Path) -> tuple[str, str, str]:
    """Проверяет локальный репозиторий, ничего в нем не меняя. Возвращает (url, текущая ветка, remote)."""
    if not path.is_dir():
        sys.exit(f"Папка репозитория не найдена: {path}")
    try:
        top = Path(git("rev-parse", "--show-toplevel", cwd=path))
    except RuntimeError:
        sys.exit(f"{path} не является git-репозиторием")
    if top.resolve() != path.resolve():
        print(f"Внимание: корень репозитория - {top}", file=sys.stderr)

    remote = "origin"
    remotes = {}
    for line in git("remote", "-v", cwd=path).splitlines():
        name, remote_url, _ = line.split()
        remotes.setdefault(name, remote_url)
    url = remotes.get(remote) or sys.exit(f"В {path} нет remote {remote}")
    return url, git("rev-parse", "--abbrev-ref", "HEAD", cwd=path), remote


def remote_branches(repo: Path, remote: str) -> set[str]:
    return {line.split("refs/heads/", 1)[1]
            for line in git("ls-remote", "--heads", remote, cwd=repo).splitlines()}


def fetch_branch(repo: Path, remote: str, branch: str) -> str:
    """Забирает ветку с сервера и возвращает ее последний коммит. Локальные ветки не меняются."""
    try:
        git("fetch", remote, f"refs/heads/{branch}", cwd=repo)
    except RuntimeError as e:
        sys.exit(f"Не удалось получить ветку {branch} с {remote}: {e}")
    return git("rev-parse", "FETCH_HEAD", cwd=repo)


def decode_console(data: bytes) -> str:
    """Сообщения cmd.exe приходят в OEM-кодировке консоли (cp866), а не в UTF-8."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("oem" if sys.platform == "win32" else "latin-1", errors="replace")


def ask_claude(repo_path: Path, prompt: str, model: str,
               allow_edits: bool = False, tools: str | None = None, max_turns: int | None = None) -> dict:
    claude = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or sys.exit("claude CLI не найден в PATH")
    if tools is None:
        tools = "Read,Grep,Glob,Edit,Write" if allow_edits else "Read,Grep,Glob"
    args = [claude, "-p",
            "--output-format", "json",
            "--model", model,
            "--max-turns", str(max_turns or (60 if allow_edits else 40))]
    # Пустой список - отключить все инструменты (--allowedTools "" ничего не запрещает).
    args += ["--allowedTools", tools] if tools else ["--tools", ""]
    if allow_edits:
        args += ["--permission-mode", "acceptEdits"]
    proc = subprocess.run(args, input=prompt.encode("utf-8"), cwd=repo_path, capture_output=True)
    stdout = proc.stdout.decode("utf-8", errors="replace")
    try:
        res = json.loads(stdout)
    except json.JSONDecodeError:
        sys.exit(f"claude завершился с кодом {proc.returncode}\nstdout: {stdout}\nstderr: {decode_console(proc.stderr)}")
    if res.get("is_error"):
        sys.exit(f"Ошибка Claude: {res.get('result')}")
    return res


SLUG_PROMPT = """Придумай короткое имя git-ветки для исправления бага.
Требования: от 2 до 7 английских слов в kebab-case (только a-z, 0-9 и дефисы), передающих суть проблемы,
например fix-null-price-in-order-export. Без номера бага, без префиксов и кавычек.
Ответь только именем, одной строкой.

Название бага: {title}
Описание:
---
{bug}
---"""

TRANSLIT = dict(zip("абвгдеёзийклмнопрстуфхцыэ", "abvgdeeziyklmnoprstufhcye")) | {
    "ж": "zh", "ч": "ch", "ш": "sh", "щ": "sch", "ю": "yu", "я": "ya", "ъ": "", "ь": ""}


def kebab_words(text: str) -> list[str]:
    text = "".join(TRANSLIT.get(ch, ch) for ch in text.lower())
    return re.findall(r"[a-z0-9]+", text)


def branch_slug(repo_path: Path, title: str, bug: str) -> str:
    """Постфикс ветки из 2-7 слов в kebab-case по сути бага. Если Claude не справился - транслит названия."""
    try:
        res = ask_claude(repo_path, SLUG_PROMPT.format(title=title, bug=bug[:4000]), SLUG_MODEL,
                         tools="", max_turns=1)
        words = kebab_words(res["result"].strip().splitlines()[0])
        if 2 <= len(words) <= 7:
            return "-".join(words)
        print(f"Claude предложил неподходящий постфикс ветки: {res['result'][:100]!r}", file=sys.stderr)
    except (SystemExit, Exception) as e:
        print(f"Не удалось получить постфикс ветки от Claude: {e}", file=sys.stderr)
    words = kebab_words(title)[:7] or ["bug"]
    return "-".join(words if len(words) >= 2 else ["fix", *words])


def branch_prefix(bug_url: str, area: str, base: str) -> str:
    # Префикс - команда из ссылки на доску: .../_boards/board/t/Vega/Stories/?workitem=43383 -> vega/43383-<slug>,
    # иначе команда из Area карточки: SmartInstruments\Vega -> vega/...
    # Если Area - корень проекта, берем префикс базовой ветки: vega/43375-x -> vega/...
    # Ветку внутри базовой (vega/x -> vega/x/43328) git создать не даст: имя уже занято файлом ref.
    team = parse_team(bug_url) or area_team(area)
    if team:
        return re.sub(r"[^\w.-]+", "-", team.lower()).strip("-.")
    return base.split("/")[0] if "/" in base else "bugfix"


def existing_bug_branches(repo: Path, remote: str, name: str) -> list[tuple[str, str]]:
    """Ветки бага name (<префикс>/<номер>), локальные и на сервере, как (ветка, где найдена):
    vega/455667-a и vega/455667-b считаются одной веткой, описание после номера не сравнивается."""
    same = lambda n: n == name or n.startswith(name + "-")
    local = [line.removeprefix("refs/heads/")
             for line in git("for-each-ref", "--format=%(refname)", "refs/heads/", cwd=repo).splitlines()]
    found = [(n, "в локальном репозитории") for n in local if same(n)]
    found += [(n, f"на сервере {remote}") for n in sorted(remote_branches(repo, remote)) if same(n)]
    return found


def free_branch_name(name: str, taken: set[str]) -> str:
    """name, а если оно занято - name-2, name-3 и т.д."""
    candidate, n = name, 1
    while candidate in taken:
        n += 1
        candidate = f"{name}-{n}"
    return candidate


PR_TEMPLATE_DIRS = (".azuredevops/", ".vsts/", "docs/", "")  # порядок поиска шаблонов в TFS


def pr_template(repo: Path, sha: str, target: str) -> str:
    """Шаблон описания pull request, который TFS подставляет при создании PR в веб-интерфейсе
    (через REST API он не применяется). Сначала ищется шаблон целевой ветки
    (<папка>/pull_request_template/branches/<ветка>.md), затем общий (<папка>/pull_request_template.md).
    Берется из коммита sha; пустая строка, если шаблона нет."""
    files = {f.lower(): f for f in git("ls-tree", "-r", "--name-only", sha, cwd=repo).splitlines()}
    branch_names = dict.fromkeys([target.lower(), target.split("/")[0].lower()])
    candidates = [f"{d}pull_request_template/branches/{b}{ext}"
                  for d in PR_TEMPLATE_DIRS for b in branch_names for ext in (".md", ".txt")]
    candidates += [f"{d}pull_request_template{ext}" for d in PR_TEMPLATE_DIRS for ext in (".md", ".txt")]
    path = next((files[c] for c in candidates if c in files), None)
    if path is None:
        return ""
    print(f"Шаблон описания pull request: {path}", file=sys.stderr)
    return git("show", f"{sha}:{path}", cwd=repo).lstrip("﻿")


def fix_in_new_branch(bug_url: str, repo_path: Path, repo_url: str, base: str, base_sha: str, remote: str,
                      bug_id: int, title: str, bug: str, area: str, allow_duplicate: bool = False,
                      code_rules: str | None = None) -> None:
    # Проверяем по префиксу и номеру бага до запроса постфикса у Claude, чтобы не тратить на него вызов.
    new_branch = f"{branch_prefix(bug_url, area, base)}/{bug_id}"
    found = existing_bug_branches(repo_path, remote, new_branch)
    listing = "\n  ".join(f"{n} ({where})" for n, where in found)
    if found and not allow_duplicate:
        sys.exit(f"Ветка для {new_branch} уже существует:\n  {listing}\n"
                 "Работа прервана, ничего не изменено. Чтобы исправить баг заново, удалите ветку "
                 "или повторите запрос с разрешением дубликата.")
    if found:
        print(f"Ветка для {new_branch} уже существует, создаю еще одну (разрешен дубликат):\n  {listing}",
              file=sys.stderr)
    # Постфикс от Claude может совпасть с уже существующей веткой - тогда добавляем -2, -3...
    new_branch = free_branch_name(f"{new_branch}-{branch_slug(repo_path, title, bug)}", {n for n, _ in found})

    worktree = repo_path.parent / f"{repo_path.name}-ai" / str(bug_id)
    if worktree.exists():
        sys.exit(f"Папка {worktree} уже существует, удалите ее.")

    # Новая ветка создается от последнего коммита базовой ветки на сервере в отдельной папке (git worktree).
    # Рабочая копия и ее текущая ветка при этом не меняются.
    print(f"Создаю ветку {new_branch} от {remote}/{base} ({base_sha[:10]}) в {worktree}", file=sys.stderr)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "-b", new_branch, str(worktree), base_sha, cwd=repo_path)

    def cleanup(delete_branch: bool):
        git("worktree", "remove", "--force", str(worktree), cwd=repo_path)
        if delete_branch:
            git("branch", "-D", new_branch, cwd=repo_path)

    try:
        prompt = BUG_BLOCK.format(repo=repo_url, branch=new_branch, bug=bug)
        if code_rules:
            prompt += RULES_BLOCK.format(rules=code_rules)
        prompt += FIX_TASK
        res = ask_claude(worktree, prompt, MODEL, allow_edits=True)
    except BaseException:
        cleanup(delete_branch=True)
        raise
    summary = res["result"].strip()
    print(summary)
    print(f"\n---\ncost: ${res.get('total_cost_usd', 0):.2f}, session: {res.get('session_id')}", file=sys.stderr)

    if not git("status", "--porcelain", cwd=worktree):
        cleanup(delete_branch=True)
        sys.exit("Claude не внес изменений в файлы, ветка не создана.")

    git("add", "-A", cwd=worktree)
    print("\nИзменения:\n" + git("diff", "--cached", "--stat", cwd=worktree), file=sys.stderr)
    git("commit", "-F", "-", cwd=worktree, input=f"#{bug_id} {title}\n\n{summary}\n")

    # Явный refspec: пушится только новая ветка, исходная не затрагивается.
    git("push", "-u", remote, f"refs/heads/{new_branch}:refs/heads/{new_branch}", cwd=worktree)
    print(f"Ветка {new_branch} запушена в {remote}.", file=sys.stderr)
    cleanup(delete_branch=False)

    description = f"Карточка: {bug_url}\n\nИсправление предложено Claude.\n\n{summary}"
    try:
        template = pr_template(repo_path, base_sha, base)
    except RuntimeError as e:
        print(f"Не удалось прочитать шаблон описания pull request: {e}", file=sys.stderr)
        template = ""
    pr_url = create_pull_request(repo_url, new_branch, base, f"#{bug_id} {title}", description,
                                 template=template, draft=True)
    print(f"Pull request {new_branch} -> {base}: {pr_url}", file=sys.stderr)

    # Pull request уже создан, поэтому ошибка комментария не прерывает работу, а только выводится в лог.
    link = html.escape(pr_url)
    try:
        add_workitem_comment(bug_url, f'Исправление бага: <a href="{link}">{link}</a>')
        print(f"В карточку #{bug_id} добавлен комментарий со ссылкой на pull request.", file=sys.stderr)
    except Exception as e:
        print(f"Внимание: {e}", file=sys.stderr)


def fix_bug(bug_url: str, repo_path: str, allow_duplicate: bool = False, code_rules: str | None = None) -> None:
    """Исправляет баг в новой ветке: коммит, push и черновик pull request.
    allow_duplicate - создать новую ветку, даже если ветка этого бага уже есть.
    code_rules - правила написания кода, которые Claude учитывает при исправлении.
    Ошибки завершаются через sys.exit("текст")."""
    repo_path = Path(repo_path)
    repo_url, current, remote = check_repo(repo_path)
    print(f"Репозиторий: {repo_path} ({repo_url}), текущая ветка {current}", file=sys.stderr)

    item = load_workitem(bug_url)
    heads = remote_branches(repo_path, remote)
    base, source = find_base_branch(item, repo_url, heads.__contains__)
    print(f"Базовая ветка: {base} ({source})", file=sys.stderr)

    base_sha = fetch_branch(repo_path, remote, base)
    fix_in_new_branch(bug_url, repo_path, repo_url, base, base_sha, remote, item.id, item.title, item.text, item.area,
                      allow_duplicate, code_rules)
