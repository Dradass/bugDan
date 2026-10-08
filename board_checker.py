"""Проверки бага при переносе его карточки по столбцам доски (endpoint /check-work-item сервера).
Срабатывают только для карточек с тегом AICheck; что проверять, зависит от столбца, в который перенесли карточку."""
import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from bug_fixer import SLUG_MODEL, TODO_COLUMN, ask_claude
from tfs import (COMMENT_PREFIX, History, WorkItem, add_workitem_comment, html_to_text, linked_pull_requests,
                 load_history, load_workitem, mention_html, same_person)

CHECK_TAG = "AICheck"  # Тег карточки, при котором выполняются проверки
ANALYSIS_COLUMN, READY_FOR_TESTING_COLUMN, CLOSED_COLUMN = "Analysis", "Ready for testing", "Closed"
DECISION_MODEL = SLUG_MODEL  # Модель, которая оценивает объяснение ответственного, почему баг не будет исправлен
MAX_NOTES = 20000  # Сколько последних символов комментариев и дополнений ответственного передается Claude

# Ссылка на pull request: https://<сервер>/<коллекция>/<проект>/_git/<репозиторий>/pullrequest/<номер>
PR_LINK_RE = re.compile(r"https?://[^\s\"'<>]+/_git/[^/\s\"'<>]+/pullrequest/\d+", re.IGNORECASE)

DECISION_PROMPT = """Баг перенесен в столбец "{column}", но с ним не связан pull request с исправлением.
Ниже - комментарии ответственного за баг и текст, который он дописал в описание бага.
Определи, объяснил ли ответственный, почему баг не будет исправлен (например: не воспроизводится, дубликат,
поведение соответствует замыслу, уже исправлен в другой задаче, проблема в данных или настройках).
Первой строкой ответь только "ДА" или "НЕТ", второй строкой - обоснование одним предложением.

Баг #{id}: {title}
---
{notes}
---"""

MISSING_DECISION_COMMENT = """{person}, карточка перенесена в столбец {column}, но в ней нет информации о решении бага.<br>
Нужно одно из:<br>
1. Связанный pull request с исправлением в разделе Development - его нет.<br>
2. Ссылка на pull request в комментариях - ее нет.<br>
3. Объяснение ответственного в комментарии или в тексте бага, почему баг не будет исправлен - {explanation}.<br>
Добавьте недостающую информацию."""

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


def pr_link_in_comments(comments: list[tuple[dict, str]]) -> str | None:
    """Последняя ссылка на pull request в комментариях карточки (ищется в HTML, чтобы учесть href ссылок)."""
    for _, text in reversed(comments):
        if links := PR_LINK_RE.findall(text):
            return links[-1]
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


def assignee_explanation(item: WorkItem, history: History) -> tuple[str | None, str]:
    """(обоснование Claude, если последний ответственный объяснил, почему баг не будет исправлен, иначе None;
    чего не хватает в его записях - для комментария в карточку)."""
    person = history.last_assigned_to
    if not person:
        return None, "у карточки ни разу не был указан ответственный"
    name = person.get("displayName") or person.get("uniqueName")
    notes = assignee_notes(person, history)
    if not notes:
        return None, f"ответственный ({name}) не оставил комментариев и не дополнял текст бага"
    prompt = DECISION_PROMPT.format(column=READY_FOR_TESTING_COLUMN, id=item.id, title=item.title,
                                    notes="\n\n".join(notes)[-MAX_NOTES:])
    answer = ask_claude(Path(__file__).resolve().parent, prompt, DECISION_MODEL, tools="", max_turns=1)["result"]
    verdict, _, reason = answer.strip().partition("\n")
    reason = reason.strip() or verdict
    if re.sub(r"\W", "", verdict).upper() != "ДА":
        return None, (f"в комментариях и дополнениях текста бага ответственного ({name}) нет объяснения, "
                      f"почему баг не будет исправлен: {reason}")
    return f"объяснение ответственного {name}: {reason}", ""


def decision_evidence(item: WorkItem, history: History) -> tuple[str | None, str]:
    """(чем подтверждено решение бага - исправление или отказ от него, None - подтверждения нет;
    чего не хватает для подтверждения объяснением ответственного).
    Сначала проверяются pull request, затем объяснение ответственного - оно требует запроса к Claude."""
    if prs := linked_pull_requests(item.relations):
        return f"pull request в секции Development: {len(prs)}", ""
    if link := pr_link_in_comments(history.comments):
        return f"ссылка на pull request в комментарии: {link}", ""
    return assignee_explanation(item, history)


def missing_decision_comment(url: str, person: dict | None, missing: str) -> str:
    """HTML комментария о том, что в карточке нет информации о решении бага, с упоминанием последнего ответственного."""
    return MISSING_DECISION_COMMENT.format(person=mention_html(url, person) if person else "Ответственный не указан",
                                           column=html.escape(READY_FOR_TESTING_COLUMN),
                                           explanation=html.escape(missing))


def _check_decision(bug_id: int, url: str) -> None:
    item = load_workitem(url, with_comments=False)
    history = load_history(item)
    evidence, missing = decision_evidence(item, history)
    if evidence:
        log.info("Карточка %s: решение бага подтверждено - %s", bug_id, evidence)
        return
    add_workitem_comment(url, missing_decision_comment(url, history.last_assigned_to, missing))
    log.warning("Карточка %s: нет информации о решении бага (%s), в карточку добавлен комментарий", bug_id, missing)


def check_decision(bug_id: int, url: str) -> None:
    """Карточка перенесена в Ready for testing: проверка, что в ней есть информация о решении бага - связанный
    pull request, ссылка на него в комментариях или объяснение ответственного, почему баг не будет исправлен.
    Если ее нет, в карточку добавляется комментарий. Проверка идет в фоне."""
    log.info("Карточка %s: проверка решения бага поставлена в очередь", bug_id)
    _checks.submit(_run_check, _check_decision, bug_id, url)


def check_closing(bug_id: int, url: str) -> None:
    """Карточка перенесена в Closed: проверка закрытия бага."""
    log.info("Карточка %s: Check bug closing", bug_id)


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
