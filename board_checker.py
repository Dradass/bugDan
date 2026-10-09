"""Проверки бага при переносе его карточки по столбцам доски (endpoint /check-work-item сервера).
Срабатывают только для карточек с тегом AICheck; что проверять, зависит от столбца, в который перенесли карточку."""
import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import requests

from bug_fixer import SLUG_MODEL, TODO_COLUMN, ask_claude
from tfs import (COMMENT_PREFIX, History, PullRequest, WorkItem, add_workitem_comment, get_pull_request, html_to_text,
                 linked_commits, linked_pull_requests, load_history, load_workitem, mention_html, move_to_board_column,
                 same_person)

CHECK_TAG = "AICheck"  # Тег карточки, при котором выполняются проверки
ANALYSIS_COLUMN, READY_FOR_TESTING_COLUMN, CLOSED_COLUMN = "Analysis", "Ready for testing", "Closed"
FINALIZING_COLUMN = "Finalizing"  # Куда возвращается закрытая карточка, если ее pull request не завершены
DECISION_MODEL = SLUG_MODEL  # Модель, которая оценивает объяснение ответственного, почему баг не будет исправлен
MAX_NOTES = 20000  # Сколько последних символов комментариев и дополнений ответственного передается Claude

# Ссылка на pull request: https://<сервер>/<коллекция>/<проект>/_git/<репозиторий>/pullrequest/<номер>
PR_LINK_RE = re.compile(r"https?://[^\s\"'<>]+/_git/[^/\s\"'<>]+/pullrequest/\d+", re.IGNORECASE)
# Ссылка на коммит: https://<сервер>/<коллекция>/<проект>/_git/<репозиторий или его id>/commit/<sha>
COMMIT_LINK_RE = re.compile(r"https?://[^\s\"'<>]+/_git/[^/\s\"'<>]+/commit/[0-9a-f]{7,40}", re.IGNORECASE)

DECISION_PROMPT = """Баг перенесен в столбец "{column}", но с ним не связаны pull request или коммит с исправлением.
Ниже - комментарии ответственного за баг и текст, который он дописал в описание бага.
Определи, объяснил ли ответственный, почему баг не будет исправлен (например: не воспроизводится, дубликат,
поведение соответствует замыслу, уже исправлен в другой задаче, проблема в данных или настройках).
Первой строкой ответь только "ДА" или "НЕТ", второй строкой - обоснование одним предложением.

Баг #{id}: {title}
---
{notes}
---"""

MISSING_DECISION_COMMENT = """{person}, карточка перенесена в столбец {column}, но в ней нет информации о решении бага.<br>
{abandoned}Нужно одно из:<br>
1. Связанный pull request (не отмененный) или коммит с исправлением в разделе Development - их нет.<br>
2. Ссылка на pull request (не отмененный) или коммит в комментариях - ее нет.<br>
3. Объяснение ответственного в комментарии или в тексте бага, почему баг не будет исправлен - {explanation}.<br>
Добавьте недостающую информацию."""

UNFINISHED_PR_COMMENT = """{person}, карточка перенесена в столбец {column}, но связанные pull request не завершены:<br>
{prs}<br>
{moved}"""

ABANDONED_PR_BLOCK = """Все связанные pull request отменены и не считаются исправлением:<br>
{prs}<br>"""

PR_STATUSES = {"active": "в работе", "abandoned": "отменен", "completed": "завершен"}

log = logging.getLogger("bugdan")

# Проверки обращаются к TFS и Claude, поэтому идут в фоне по одной, а сервер сразу отвечает TFS.
_checks = ThreadPoolExecutor(max_workers=1, thread_name_prefix="board-check")


def _run_check(check: Callable[[int, str], None], bug_id: int, url: str) -> None:
    try:
        check(bug_id, url)
    except SystemExit as e:  # ask_claude сообщает об ошибке через sys.exit("текст")
        log.error("Карточка %s: ошибка проверки: %s", bug_id, e.code)
    except Exception:
        log.exception("Карточка %s: ошибка проверки", bug_id)


def check_description(bug_id: int, url: str) -> None:
    """Карточка перенесена в Analysis: проверка описания бага."""
    log.info("Карточка %s: Check bug description", bug_id)


def links_in_comments(comments: list[tuple[dict, str]], pattern: re.Pattern) -> list[str]:
    """Ссылки по шаблону pattern в комментариях карточки, новые первыми, без повторов
    (ищутся в HTML, чтобы учесть href ссылок)."""
    links = [link for _, text in reversed(comments) for link in reversed(pattern.findall(text))]
    return list(dict.fromkeys(links))


def pr_links_in_comments(comments: list[tuple[dict, str]]) -> list[str]:
    """Ссылки на pull request в комментариях карточки, новые первыми."""
    return links_in_comments(comments, PR_LINK_RE)


def commit_evidence(item: WorkItem, history: History) -> str | None:
    """Чем подтверждено исправление коммитом (без pull request): связь в секции Development
    или ссылка в комментарии; None, если коммита нет."""
    if commits := linked_commits(item.relations):
        return f"коммит в секции Development: {len(commits)}"
    if links := links_in_comments(history.comments, COMMIT_LINK_RE):
        return f"ссылка на коммит в комментарии: {links[0]}"
    return None


def assignee_notes(person: dict, history: History) -> list[str]:
    """Комментарии ответственного (кроме комментариев бота) и текст, который он дописал в поля бага."""
    notes = []
    for author, text in history.comments:
        text = html_to_text(text)
        if same_person(author, person) and not text.startswith(COMMENT_PREFIX.strip()):
            notes.append(f"Комментарий:\n{text}")
    notes += [f'Дописано в поле "{e.field}":\n{e.added}' for e in history.text_edits if same_person(e.author, person)]
    return notes


def assignee_explanation(item: WorkItem, history: History, column: str) -> tuple[str | None, str]:
    """(обоснование Claude, если последний ответственный объяснил, почему баг не будет исправлен, иначе None;
    чего не хватает в его записях - для комментария в карточку). column - столбец, в который перенесли карточку."""
    person = history.last_assigned_to
    if not person:
        return None, "у карточки ни разу не был указан ответственный"
    name = person.get("displayName") or person.get("uniqueName")
    notes = assignee_notes(person, history)
    if not notes:
        return None, f"ответственный ({name}) не оставил комментариев и не дополнял текст бага"
    prompt = DECISION_PROMPT.format(column=column, id=item.id, title=item.title,
                                    notes="\n\n".join(notes)[-MAX_NOTES:])
    answer = ask_claude(Path(__file__).resolve().parent, prompt, DECISION_MODEL, tools="", max_turns=1)["result"]
    verdict, _, reason = answer.strip().partition("\n")
    reason = reason.strip() or verdict
    if re.sub(r"\W", "", verdict).upper() != "ДА":
        return None, (f"в комментариях и дополнениях текста бага ответственного ({name}) нет объяснения, "
                      f"почему баг не будет исправлен: {reason}")
    return f"объяснение ответственного {name}: {reason}", ""


def mention(url: str, person: dict | None) -> str:
    """HTML упоминания последнего ответственного в начале комментария карточки."""
    return mention_html(url, person) if person else "Ответственный не указан"


def pull_requests(item: WorkItem, history: History) -> list[PullRequest]:
    """Pull request карточки: связанные в секции Development и по ссылкам из комментариев, без повторов.
    Ошибка получения связанного pull request прерывает проверку, а pull request по ссылке из комментария
    пропускается: ссылка может быть устаревшей или вести туда, куда нет доступа."""
    found = [get_pull_request(link, item.collection) for link in linked_pull_requests(item.relations)]
    for link in pr_links_in_comments(history.comments):
        try:
            found.append(get_pull_request(link, item.collection))
        except (RuntimeError, ValueError, requests.RequestException) as e:
            log.warning("Карточка %s: pull request из комментария пропущен: %s", item.id, e)
    return list({pr.url.lower(): pr for pr in found}.values())


def unfinished_pull_requests(prs: list[PullRequest]) -> list[PullRequest]:
    """Незавершенные pull request: те, что еще в работе, а если таких нет и ни один не завершен - все (отмененные)."""
    active = [pr for pr in prs if pr.status not in ("completed", "abandoned")]
    if active or any(pr.status == "completed" for pr in prs):
        return active
    return prs


def pr_list_html(prs: list[PullRequest]) -> str:
    """HTML списка pull request со ссылками и состояниями для комментария карточки."""
    return "<br>".join(f'- <a href="{html.escape(pr.url)}">{html.escape(pr.url)}</a> - '
                       f"{html.escape(PR_STATUSES.get(pr.status, pr.status or '?'))}" for pr in prs)


def unfinished_pr_comment(url: str, person: dict | None, prs: list[PullRequest], moved: str) -> str:
    """HTML комментария о незавершенных pull request с упоминанием последнего ответственного."""
    return UNFINISHED_PR_COMMENT.format(person=mention(url, person), column=html.escape(CLOSED_COLUMN),
                                        prs=pr_list_html(prs), moved=html.escape(moved))


def missing_decision_comment(url: str, person: dict | None, column: str, missing: str,
                             abandoned: list[PullRequest]) -> str:
    """HTML комментария о том, что в карточке нет информации о решении бага, с упоминанием последнего ответственного.
    abandoned - отмененные pull request карточки (у нее нет других)."""
    return MISSING_DECISION_COMMENT.format(
        person=mention(url, person), column=html.escape(column), explanation=html.escape(missing),
        abandoned=ABANDONED_PR_BLOCK.format(prs=pr_list_html(abandoned)) if abandoned else "")


def report_missing_decision(item: WorkItem, url: str, history: History, column: str,
                            abandoned: list[PullRequest] = ()) -> None:
    """Проверка решения бага, когда у карточки нет pull request или все они отменены (abandoned): исправление
    коммитом, а если его нет - объяснение ответственного, почему баг не будет исправлен (требует запроса к Claude).
    Если нет и его, добавляет в карточку комментарий о том, чего не хватает."""
    if evidence := commit_evidence(item, history):
        log.info("Карточка %s: решение бага подтверждено - %s", item.id, evidence)
        return
    evidence, missing = assignee_explanation(item, history, column)
    if evidence:
        log.info("Карточка %s: решение бага подтверждено - %s", item.id, evidence)
        return
    add_workitem_comment(url, missing_decision_comment(url, history.last_assigned_to, column, missing, list(abandoned)))
    log.warning("Карточка %s: нет информации о решении бага (%s%s), в карточку добавлен комментарий", item.id,
                missing, f"; отменены pull request: {', '.join(pr.url for pr in abandoned)}" if abandoned else "")


def _check_decision(bug_id: int, url: str) -> None:
    """Сначала проверяются pull request, затем коммиты и объяснение ответственного."""
    item = load_workitem(url, with_comments=False)
    history = load_history(item)
    prs = pull_requests(item, history)
    if alive := [pr for pr in prs if pr.status != "abandoned"]:
        log.info("Карточка %s: решение бага подтверждено - pull request: %s", bug_id,
                 ", ".join(f"{pr.url} ({pr.status})" for pr in alive))
        return
    report_missing_decision(item, url, history, READY_FOR_TESTING_COLUMN, abandoned=prs)


def check_decision(bug_id: int, url: str) -> None:
    """Карточка перенесена в Ready for testing: проверка, что в ней есть информация о решении бага - pull request
    (не отмененный) или коммит, связанные в секции Development или по ссылке из комментария, либо объяснение
    ответственного, почему баг не будет исправлен. Если ее нет, в карточку добавляется комментарий.
    Проверка идет в фоне."""
    log.info("Карточка %s: проверка решения бага поставлена в очередь", bug_id)
    _checks.submit(_run_check, _check_decision, bug_id, url)


def _check_closing(bug_id: int, url: str) -> None:
    item = load_workitem(url, with_comments=False)
    history = load_history(item)
    prs = pull_requests(item, history)
    if not prs:
        report_missing_decision(item, url, history, CLOSED_COLUMN)
        return
    unfinished = unfinished_pull_requests(prs)
    if not unfinished:
        log.info("Карточка %s: все pull request завершены или отменены: %s", bug_id,
                 ", ".join(f"{pr.url} ({pr.status})" for pr in prs))
        return
    try:
        result = move_to_board_column(url, FINALIZING_COLUMN, only_from=CLOSED_COLUMN)
        moved = f"Карточка возвращена в столбец {FINALIZING_COLUMN}."
        log.info("Карточка %s: %s", bug_id, result)
    except Exception as e:
        moved = f"Карточку не удалось вернуть в столбец {FINALIZING_COLUMN}."
        log.error("Карточка %s: не удалось перенести в столбец %s: %s", bug_id, FINALIZING_COLUMN, e)
    add_workitem_comment(url, unfinished_pr_comment(url, history.last_assigned_to, unfinished, moved))
    log.warning("Карточка %s: pull request не завершены (%s), в карточку добавлен комментарий", bug_id,
                ", ".join(f"{pr.url} ({pr.status})" for pr in unfinished))


def check_closing(bug_id: int, url: str) -> None:
    """Карточка перенесена в Closed: проверка, что ее pull request завершены, а если их нет - что баг исправлен
    коммитом или ответственный объяснил, почему баг не будет исправлен. Если pull request не завершены, карточка
    возвращается в Finalizing, а в нее добавляется комментарий; если нет ни pull request, ни коммита,
    ни объяснения - только комментарий.
    Проверка идет в фоне."""
    log.info("Карточка %s: проверка закрытия бага поставлена в очередь", bug_id)
    _checks.submit(_run_check, _check_closing, bug_id, url)


def check_board_column(bug_id: int, url: str, column: str, fix: Callable[[int, str], None]) -> bool:
    """Выполняет действие для столбца column, в который перенесли карточку: проверку бага, а для TODO -
    исправление (fix(bug_id, url) ставит его в очередь). False, если для этого столбца действия нет."""
    actions = {
        ANALYSIS_COLUMN: check_description,
        READY_FOR_TESTING_COLUMN: check_decision,
        CLOSED_COLUMN: check_closing,
        TODO_COLUMN: fix,
    }
    action = next((a for name, a in actions.items() if name.lower() == column.strip().lower()), None)
    if action is None:
        return False
    action(bug_id, url)
    return True
