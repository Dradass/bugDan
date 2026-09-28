import argparse, json, os, shutil, subprocess, sys
from pathlib import Path

from tfs import create_pull_request, load_workitem

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


def check_repo(path: Path, url: str | None, branch: str | None) -> tuple[str, str, str]:
    """Проверяет локальный репозиторий, ничего в нем не меняя. Возвращает (url, ветка, remote)."""
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

    current = git("rev-parse", "--abbrev-ref", "HEAD", cwd=path)
    if current == "HEAD":
        sys.exit(f"В {path} detached HEAD, переключитесь на ветку.")
    if branch and branch != current:
        sys.exit(f"В {path} сейчас открыта ветка {current}, а запрошена {branch}.\n"
                 f"Переключите ветку вручную (git checkout {branch}) или не указывайте --branch.")
    return url, current, remote


def decode_console(data: bytes) -> str:
    """Сообщения cmd.exe приходят в OEM-кодировке консоли (cp866), а не в UTF-8."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("oem" if sys.platform == "win32" else "latin-1", errors="replace")


def ask_claude(repo_path: Path, prompt: str, model: str, claude: str | None = None,
               allow_edits: bool = False) -> dict:
    claude = claude or os.environ.get("CLAUDE_BIN") or shutil.which("claude") or sys.exit("claude CLI не найден в PATH")
    tools = "Read,Grep,Glob,Edit,Write" if allow_edits else "Read,Grep,Glob"
    args = [claude, "-p",
            "--output-format", "json",
            "--model", model,
            "--allowedTools", tools,
            "--max-turns", "60" if allow_edits else "40"]
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


def default_branch_name(current: str, bug_id: int) -> str:
    # Ветку внутри текущей (vega/x -> vega/x/43328) git создать не даст: имя уже занято файлом ref.
    # Поэтому берем префикс команды из текущей ветки: vega/43375-update-promts -> vega/43328-ai-fix.
    prefix = current.split("/")[0] if "/" in current else "bugfix"
    return f"{prefix}/{bug_id}-ai-fix"


def branch_locations(repo: Path, remote: str, name: str) -> list[str]:
    """Где уже есть ветка с таким именем: локально и/или на сервере."""
    found = []
    if subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
                      cwd=repo, capture_output=True).returncode == 0:
        found.append("в локальном репозитории")
    if git("ls-remote", "--heads", remote, f"refs/heads/{name}", cwd=repo):
        found.append(f"на сервере ({remote})")
    return found


def fix_in_new_branch(a, repo_path: Path, repo_url: str, current: str, remote: str,
                      bug_id: int, title: str, bug: str) -> None:
    new_branch = a.new_branch or default_branch_name(current, bug_id)
    found = branch_locations(repo_path, remote, new_branch)
    if found:
        sys.exit(f"Ветка {new_branch} уже существует {' и '.join(found)}.\n"
                 f"Работа прервана, ничего не изменено. Удалите ветку или задайте другое имя через --new-branch.")

    worktree = Path(a.worktree_dir) if a.worktree_dir else repo_path.parent / f"{repo_path.name}-ai" / str(bug_id)
    if worktree.exists():
        sys.exit(f"Папка {worktree} уже существует, удалите ее или укажите --worktree-dir.")

    if git("status", "--porcelain", cwd=repo_path):
        print("Внимание: незакоммиченные изменения рабочей копии в новую ветку не попадут.", file=sys.stderr)

    # Новая ветка создается от последнего коммита текущей ветки в отдельной папке (git worktree).
    # Рабочая копия и исходная ветка при этом не меняются.
    base = git("rev-parse", "HEAD", cwd=repo_path)
    print(f"Создаю ветку {new_branch} от {current} ({base[:10]}) в {worktree}", file=sys.stderr)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "-b", new_branch, str(worktree), base, cwd=repo_path)

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
    if not git("ls-remote", "--heads", remote, current, cwd=repo_path):
        print(f"Pull request не создан: ветки {current} нет на {remote}, сначала запушьте ее.", file=sys.stderr)
        return
    description = (f"Карточка: {a.bug_url}\n\n" if a.bug_url else "") + \
                  f"Исправление предложено Claude.\n\n{summary}"
    pr_url = create_pull_request(repo_url, new_branch, current, f"#{bug_id} {title}", description,
                                 draft=not a.publish_pr)
    print(f"Pull request {new_branch} -> {current}: {pr_url}", file=sys.stderr)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--repo-path", default=DEFAULT_REPO_PATH, help="Локальная папка репозитория")
    p.add_argument("--repo", help="URL репозитория для проверки remote (по умолчанию берется origin)")
    p.add_argument("--branch", help="Ожидаемая ветка (по умолчанию текущая)")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--bug-url", help="Ссылка на карточку в TFS")
    src.add_argument("--bug", help="Текст бага")
    src.add_argument("--bug-file", help="Файл с описанием бага")
    p.add_argument("--bug-id", type=int, help="Номер бага, если он не берется из карточки TFS")
    p.add_argument("--no-comments", action="store_true", help="Не добавлять комментарии карточки")
    p.add_argument("--print-bug", action="store_true", help="Только показать текст бага, без вызова Claude")
    p.add_argument("--analyze-only", action="store_true", help="Только анализ, без ветки и коммита")
    p.add_argument("--new-branch", help="Имя новой ветки (по умолчанию <префикс>/<номер>-ai-fix)")
    p.add_argument("--worktree-dir", help="Папка для новой ветки (по умолчанию <repo-path>-ai/<номер>)")
    p.add_argument("--no-push", action="store_true", help="Сделать коммит, но не пушить")
    p.add_argument("--no-pr", action="store_true", help="Не создавать pull request после push")
    p.add_argument("--publish-pr", action="store_true", help="Создать обычный pull request вместо черновика")
    p.add_argument("--keep-worktree", action="store_true", help="Не удалять папку новой ветки после push")
    p.add_argument("--model", default="claude-opus-5")
    p.add_argument("--claude", help="Путь к claude CLI (по умолчанию CLAUDE_BIN или поиск в PATH)")
    a = p.parse_args()

    repo_path = Path(a.repo_path)
    repo_url, current, remote = check_repo(repo_path, a.repo, a.branch)
    print(f"Репозиторий: {repo_path} ({repo_url}), ветка {current}", file=sys.stderr)

    if a.bug_url:
        item = load_workitem(a.bug_url, with_comments=not a.no_comments)
        bug_id, title, bug = item.id, item.title, item.text
    else:
        bug = Path(a.bug_file).read_text(encoding="utf-8") if a.bug_file else a.bug
        bug_id, title = a.bug_id, bug.strip().splitlines()[0][:100]

    if a.print_bug:
        print(bug)
        sys.exit(0)

    if a.analyze_only:
        if git("status", "--porcelain", cwd=repo_path):
            print("Внимание: Claude будет анализировать код вместе с незакоммиченными изменениями.", file=sys.stderr)
        res = ask_claude(repo_path, BUG_BLOCK.format(repo=repo_url, branch=current, bug=bug) + ANALYZE_TASK,
                         a.model, a.claude)
        print(res["result"])
        print(f"\n---\ncost: ${res.get('total_cost_usd', 0):.2f}, session: {res.get('session_id')}", file=sys.stderr)
    else:
        if bug_id is None:
            sys.exit("Номер бага не известен: укажите --bug-url или --bug-id.")
        fix_in_new_branch(a, repo_path, repo_url, current, remote, bug_id, title, bug)


if __name__ == "__main__":
    main()
