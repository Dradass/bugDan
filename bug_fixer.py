import html, json, os, re, shutil, subprocess, sys
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple

from tfs import (Attachment, WorkItem, add_required_reviewer, add_workitem_comment, area_team, create_pull_request,
                 create_workitem_comment, download_attachments, find_base_branch, load_workitem, move_to_board_column,
                 parse_team, update_workitem_comment)

# Модели можно задать аргументами сервера (use_models), в том числе модель на своем сервере.
MODEL = "claude-opus-5"
SLUG_MODEL = "claude-haiku-4-5"  # Модель для постфикса имени ветки по сути бага

# Столбцы доски: карточку из TODO при взятии в работу переносим в Development.
TODO_COLUMN, DEVELOPMENT_COLUMN = "TODO", "Development"

MAX_ATTACHMENT_SIZE = 2 * 1024 * 1024  # Вложения карточки больше этого размера не скачиваются

CLAUDE_TIMEOUT = 60 * 60  # Сколько секунд ждать завершения claude, после этого процесс останавливается
FIX_MAX_TURNS = 100  # Сколько ходов (вызовов инструментов) Claude может сделать при исправлении
READ_MAX_TURNS = 40  # То же для запросов без правок кода
CLAUDE_HIDDEN_ENV = {"TFS_PAT", "BUGDAN_HOOK_USER", "BUGDAN_HOOK_PASSWORD"}  # Не передаются в окружение claude
_model_env: dict[str, str] = {}  # Окружение claude для своего сервера модели (use_models)
# Уровень рассуждений для своей модели: Claude Code по умолчанию передает high, а сервер модели
# принимает только xhigh, medium и low (через LiteLLM - reasoning_effort)
LOCAL_MODEL_EFFORT = "medium"

BUG_BLOCK = """Ты работаешь в репозитории {repo} (ветка {branch}).
Описание бага:
---
{bug}
---
"""

ATTACHMENTS_BLOCK = """К карточке приложены файлы. Они сохранены в папке {dir} (вне репозитория).
Это часть описания карточки: изучи их и учитывай при работе. Файлы в этой папке не изменяй.
{files}
"""

SKIPPED_ATTACHMENTS_BLOCK = """Эти вложения карточки получить не удалось, их содержимое тебе недоступно:
{files}
"""

RULES_BLOCK = """Правила написания кода, которые нужно соблюдать при исправлении:
---
{rules}
---
"""

ACCURACY_TASK = """4. Последней строкой ответа оцени, с какой точностью {what},
   в формате "Точность: N% - обоснование", где N от 0 до 100, а обоснование - одно предложение
   (насколько уверен в причине, проверено ли решение, что осталось непроверенным)."""

FIX_TASK = """Задача:
1. Найди в коде причину бага.
2. Внеси исправление прямо в файлы репозитория. Изменения должны быть минимальными
   и в стиле окружающего кода. Не создавай вспомогательных файлов, не делай коммитов.
3. В ответе кратко опиши: причину бага, что изменено (файлы), риски.
   Этот текст станет описанием коммита, поэтому пиши без markdown-заголовков.
Если исправление в коде не требуется (например, проблема в данных, настройках или окружении, поведение
соответствует замыслу или баг уже исправлен), не меняй файлы, а в ответе кратко объясни, почему исправление
в коде не нужно и что можно сделать вместо него. Этот текст станет комментарием в карточке бага,
поэтому пиши без markdown-разметки.
""" + ACCURACY_TASK.format(what="ты устранил баг (или верно определил, что исправление в коде не требуется)")

TASK_BLOCK = """Ты работаешь в репозитории {repo} (ветка {branch}).
Описание work item:
---
{bug}
---
"""

WORK_TASK = """Задача:
1. Изучи поставленную задачу в описании work item.
2. Выполни ее, внеся изменения прямо в файлы репозитория. Изменения должны быть в стиле окружающего кода.
   Не создавай вспомогательных файлов, не делай коммитов.
3. В ответе кратко опиши: что сделано, что изменено (файлы), риски.
   Этот текст станет описанием коммита, поэтому пиши без markdown-заголовков.
Если изменения в коде не требуются (например, задача уже выполнена или решается настройками, данными
или окружением) или описания недостаточно, чтобы ее выполнить, не меняй файлы, а в ответе кратко объясни
причину и что нужно сделать или уточнить. Этот текст станет комментарием в карточке,
поэтому пиши без markdown-разметки.
""" + ACCURACY_TASK.format(what="ты решил задачу (или верно определил, что изменения в коде не требуются)")


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


_claude_bin: str | None = None


def find_claude() -> str:
    """Абсолютный путь до Claude Code CLI: из CLAUDE_BIN или из PATH. Ищется один раз, ошибки - через sys.exit."""
    global _claude_bin
    if _claude_bin:
        return _claude_bin
    if env_bin := os.environ.get("CLAUDE_BIN"):
        path = Path(env_bin)
        if not path.is_absolute() or not path.is_file():
            sys.exit(f"CLAUDE_BIN должен быть абсолютным путем до существующего файла: {env_bin}")
    else:
        found = shutil.which("claude") or sys.exit("claude CLI не найден в PATH, задайте путь в CLAUDE_BIN")
        path = Path(found).resolve()
        # На Windows shutil.which сначала ищет в текущей папке, даже если ее нет в PATH.
        if path.parent not in {Path(d).resolve() for d in os.environ.get("PATH", "").split(os.pathsep) if d}:
            sys.exit(f"claude найден вне PATH ({path}), задайте путь в CLAUDE_BIN")
    _claude_bin = str(path)
    return _claude_bin


def use_models(model: str | None = None, slug_model: str | None = None, base_url: str | None = None,
               api_key: str | None = None) -> None:
    """Задает модели вместо MODEL и SLUG_MODEL. base_url - адрес своего сервера модели с Anthropic Messages API
    (например, прокси LiteLLM перед моделью с OpenAI API): все запросы claude, и служебные тоже, идут в него,
    к модели model с ключом api_key (master_key прокси); slug_model по умолчанию та же.
    Настройки передаются только в окружение процесса claude."""
    global MODEL, SLUG_MODEL
    if base_url and not (model and api_key):
        raise ValueError("Для своего сервера модели нужно указать имя модели и ключ")
    MODEL = model or MODEL
    SLUG_MODEL = slug_model or (model if base_url else SLUG_MODEL)
    _model_env.clear()
    if base_url:
        _model_env.update({"ANTHROPIC_BASE_URL": base_url.rstrip("/"), "ANTHROPIC_AUTH_TOKEN": api_key,
                           "ANTHROPIC_DEFAULT_OPUS_MODEL": MODEL, "ANTHROPIC_DEFAULT_SONNET_MODEL": MODEL,
                           "ANTHROPIC_DEFAULT_HAIKU_MODEL": SLUG_MODEL,
                           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})  # Без обращений к сервисам Anthropic


def run_claude(args: list[str], timeout: int, input: bytes = b"", cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Запускает claude без секретов сервера в окружении. Если он не завершился за timeout секунд,
    завершает его вместе с дочерними процессами. Ошибки - через sys.exit."""
    claude = find_claude()
    env = {k: v for k, v in os.environ.items() if k.upper() not in CLAUDE_HIDDEN_ENV}
    if _model_env:  # Свой сервер модели: ключ API Anthropic из окружения не должен перебить его ключ
        env = {k: v for k, v in env.items() if k.upper() != "ANTHROPIC_API_KEY"} | _model_env
    try:
        proc = subprocess.Popen([claude, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=cwd, env=env)
    except OSError as e:
        sys.exit(f"Не удалось запустить {claude}: {e}")
    try:
        stdout, stderr = proc.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired:
        if sys.platform == "win32":  # Обертка claude.cmd запускает node дочерним процессом, kill() его не завершит
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        proc.kill()
        proc.wait()
        sys.exit(f"claude не завершился за {timeout} с, процесс остановлен")
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def claude_version() -> str:
    """Версия Claude Code CLI (claude --version). Ошибки - через sys.exit."""
    proc = run_claude(["--version"], timeout=60)
    if proc.returncode != 0:
        sys.exit(f"claude --version завершился с кодом {proc.returncode}: {decode_console(proc.stderr)}")
    return proc.stdout.decode("utf-8", errors="replace").strip()


def ask_claude(repo_path: Path, prompt: str, model: str, allow_edits: bool = False, tools: str | None = None,
               max_turns: int | None = None, add_dirs: list[Path] = ()) -> dict:
    if tools is None:
        tools = "Read,Grep,Glob,Edit,Write" if allow_edits else "Read,Grep,Glob"
    args = ["-p",
            "--output-format", "json",
            "--model", model,
            "--max-turns", str(max_turns or (FIX_MAX_TURNS if allow_edits else READ_MAX_TURNS))]
    if _model_env:
        args += ["--effort", LOCAL_MODEL_EFFORT]
    # --tools ограничивает набор доступных инструментов, --allowedTools - разрешает их без запроса.
    # Без --tools Claude видит Bash/PowerShell и тратит ходы на вызовы, которые отклоняются.
    args += ["--tools", tools] + (["--allowedTools", tools] if tools else [])
    if allow_edits:
        args += ["--permission-mode", "acceptEdits"]
    for directory in add_dirs:  # Папки вне репозитория, к которым Claude получает доступ (вложения карточки)
        args += ["--add-dir", str(directory)]
    proc = run_claude(args, CLAUDE_TIMEOUT, prompt.encode("utf-8"), repo_path)
    stdout = proc.stdout.decode("utf-8", errors="replace")
    try:
        res = json.loads(stdout)
    except json.JSONDecodeError:
        sys.exit(f"claude завершился с кодом {proc.returncode}\nstdout: {stdout}\nstderr: {decode_console(proc.stderr)}")
    if res.get("is_error") or res.get("subtype", "success") != "success":
        # При error_max_turns / error_during_execution поля result нет - выводим подтип и детали.
        details = res.get("result") or "; ".join(map(str, res.get("errors") or [])) or "без описания"
        sys.exit(f"Ошибка Claude ({res.get('subtype')}, ходов: {res.get('num_turns')}, "
                 f"session: {res.get('session_id')}): {details}")
    return res


def usage_summary(res: dict) -> str:
    """Токены и стоимость запроса к Claude из JSON-ответа claude -p: входные (без кеша), записанные в кеш,
    прочитанные из кеша, выходные."""
    u = res.get("usage") or {}
    return (f"tokens: in {u.get('input_tokens', 0)}, cache write {u.get('cache_creation_input_tokens', 0)}, "
            f"cache read {u.get('cache_read_input_tokens', 0)}, out {u.get('output_tokens', 0)}; "
            f"cost: ${res.get('total_cost_usd', 0):.4f}; turns: {res.get('num_turns')}; session: {res.get('session_id')}")


SLUG_PROMPT = """Придумай короткое имя git-ветки для исправления бага.
Требования: от 2 до 7 английских слов в kebab-case (только a-z, 0-9 и дефисы), передающих суть проблемы,
например fix-null-price-in-order-export. Без номера бага, без префиксов и кавычек.
Ответь только именем, одной строкой.

Название бага: {title}
Описание:
---
{bug}
---"""

TASK_SLUG_PROMPT = """Придумай короткое имя git-ветки для выполнения задачи.
Требования: от 2 до 7 английских слов в kebab-case (только a-z, 0-9 и дефисы), передающих суть задачи,
например add-price-column-to-order-export. Без номера задачи, без префиксов и кавычек.
Ответь только именем, одной строкой.

Название задачи: {title}
Описание:
---
{bug}
---"""


class Texts(NamedTuple):
    """Тексты, которые зависят от типа карточки: запросы к Claude, комментарии в карточку и описание pull request."""
    block: str  # Описание карточки в запросе Claude
    task: str  # Что Claude должен сделать
    slug: str  # Запрос постфикса имени ветки
    no_change: str  # Заголовок комментария, когда Claude не внес изменений
    pr_note: str  # Строка в описании pull request
    pr_link: str  # Подпись ссылки на pull request в комментарии карточки


BUG_TEXTS = Texts(BUG_BLOCK, FIX_TASK, SLUG_PROMPT, "Исправление в коде не требуется.",
                  "Исправление предложено Claude.", "Исправление бага")
TASK_TEXTS = Texts(TASK_BLOCK, WORK_TASK, TASK_SLUG_PROMPT, "Изменения в коде не требуются.",
                   "Доработка выполнена Claude.", "Доработка по задаче")


def texts_for(item_type: str) -> Texts:
    """Тексты для бага - про поиск причины и исправление, для остальных типов (User Story, Task) - про выполнение задачи."""
    return BUG_TEXTS if item_type.lower() == "bug" else TASK_TEXTS


TRANSLIT = dict(zip("абвгдеёзийклмнопрстуфхцыэ", "abvgdeeziyklmnoprstufhcye")) | {
    "ж": "zh", "ч": "ch", "ш": "sh", "щ": "sch", "ю": "yu", "я": "ya", "ъ": "", "ь": ""}


def kebab_words(text: str) -> list[str]:
    text = "".join(TRANSLIT.get(ch, ch) for ch in text.lower())
    return re.findall(r"[a-z0-9]+", text)


def branch_slug(repo_path: Path, title: str, bug: str, prompt: str = SLUG_PROMPT) -> str:
    """Постфикс ветки из 2-7 слов в kebab-case по сути карточки. Если Claude не справился - транслит названия."""
    try:
        res = ask_claude(repo_path, prompt.format(title=title, bug=bug[:4000]), SLUG_MODEL,
                         tools="", max_turns=1)
        print(f"Постфикс ветки: {usage_summary(res)}", file=sys.stderr)
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


def move_card(bug_url: str, column: str, only_from: str | None = None) -> None:
    """Переносит карточку в столбец доски. Ошибка не прерывает работу, а только выводится в лог."""
    try:
        print(f"Доска: {move_to_board_column(bug_url, column, only_from)}.", file=sys.stderr)
    except Exception as e:
        print(f"Внимание: не удалось перенести карточку в столбец {column}: {e}", file=sys.stderr)


ACCURACY_RE = re.compile(r"^[^\w\n]*Точность[^\w\n]*(\d{1,3})\s*%[^\w\n]*(.*)$", re.IGNORECASE | re.MULTILINE)


def split_accuracy(summary: str) -> tuple[str, str | None]:
    """Отделяет от ответа Claude последнюю строку с оценкой точности. Возвращает (ответ без нее, "N% - обоснование")
    или (ответ, None), если оценки нет."""
    match = None
    for match in ACCURACY_RE.finditer(summary):
        pass
    if match is None:
        return summary, None
    percent, reason = min(int(match[1]), 100), match[2].strip()
    rest = (summary[:match.start()] + summary[match.end():]).strip()
    return rest, f"{percent}%" + (f" - {reason}" if reason else "")


def with_accuracy(comment: str, accuracy: str | None) -> str:
    """HTML комментария в карточку с оценкой точности Claude после основного текста."""
    return comment + (f"<br><b>Точность:</b> {html.escape(accuracy)}" if accuracy else "")


def no_fix_comment(summary: str, caption: str = BUG_TEXTS.no_change) -> str:
    """HTML комментария в карточку, когда Claude не внес изменений: его объяснение, почему изменения в коде не нужны."""
    text = "<br>".join(html.escape(line) for line in summary.splitlines())
    return f"<b>{html.escape(caption)}</b><br>{text}"


class CardComment:
    """Комментарий бота в карточке, который дописывается по ходу работы. Пока он не создан (start),
    каждое сообщение добавляется в карточку отдельным комментарием. Ошибки TFS - в RuntimeError."""

    def __init__(self, url: str):
        self.url, self.id, self.parts = url, None, []

    def start(self, text: str) -> None:
        self.id, self.parts = create_workitem_comment(self.url, text), [text]

    def add(self, text: str) -> None:
        if self.id is None:
            add_workitem_comment(self.url, text)
            return
        self.parts.append(text)
        update_workitem_comment(self.url, self.id, "<br><br>".join(self.parts))


def attachments_block(directory: Path, attachments: list[Attachment]) -> str:
    """Блок запроса Claude со списком вложений карточки: скачанные и те, что получить не удалось."""
    saved = [f"- {a.path.name} ({a.size} байт)" for a in attachments if a.path]
    skipped = [f"- {a.name}: {a.skipped}" for a in attachments if not a.path]
    block = ATTACHMENTS_BLOCK.format(dir=directory, files="\n".join(saved)) if saved else ""
    if skipped:
        block += SKIPPED_ATTACHMENTS_BLOCK.format(files="\n".join(skipped))
    return block


def fix_in_new_branch(bug_url: str, repo_path: Path, repo_url: str, base: str, base_sha: str, remote: str,
                      item: WorkItem, comment: CardComment, allow_duplicate: bool = False,
                      code_rules: str | None = None) -> None:
    bug_id, title, bug = item.id, item.title, item.text
    texts = texts_for(item.type)
    # Проверяем по префиксу и номеру бага до запуска Claude, чтобы не тратить на него вызовы.
    bug_branch_name = f"{branch_prefix(bug_url, item.area, base)}/{bug_id}"
    found = existing_bug_branches(repo_path, remote, bug_branch_name)
    listing = "\n  ".join(f"{n} ({where})" for n, where in found)
    if found and not allow_duplicate:
        sys.exit(f"Ветка для {bug_branch_name} уже существует:\n  {listing}\n"
                 "Работа прервана, ничего не изменено. Чтобы исправить баг заново, удалите ветку "
                 "или повторите запрос с разрешением дубликата.")
    if found:
        print(f"Ветка для {bug_branch_name} уже существует, создаю еще одну (разрешен дубликат):\n  {listing}",
              file=sys.stderr)

    worktree = repo_path.parent / f"{repo_path.name}-ai" / str(bug_id)
    attachments_dir = worktree.parent / f"{bug_id}-attachments"  # Вложения карточки, рядом с worktree
    for folder in (worktree, attachments_dir):
        if folder.exists():
            sys.exit(f"Папка {folder} уже существует, удалите ее.")

    move_card(bug_url, DEVELOPMENT_COLUMN, only_from=TODO_COLUMN)  # Баг взят в работу

    # Claude работает в отдельной папке (git worktree) на последнем коммите базовой ветки на сервере, без ветки
    # (detached HEAD). Ветка создается только если Claude изменил код. Рабочая копия и ее текущая ветка не меняются.
    print(f"Создаю рабочую папку {worktree} от {remote}/{base} ({base_sha[:10]})", file=sys.stderr)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "--detach", str(worktree), base_sha, cwd=repo_path)

    def cleanup():
        shutil.rmtree(attachments_dir, ignore_errors=True)
        git("worktree", "remove", "--force", str(worktree), cwd=repo_path)

    try:
        attachments = download_attachments(item, attachments_dir, MAX_ATTACHMENT_SIZE)
        for a in attachments:
            print(f"Вложение {a.name}: " + (f"сохранено в {a.path}" if a.path else f"не скачано, {a.skipped}"),
                  file=sys.stderr)
        prompt = texts.block.format(repo=repo_url, branch=base, bug=bug)
        prompt += attachments_block(attachments_dir, attachments)
        if code_rules:
            prompt += RULES_BLOCK.format(rules=code_rules)
        prompt += texts.task
        res = ask_claude(worktree, prompt, MODEL, allow_edits=True,
                         add_dirs=[attachments_dir] if attachments_dir.exists() else [])
    except BaseException:
        cleanup()
        raise
    summary, accuracy = split_accuracy(res["result"].strip())
    print(summary)
    print(f"Точность: {accuracy or 'Claude не указал'}", file=sys.stderr)
    print(f"\n---\n{usage_summary(res)}", file=sys.stderr)

    if not git("status", "--porcelain", cwd=worktree):
        cleanup()
        try:
            comment.add(with_accuracy(no_fix_comment(summary, texts.no_change), accuracy))
            print(f"В карточку #{bug_id} добавлен комментарий о том, что изменения в коде не требуются.",
                  file=sys.stderr)
        except Exception as e:
            print(f"Внимание: {e}", file=sys.stderr)
        raise FixSkipped("Claude не внес изменений в файлы, ветка не создана")

    # Постфикс запрашивается только когда ясно, что ветка нужна. Он может совпасть с уже существующей
    # веткой - тогда добавляем -2, -3...
    slug = branch_slug(repo_path, title, bug, texts.slug)
    new_branch_name = free_branch_name(f"{bug_branch_name}-{slug}", {n for n, _ in found})
    print(f"Создаю ветку {new_branch_name} от {remote}/{base} ({base_sha[:10]})", file=sys.stderr)
    git("switch", "-c", new_branch_name, cwd=worktree)
    git("add", "-A", cwd=worktree)
    print("\nИзменения:\n" + git("diff", "--cached", "--stat", cwd=worktree), file=sys.stderr)
    git("commit", "-F", "-", cwd=worktree, input=f"#{bug_id} {title}\n\n{summary}\n")

    # Явный refspec: пушится только новая ветка, исходная не затрагивается.
    git("push", "-u", remote, f"refs/heads/{new_branch_name}:refs/heads/{new_branch_name}", cwd=worktree)
    print(f"Ветка {new_branch_name} запушена в {remote}.", file=sys.stderr)
    cleanup()

    description = f"Карточка: {bug_url}\n\n{texts.pr_note}\n\n{summary}"
    try:
        template = pr_template(repo_path, base_sha, base)
    except RuntimeError as e:
        print(f"Не удалось прочитать шаблон описания pull request: {e}", file=sys.stderr)
        template = ""
    pr_url = create_pull_request(repo_url, new_branch_name, base, f"#{bug_id} {title}", description,
                                 template=template)
    print(f"Pull request {new_branch_name} -> {base}: {pr_url}", file=sys.stderr)

    # Pull request уже создан, поэтому ошибки рецензента и комментария не прерывают работу, а только выводятся в лог.
    if item.assigned_to:
        try:
            add_required_reviewer(pr_url, item.assigned_to)
            print(f"Обязательный рецензент pull request: {item.assigned_to.get('displayName')}.", file=sys.stderr)
        except Exception as e:
            print(f"Внимание: {e}", file=sys.stderr)
    else:
        print(f"У карточки #{bug_id} нет ответственного, обязательный рецензент не добавлен.", file=sys.stderr)

    link = html.escape(pr_url)
    try:
        comment.add(with_accuracy(f'{texts.pr_link}: <a href="{link}">{link}</a>', accuracy))
        print(f"В карточку #{bug_id} добавлен комментарий со ссылкой на pull request.", file=sys.stderr)
    except Exception as e:
        print(f"Внимание: {e}", file=sys.stderr)


class FixSkipped(Exception):
    """Исправление не выполнено, но это не ошибка: в карточке не указана ветка или Claude не внес изменений."""


DEFAULT_BRANCH_NOTE = """
Ветка, в которой ведется работа, не указана ни в карточке, ни в ее родительской User Story/Feature,
поэтому ветка взята из значения "По умолчанию" для репозитория: {branch}."""

NO_BRANCH_COMMENT = """<b>Работа не начата: не указана ветка, в которой проявляется баг.</b><br>
Ветка не найдена ни в карточке, ни в ее родительской User Story/Feature, а ветка по умолчанию для репозитория
{repo} не задана. Укажите ветку одним из способов:<br>
1. Свяжите ветку с карточкой в секции Development (Add link -&gt; Branch).<br>
2. Напишите в комментарии карточки имя ветки после слова «ветка» или «branch», например: «ветка: vega/43383».<br>
3. Сделайте то же в родительской User Story или Feature.<br>
Ветка должна быть в репозитории на сервере. После добавления ветки поставьте на карточку тег {tag} заново (после обработки он снимается автоматически)."""
FIX_TAG = "AIFix"  # Тег запуска исправления (подписка TFS), упоминается в комментарии NO_BRANCH_COMMENT


def fix_bug(bug_url: str, repo_path: str, allow_duplicate: bool = False, code_rules: str | None = None,
            default_branch: str | None = None, require_branch: bool = False,
            comment: CardComment | None = None) -> None:
    """Исправляет баг или выполняет задачу (User Story, Task) в новой ветке: коммит, push и pull request.
    allow_duplicate - создать новую ветку, даже если ветка этого бага уже есть.
    code_rules - правила написания кода, которые Claude учитывает при исправлении.
    default_branch - базовая ветка, если в карточке и ее родителе ветка не найдена (в текст бага для Claude
    дописывается, что ветка взята по умолчанию). Если она не задана, в карточку добавляется комментарий о том,
    как указать ветку, и выбрасывается FixSkipped.
    require_branch - если ветка в карточке и ее родителе не найдена, не исправлять, а выбросить FixSkipped.
    comment - комментарий в карточке, в который дописываются итоги; None - каждый итог отдельным комментарием.
    Ошибки завершаются через sys.exit("текст")."""
    repo_path = Path(repo_path)
    comment = comment or CardComment(bug_url)
    repo_url, current, remote = check_repo(repo_path)
    print(f"Репозиторий: {repo_path} ({repo_url}), текущая ветка {current}", file=sys.stderr)

    item = load_workitem(bug_url)
    heads = remote_branches(repo_path, remote)
    found = find_base_branch(item, repo_url, heads.__contains__)
    if found:
        base, source = found
    elif require_branch:
        raise FixSkipped("в карточке и ее родительской User Story/Feature не указана ветка")
    elif default_branch is None:
        try:
            comment.add(NO_BRANCH_COMMENT.format(repo=html.escape(repo_url), tag=FIX_TAG))
            commented = "в карточку добавлен комментарий о том, как указать ветку"
        except Exception as e:
            commented = f"комментарий в карточку добавить не удалось: {e}"
        raise FixSkipped("ветка не указана ни в карточке и ее родительской User Story/Feature, "
                         f"ни в default_branch репозитория; {commented}")
    else:
        base, source = default_branch, "ветка репозитория по умолчанию"
        item = replace(item, text=item.text + DEFAULT_BRANCH_NOTE.format(branch=base))
    print(f"Базовая ветка: {base} ({source})", file=sys.stderr)

    base_sha = fetch_branch(repo_path, remote, base)
    fix_in_new_branch(bug_url, repo_path, repo_url, base, base_sha, remote, item, comment, allow_duplicate, code_rules)
