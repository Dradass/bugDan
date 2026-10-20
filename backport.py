"""Бэкпорт исправления бага в ветки версий по тегу AIBackport (endpoint /check-work-item сервера).

Когда карточку бага с тегом AIBackport переносят в столбец Finalizing (или ставят тег на карточку в Finalizing),
изменения связанного с ней pull request или коммита переносятся в каждую ветку из "backport_branches" репозитория
в config.json: создается ветка <ветка исправления>-<ветка версии>, в нее коммитятся изменения, и создается
pull request в ветку версии. После обработки тег AIBackport снимается, чтобы его можно было поставить снова."""
import html
import logging
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import requests

from board_checker import COMMIT_LINK_RE, FINALIZING_COLUMN, links_in_comments, mention, pull_requests
from bug_fixer import REPO_LOCK, branch_prefix, check_repo, git, pr_template, remote_branches
from tfs import (History, WorkItem, add_workitem_comment, create_pull_request, get_commit,
                 linked_commits, load_history, load_workitem, mention_html, repository_id, set_workitem_tag)

BACKPORT_TAG = "AIBackport"  # Тег карточки, по которому выполняется бэкпорт
ERROR_LIMIT = 500  # Сколько символов текста ошибки по ветке попадает в комментарий карточки

NO_REPO_COMMENT = """{person}, бэкпорт не выполнен: на карточке нет тега репозитория ({tags}).<br>
Поставьте тег репозитория и снова поставьте тег {tag}."""

NO_BRANCHES_COMMENT = """{person}, бэкпорт не выполнен: в config.json сервера для репозитория {repos} не заданы ветки \
для бэкпорта (backport_branches)."""

NO_SOURCE_COMMENT = """{person}, бэкпорт не выполнен: в карточке нет связанного pull request (не отмененного) или коммита \
- ни в разделе Development, ни ссылкой в комментариях.<br>
Привяжите pull request или коммит с исправлением и снова поставьте тег {tag}."""

MANY_SOURCES_COMMENT = """{person}, бэкпорт не выполнен: в карточке бага для бэкпорта должен быть привязан 1 элемент \
разработки - pull request или (если pull request нет) коммит, а найдено несколько:<br>
{links}<br>
Оставьте один элемент разработки и снова поставьте тег {tag}."""

OTHER_REPO_COMMENT = """{person}, бэкпорт не выполнен: {kind} <a href="{url}">{url}</a> из репозитория, для которого \
в config.json сервера не заданы ветки бэкпорта (теги репозиториев карточки: {repos})."""

ERROR_COMMENT = """{person}, бэкпорт не выполнен из-за ошибки:<br>{error}"""

RESULT_COMMENT = """{person}, {result} для бэкпорта изменений из {kind} <a href="{url}">{url}</a>:<br>
{created}{failed}"""

log = logging.getLogger("bugdan")

# Бэкпорт обращается к TFS и git, поэтому идет в фоне по одному, а сервер сразу отвечает TFS.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="backport")
_pending: set[int] = set()  # Карточки, бэкпорт которых ждет запуска или идет: повтор события пропускается
_pending_lock = threading.Lock()


@dataclass
class Source:
    """Элемент разработки карточки, изменения которого переносятся в ветки версий."""
    kind: str  # "pull request" или "коммит"
    url: str  # Ссылка в веб-интерфейсе
    title: str
    repo_id: str
    author: dict | None
    branch: str  # Ветка исправления, к имени которой добавляется ветка версии
    diff: list[str]  # Аргументы git diff, которые дают изменения элемента
    commits: list[str]  # Коммиты, которые нужны для git diff
    refs: list[str]  # Ветки, в которых эти коммиты стоит искать на сервере


class BackportError(Exception):
    """Бэкпорт в ветку версии не выполнен; текст - для комментария карточки."""


def submit(bug_id: int, url: str, repos: dict) -> bool:
    """Ставит бэкпорт карточки в очередь. repos - репозитории по тегам (server.Repo: path, backport_branches).
    False, если бэкпорт этой карточки уже ждет запуска или идет (повтор события)."""
    with _pending_lock:
        if bug_id in _pending:
            return False
        _pending.add(bug_id)
    _executor.submit(_run, bug_id, url, repos)
    return True


def _run(bug_id: int, url: str, repos: dict) -> None:
    """Загружает карточку и выполняет бэкпорт, если она еще в Finalizing и на ней стоит тег. После обработки
    (успешной или нет) снимает тег; при неожиданной ошибке добавляет в карточку комментарий."""
    try:
        item = load_workitem(url, with_comments=False)
        if item.board_column.strip().lower() != FINALIZING_COLUMN.lower():
            log.info("Карточка %s: бэкпорт пропущен, карточка уже в столбце %s", bug_id, item.board_column or "-")
            return
        if not any(t.lower() == BACKPORT_TAG.lower() for t in item.tags):
            log.info("Карточка %s: бэкпорт пропущен, тег %s уже снят", bug_id, BACKPORT_TAG)
            return
        history = load_history(item)
        try:
            backport(item, url, history, repos)
        except Exception as e:
            log.exception("Карточка %s: ошибка бэкпорта", bug_id)
            _comment(url, ERROR_COMMENT.format(person=_person(url, history), error=_error_html(e)))
        _remove_tag(bug_id, url)
    except Exception:
        log.exception("Карточка %s: ошибка бэкпорта", bug_id)
    finally:
        with _pending_lock:
            _pending.discard(bug_id)


def backport(item: WorkItem, url: str, history: History, repos: dict) -> None:
    """Бэкпорт изменений элемента разработки карточки в ветки версий ее репозитория. Если ветки бэкпорта
    не заданы или элемент разработки не найден (или их несколько), в карточку добавляется комментарий об этом."""
    person = _person(url, history)
    card_tags = {t.lower() for t in item.tags}
    card_repos = {tag: repo for tag, repo in repos.items() if tag in card_tags}
    if not card_repos:
        _comment(url, NO_REPO_COMMENT.format(person=person, tags=html.escape(", ".join(sorted(repos))),
                                             tag=BACKPORT_TAG))
        log.warning("Карточка %s: бэкпорт не выполнен, на карточке нет тега репозитория", item.id)
        return
    targets = {tag: repo for tag, repo in card_repos.items() if repo.backport_branches}
    if not targets:
        _comment(url, NO_BRANCHES_COMMENT.format(person=person, repos=html.escape(", ".join(sorted(card_repos)))))
        log.warning("Карточка %s: бэкпорт не выполнен, для репозиториев %s не заданы backport_branches", item.id,
                    ", ".join(sorted(card_repos)))
        return

    prs = [pr for pr in pull_requests(item, history) if pr.status != "abandoned"]
    commits = [] if prs else _commits(item, history)
    sources = prs or commits
    if not sources:
        _comment(url, NO_SOURCE_COMMENT.format(person=person, tag=BACKPORT_TAG))
        log.warning("Карточка %s: бэкпорт не выполнен, нет связанного pull request или коммита", item.id)
        return
    if len(sources) > 1:
        links = "<br>".join(f'- <a href="{html.escape(s.url)}">{html.escape(s.url)}</a>' for s in sources)
        _comment(url, MANY_SOURCES_COMMENT.format(person=person, links=links, tag=BACKPORT_TAG))
        log.warning("Карточка %s: бэкпорт не выполнен, элементов разработки несколько: %s", item.id,
                    ", ".join(s.url for s in sources))
        return
    source = _pr_source(prs[0]) if prs else _commit_source(commits[0], url, item)

    with REPO_LOCK:
        found = _find_repo(targets, source.repo_id)
        if found is None:
            _comment(url, OTHER_REPO_COMMENT.format(person=person, kind=source.kind, url=html.escape(source.url),
                                                    repos=html.escape(", ".join(sorted(card_repos)))))
            log.warning("Карточка %s: бэкпорт не выполнен, %s %s не из репозиториев %s", item.id, source.kind,
                        source.url, ", ".join(sorted(targets)))
            return
        repo, repo_url, remote = found
        created, failed = [], []
        for target in repo.backport_branches:
            try:
                pr_url = backport_branch(repo.path, repo_url, remote, source, target, item, url)
                created.append((target, pr_url))
                log.info("Карточка %s: бэкпорт в %s - %s", item.id, target, pr_url)
            except Exception as e:
                failed.append((target, e))
                log.error("Карточка %s: бэкпорт в %s не выполнен: %s", item.id, target, e)

    author = mention_html(url, source.author) if source.author else person
    _comment(url, RESULT_COMMENT.format(
        person=author, result="созданы pull request" if created else "не удалось создать pull request",
        kind=source.kind, url=html.escape(source.url),
        created="".join(f'- {html.escape(t)}: <a href="{html.escape(u)}">{html.escape(u)}</a><br>'
                        for t, u in created),
        failed=("Не удалось создать pull request:<br>" if created else "")
        + "".join(f"- {html.escape(t)}: {_error_html(e)}<br>" for t, e in failed)))


def backport_branch(repo_path: Path, repo_url: str, remote: str, source: Source, target: str,
                    item: WorkItem, url: str) -> str:
    """Создает от ветки версии target ветку <ветка исправления>-<target> с изменениями source и pull request
    из нее в target; возвращает ссылку на pull request. Ошибки - в BackportError или RuntimeError (git, TFS)."""
    heads = remote_branches(repo_path, remote)
    if target not in heads:
        raise BackportError(f"ветки {target} нет в репозитории")
    branch = f"{source.branch}-{target}"
    if branch in heads:
        raise BackportError(f"ветка {branch} уже есть на сервере - бэкпорт в нее уже выполнялся")
    _ensure_commits(repo_path, remote, source)
    git("fetch", remote, f"refs/heads/{target}", cwd=repo_path)
    base_sha = git("rev-parse", "FETCH_HEAD", cwd=repo_path)

    folder = f"{item.id}-backport-" + re.sub(r"[^\w.-]+", "-", target)
    worktree = repo_path.parent / f"{repo_path.name}-ai" / folder
    patch = worktree.parent / f"{folder}.patch"
    if worktree.exists():
        raise BackportError(f"папка {worktree} уже существует, удалите ее")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git("diff", "--binary", f"--output={patch}", *source.diff, cwd=repo_path)
    if not patch.stat().st_size:
        patch.unlink()
        raise BackportError(f"в {source.kind} нет изменений файлов")

    git("worktree", "add", "--detach", str(worktree), base_sha, cwd=repo_path)
    try:
        try:
            git("apply", "--3way", str(patch), cwd=worktree)
        except RuntimeError as e:
            conflicts = git("diff", "--name-only", "--diff-filter=U", cwd=worktree).split()
            raise BackportError("изменения не применились"
                                + (": конфликты в файлах " + ", ".join(conflicts) if conflicts else f" - {e}"))
        if not git("diff", "--cached", "--name-only", cwd=worktree):
            raise BackportError("изменения уже есть в ветке")
        git("switch", "-c", branch, cwd=worktree)
        git("commit", "-F", "-", cwd=worktree,
            input=f"#{item.id} {item.title}\n\nБэкпорт в {target}: {source.url}\n")
        git("push", remote, f"refs/heads/{branch}:refs/heads/{branch}", cwd=worktree)
    finally:
        git("worktree", "remove", "--force", str(worktree), cwd=repo_path)
        patch.unlink(missing_ok=True)
        try:
            git("branch", "-D", branch, cwd=repo_path)  # Ветка нужна только на сервере
        except RuntimeError:
            pass

    description = f"Карточка: {url}\n\nБэкпорт в ветку {target} изменений из {source.kind} {source.url}."
    try:
        template = pr_template(repo_path, base_sha, target)
    except RuntimeError as e:
        log.warning("Карточка %s: не удалось прочитать шаблон описания pull request: %s", item.id, e)
        template = ""
    return create_pull_request(repo_url, branch, target, f"[{target}] {source.title}", description, template=template)


def _commits(item: WorkItem, history: History) -> list:
    """Коммиты карточки: связанные в секции Development и по ссылкам из комментариев, без повторов.
    Коммит по ссылке из комментария, который не удалось получить, пропускается с предупреждением в логе."""
    found = {}
    for link in linked_commits(item.relations):
        commit = get_commit(link, item.collection)
        found.setdefault(commit.sha.lower(), commit)
    for link in links_in_comments(history.comments, COMMIT_LINK_RE):
        sha = link.rstrip("/").rsplit("/", 1)[-1].lower()
        if any(s.startswith(sha) for s in found):
            continue
        try:
            commit = get_commit(link, item.collection)
        except (RuntimeError, ValueError, requests.RequestException) as e:
            log.warning("Карточка %s: коммит из комментария пропущен: %s", item.id, e)
            continue
        found.setdefault(commit.sha.lower(), commit)
    return list(found.values())


def _pr_source(pr) -> Source:
    """Изменения pull request: для завершенного - что слияние добавило в целевую ветку, для остальных -
    изменения ветки pull request относительно общего предка с целевой веткой."""
    if not (pr.merge_target_commit and (pr.merge_commit if pr.status == "completed" else pr.merge_source_commit)):
        raise RuntimeError(f"TFS не вернул коммиты слияния pull request {pr.url}")
    if pr.status == "completed":
        diff, commits = [pr.merge_target_commit, pr.merge_commit], [pr.merge_target_commit, pr.merge_commit]
    else:
        diff = [f"{pr.merge_target_commit}...{pr.merge_source_commit}"]
        commits = [pr.merge_target_commit, pr.merge_source_commit]
    return Source("pull request", pr.url, pr.title, pr.repo_id, pr.created_by, pr.source_branch, diff, commits,
                  [pr.target_branch, pr.source_branch])


def _commit_source(commit, url: str, item: WorkItem) -> Source:
    """Изменения коммита относительно его первого родителя. Ветки исправления у коммита нет, поэтому ветки бэкпорта
    называются <префикс команды>/<номер карточки>-backport-<ветка версии>."""
    title = commit.message.strip().splitlines()[0] if commit.message.strip() else f"#{item.id} {item.title}"
    return Source("коммит", commit.url, title, commit.repo_id, commit.author,
                  f"{branch_prefix(url, item.area, '')}/{item.id}-backport", [f"{commit.sha}^", commit.sha],
                  [commit.sha], [])


def _find_repo(targets: dict, repo_id: str):
    """(репозиторий из targets с id repo_id, его url, remote) или None, если такого нет."""
    for tag, repo in targets.items():
        try:
            repo_url, _, remote = check_repo(repo.path)
            if repository_id(repo_url).lower() == repo_id.lower():
                return repo, repo_url, remote
        except (SystemExit, RuntimeError, ValueError, requests.RequestException) as e:
            log.warning("Репозиторий %s (%s) пропущен: %s", tag, repo.path, getattr(e, "code", e))
    return None


def _has_commit(repo: Path, sha: str) -> bool:
    try:
        git("cat-file", "-e", f"{sha}^{{commit}}", cwd=repo)
        return True
    except RuntimeError:
        return False


def _ensure_commits(repo: Path, remote: str, source: Source) -> None:
    """Получает с сервера коммиты, нужные для изменений source: из веток source.refs, иначе по sha."""
    missing = lambda: [c for c in source.commits if not _has_commit(repo, c)]
    for fetch in ([f"refs/heads/{r}" for r in source.refs if r], [*source.commits]):
        if not missing():
            return
        for ref in fetch:
            try:
                git("fetch", remote, ref, cwd=repo)
            except RuntimeError as e:
                log.debug("git fetch %s %s: %s", remote, ref, e)
    if left := missing():
        raise BackportError("не удалось получить с сервера коммиты " + ", ".join(c[:10] for c in left))


def _person(url: str, history: History) -> str:
    """Упоминание того, кто перенес карточку в Finalizing (если его нет - последнего ответственного)."""
    return mention(url, history.moved_by(FINALIZING_COLUMN) or history.last_assigned_to)


def _error_html(e: Exception) -> str:
    text = str(e) if isinstance(e, BackportError) else f"{type(e).__name__}: {e}"
    if len(text) > ERROR_LIMIT:
        text = text[:ERROR_LIMIT].rstrip() + " [...обрезано]"
    return html.escape(text)


def _comment(url: str, text: str) -> None:
    try:
        add_workitem_comment(url, text)
    except Exception as e:
        log.error("Не удалось добавить комментарий в карточку %s: %s", url, e)


def _remove_tag(bug_id: int, url: str) -> None:
    """Снимает тег BACKPORT_TAG, чтобы бэкпорт можно было запустить снова. Ошибка только пишется в лог."""
    try:
        if set_workitem_tag(url, BACKPORT_TAG, present=False):
            log.info("Карточка %s: тег %s снят", bug_id, BACKPORT_TAG)
    except Exception as e:
        log.warning("Карточка %s: не удалось снять тег %s: %s", bug_id, BACKPORT_TAG, e)
