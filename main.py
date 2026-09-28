import argparse, json, os, re, shutil, subprocess, sys
from pathlib import Path

from tfs import create_pull_request, find_base_branch, load_workitem, parse_team

DEFAULT_REPO_PATH = r"D:\Projects\master\RX"

BUG_BLOCK = """Ты работаешь в репозитории {repo} (ветка {branch}).
Описание бага:
---
{bug}
---
"""

ANALYZE_TASK = """Задача:
1. Найди в коде причину бага (укажи файлы и строки).
2. Объясни, почему он возникает.
3. Предложи исправление в виде unified diff.
4. Перечисли риски и что стоит проверить тестами.
Файлы не изменяй, только анализируй и предлагай."""

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


def normalize_url(url: str) -> str:
    return url.strip().rstrip("/").removesuffix(".git").lower()


def check_repo(path: Path, url: str | None) -> tuple[str, str, str]:
    """Проверяет локальный репозиторий, ничего в нем не меняя. Возвращает (url, текущая ветка, remote)."""
    if not path.is_dir():
        sys.exit(f"Папка репозитория не найдена: {path}")
    try:
        top = Path(git("rev-parse", "--show-toplevel", cwd=path))
    except RuntimeError:
        sys.exit(f"{path} не является git-репозиторием")
    if top.resolve() != path.resolve():
        print(f"Внимание: корень репозитория - {top}", file=sys.stderr)

    remotes = {}
    for line in git("remote", "-v", cwd=path).splitlines():
        name, remote_url, _ = line.split()
        remotes.setdefault(name, remote_url)
    if url:
        matched = [n for n, u in remotes.items() if normalize_url(u) == normalize_url(url)]
        if not matched:
            sys.exit(f"Репозиторий в {path} не соответствует {url}.\n"
                     f"Найдены remote: {', '.join(sorted(set(remotes.values())))}")
        remote = "origin" if "origin" in matched else matched[0]
    else:
        remote = "origin"
        url = remotes.get(remote) or sys.exit(f"В {path} нет remote origin")

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


def ask_claude(repo_path: Path, prompt: str, model: str, claude: str | None = None,
               allow_edits: bool = False, tools: str | None = None, max_turns: int | None = None) -> dict:
    claude = claude or os.environ.get("CLAUDE_BIN") or shutil.which("claude") or sys.exit("claude CLI не найден в PATH")
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


def branch_slug(repo_path: Path, title: str, bug: str, model: str, claude: str | None) -> str:
    """Постфикс ветки из 2-7 слов в kebab-case по сути бага. Если Claude не справился - транслит названия."""
    try:
        res = ask_claude(repo_path, SLUG_PROMPT.format(title=title, bug=bug[:4000]), model, claude,
                         tools="", max_turns=1)
        words = kebab_words(res["result"].strip().splitlines()[0])
        if 2 <= len(words) <= 7:
            return "-".join(words)
        print(f"Claude предложил неподходящий постфикс ветки: {res['result'][:100]!r}", file=sys.stderr)
    except (SystemExit, Exception) as e:
        print(f"Не удалось получить постфикс ветки от Claude: {e}", file=sys.stderr)
    words = kebab_words(title)[:7] or ["bug"]
    return "-".join(words if len(words) >= 2 else ["fix", *words])


def branch_prefix(bug_url: str, base: str) -> str:
    # Префикс - команда из ссылки на доску: .../_boards/board/t/Vega/Stories/?workitem=43383 -> vega/43383-<slug>.
    # В ссылке без команды (.../_workitems/edit/ID) берем префикс базовой ветки: vega/43375-x -> vega/...
    # Ветку внутри базовой (vega/x -> vega/x/43328) git создать не даст: имя уже занято файлом ref.
    team = parse_team(bug_url)
    if team:
        return re.sub(r"[^\w.-]+", "-", team.lower()).strip("-.")
    return base.split("/")[0] if "/" in base else "bugfix"


def existing_bug_branches(repo: Path, remote: str, name: str) -> list[str]:
    """Ветки этого бага, локальные и на сервере: vega/455667-a и vega/455667-b считаются одной веткой,
    описание после номера не сравнивается. Имя без номера (--new-branch my-fix) сравнивается целиком."""
    m = re.match(r"(.+?/\d+)(?:-|$)", name)
    same = (lambda n: n == m[1] or n.startswith(m[1] + "-")) if m else (lambda n: n == name)
    local = [line.removeprefix("refs/heads/")
             for line in git("for-each-ref", "--format=%(refname)", "refs/heads/", cwd=repo).splitlines()]
    found = [f"{n} (в локальном репозитории)" for n in local if same(n)]
    found += [f"{n} (на сервере {remote})" for n in sorted(remote_branches(repo, remote)) if same(n)]
    return found


def fix_in_new_branch(a, repo_path: Path, repo_url: str, base: str, base_sha: str, remote: str,
                      bug_id: int, title: str, bug: str) -> None:
    # Проверяем по префиксу и номеру бага до запроса постфикса у Claude, чтобы не тратить на него вызов.
    new_branch = a.new_branch or f"{branch_prefix(a.bug_url, base)}/{bug_id}"
    found = existing_bug_branches(repo_path, remote, new_branch)
    if found:
        sys.exit(f"Ветка для {new_branch} уже существует:\n  " + "\n  ".join(found) +
                 "\nРабота прервана, ничего не изменено. Удалите ветку или задайте другое имя через --new-branch.")
    if not a.new_branch:
        new_branch += "-" + branch_slug(repo_path, title, bug, a.slug_model, a.claude)

    worktree = Path(a.worktree_dir) if a.worktree_dir else repo_path.parent / f"{repo_path.name}-ai" / str(bug_id)
    if worktree.exists():
        sys.exit(f"Папка {worktree} уже существует, удалите ее или укажите --worktree-dir.")

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
        prompt = BUG_BLOCK.format(repo=repo_url, branch=new_branch, bug=bug) + FIX_TASK
        res = ask_claude(worktree, prompt, a.model, a.claude, allow_edits=True)
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

    if a.no_push:
        print(f"Коммит создан в ветке {new_branch} (без push), папка: {worktree}", file=sys.stderr)
        return
    # Явный refspec: пушится только новая ветка, исходная не затрагивается.
    git("push", "-u", remote, f"refs/heads/{new_branch}:refs/heads/{new_branch}", cwd=worktree)
    print(f"Ветка {new_branch} запушена в {remote}.", file=sys.stderr)
    if not a.keep_worktree:
        cleanup(delete_branch=False)

    if a.no_pr:
        return
    description = f"Карточка: {a.bug_url}\n\nИсправление предложено Claude.\n\n{summary}"
    pr_url = create_pull_request(repo_url, new_branch, base, f"#{bug_id} {title}", description,
                                 draft=not a.publish_pr)
    print(f"Pull request {new_branch} -> {base}: {pr_url}", file=sys.stderr)


def analyze(a, repo_path: Path, repo_url: str, current: str, base: str, base_sha: str,
            bug_id: int, bug: str) -> None:
    prompt = BUG_BLOCK.format(repo=repo_url, branch=base, bug=bug) + ANALYZE_TASK
    if current == base:
        if git("status", "--porcelain", cwd=repo_path):
            print("Внимание: Claude будет анализировать код вместе с незакоммиченными изменениями.", file=sys.stderr)
        res = ask_claude(repo_path, prompt, a.model, a.claude)
    else:
        # Открыта другая ветка: анализируем базовую во временной папке, рабочую копию не трогаем.
        worktree = repo_path.parent / f"{repo_path.name}-ai" / f"{bug_id}-analyze"
        if worktree.exists():
            sys.exit(f"Папка {worktree} уже существует, удалите ее.")
        print(f"Открыта ветка {current}, анализирую {base} ({base_sha[:10]}) в {worktree}", file=sys.stderr)
        worktree.parent.mkdir(parents=True, exist_ok=True)
        git("worktree", "add", "--detach", str(worktree), base_sha, cwd=repo_path)
        try:
            res = ask_claude(worktree, prompt, a.model, a.claude)
        finally:
            git("worktree", "remove", "--force", str(worktree), cwd=repo_path)
    print(res["result"])
    print(f"\n---\ncost: ${res.get('total_cost_usd', 0):.2f}, session: {res.get('session_id')}", file=sys.stderr)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--repo-path", default=DEFAULT_REPO_PATH, help="Локальная папка репозитория")
    p.add_argument("--repo", help="URL репозитория для проверки remote (по умолчанию берется origin)")
    p.add_argument("--branch", help="Базовая ветка (по умолчанию определяется по карточке TFS)")
    p.add_argument("--bug-url", required=True, help="Ссылка на карточку в TFS")
    p.add_argument("--no-comments", action="store_true", help="Не добавлять комментарии карточки")
    p.add_argument("--print-bug", action="store_true", help="Только показать текст бага, без вызова Claude")
    p.add_argument("--analyze-only", action="store_true", help="Только анализ, без ветки и коммита")
    p.add_argument("--new-branch", help="Имя новой ветки (по умолчанию <префикс>/<номер>-<суть-бага>)")
    p.add_argument("--worktree-dir", help="Папка для новой ветки (по умолчанию <repo-path>-ai/<номер>)")
    p.add_argument("--no-push", action="store_true", help="Сделать коммит, но не пушить")
    p.add_argument("--no-pr", action="store_true", help="Не создавать pull request после push")
    p.add_argument("--publish-pr", action="store_true", help="Создать обычный pull request вместо черновика")
    p.add_argument("--keep-worktree", action="store_true", help="Не удалять папку новой ветки после push")
    p.add_argument("--model", default="claude-opus-5")
    p.add_argument("--slug-model", default="claude-haiku-4-5",
                   help="Модель для постфикса имени ветки по сути бага")
    p.add_argument("--claude", help="Путь к claude CLI (по умолчанию CLAUDE_BIN или поиск в PATH)")
    a = p.parse_args()

    repo_path = Path(a.repo_path)
    repo_url, current, remote = check_repo(repo_path, a.repo)
    print(f"Репозиторий: {repo_path} ({repo_url}), текущая ветка {current}", file=sys.stderr)

    item = load_workitem(a.bug_url, with_comments=not a.no_comments)
    bug_id, title, bug = item.id, item.title, item.text

    if a.branch:
        base, source = a.branch, "параметр --branch"
    else:
        heads = remote_branches(repo_path, remote)
        base, source = find_base_branch(item, repo_url, heads.__contains__)
    print(f"Базовая ветка: {base} ({source})", file=sys.stderr)

    if a.print_bug:
        print(bug)
        sys.exit(0)

    base_sha = fetch_branch(repo_path, remote, base)
    if a.analyze_only:
        analyze(a, repo_path, repo_url, current, base, base_sha, bug_id, bug)
    else:
        fix_in_new_branch(a, repo_path, repo_url, base, base_sha, remote, bug_id, title, bug)


if __name__ == "__main__":
    main()
