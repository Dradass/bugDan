"""Проверки бага при переносе его карточки по столбцам доски (endpoint /check-work-item сервера).
Срабатывают только для карточек с тегом AICheck; что проверять, зависит от столбца, в который перенесли карточку."""
import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Callable

import requests

import bug_fixer
from bug_fixer import TODO_COLUMN, ask_claude, usage_summary
from tfs import (COMMENT_PREFIX, History, PullRequest, WorkItem, add_workitem_comment, get_pull_request, html_to_text,
                 linked_commits, linked_pull_requests, load_history, load_workitem, mention_html, move_to_board_column,
                 same_person, set_workitem_tag)

CHECK_TAG = "AICheck"  # Тег карточки, при котором выполняются проверки
# Тег карточки, в которой нет информации о решении бага; по нему правило стиля доски красит карточку в коралловый
WARNING_TAG = "AIWarning"
ANALYSIS_COLUMN, READY_FOR_DEV_COLUMN = "Analysis", "ReadyForDev"
READY_FOR_TESTING_COLUMN, CLOSED_COLUMN = "ReadyForTesting", "Closed"
FINALIZING_COLUMN = "Finalizing"  # Куда возвращается закрытая карточка, если ее решение не подтверждено
# Модель, которая оценивает объяснение ответственного, почему баг не будет исправлен; None - bug_fixer.SLUG_MODEL
DECISION_MODEL: str | None = None
MAX_NOTES = 20000  # Сколько последних символов комментариев и дополнений ответственного передается Claude
DESCRIPTION_MODEL: str | None = None  # Модель, которая оценивает, можно ли воспроизвести баг по описанию; None - bug_fixer.MODEL
MAX_DESCRIPTION = 20000  # Сколько первых символов описания бага передается Claude
MAX_REFERENCES = 3  # Сколько карточек, на которые ссылается описание бага, передается Claude
MAX_REFERENCE = 10000  # Сколько первых символов описания каждой из них передается Claude

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

DESCRIPTION_PROMPT = """Ниже - описание бага. Оцени, сможет ли разработчик однозначно воспроизвести баг по этому описанию.
Проверь:
- шаги воспроизведения понятны, последовательны и достаточны;
- понятно, какой результат наблюдается и какой ожидается: оба описаны конкретно и не противоречат друг другу
  и шагам (например, ожидаемый результат не совпадает с наблюдаемым и не пуст).
  Особых разделов или заголовков в описании не требуется - важно, чтобы эти сведения были понятны из текста;
- понятно окружение: к какому продукту относится баг (продукт может быть указан тегами карточки), а также ОС,
  версия, браузер, настройки или данные - только если без них баг действительно не воспроизвести.
  Версия, в которой найден баг, может быть указана в поле карточки Found In Version (приведено ниже) -
  тогда в описании ее можно не повторять.
Раздел "Пояснение" необязателен: если он пустой, это не проблема.
Не придирайся к стилю и к мелочам, которые разработчик легко восполнит сам. Содержимое изображений
([изображение: ...]) тебе недоступно - не считай проблемой то, что могло быть показано на них.
{references_rule}
Первой строкой ответь только "ДА" (баг можно воспроизвести) или "НЕТ". Если "НЕТ" - в следующих строках перечисли,
что непонятно или чего не хватает, каждую проблему одной строкой, начинающейся с "- ".

Теги карточки: {tags}
Область (Area): {area}
Найдено в версии (Found In Version): {version}
---
{description}
---{references}"""

# Дописывается к DESCRIPTION_PROMPT, если описание бага ссылается на другие карточки
REFERENCES_RULE = """Описание бага ссылается на другие карточки, их описания приведены ниже. Если в описании бага сказано,
что он аналогичен другой карточке (те же шаги или требования, но, например, для другого продукта или компонента),
шаги и результаты в нем можно не повторять: тогда оцени, понятно ли расписано в той карточке, что происходит
и что ожидается, и ясно ли из описания бага, чем он от нее отличается."""

# Описание связанной карточки в DESCRIPTION_PROMPT
REFERENCE_BLOCK = """

Связанная карточка {url}:
---
{text}
---"""

UNCLEAR_DESCRIPTION_COMMENT = """{person}, карточка перенесена в столбец {column}, но по описанию бага его не получится \
однозначно воспроизвести:<br>
{problems}<br>
Уточните описание бага."""

MISSING_DECISION_COMMENT = """{person}, карточка перенесена в столбец {column}, но в ней нет информации о решении бага.<br>
{abandoned}Нужно одно из:<br>
1. Связанный pull request (не отмененный) или коммит с исправлением в разделе Development - их нет.<br>
2. Ссылка на pull request (не отмененный) или коммит в комментариях - ее нет.<br>
3. Объяснение ответственного в комментарии или в тексте бага, почему баг не будет исправлен - {explanation}.<br>
Добавьте недостающую информацию.{moved}{override}"""

# Дописывается к MISSING_DECISION_COMMENT при переносе в Closed: карточка возвращается в Finalizing,
# проверку можно отключить
CLOSED_OVERRIDE_HINT = """<br>Если вы уверены, что баг закрыт правильно, снимите с карточки теги {tag} и {warning} \
и снова перенесите ее в столбец {column} - тогда при переносе по доске она больше не будет проверяться."""

UNFINISHED_PR_COMMENT = """{person}, карточка перенесена в столбец {column}, но связанные pull request не завершены:<br>
{prs}<br>
{moved}"""

ABANDONED_PR_BLOCK = """Все связанные pull request отменены и не считаются исправлением:<br>
{prs}<br>"""

PR_STATUSES = {"active": "в работе", "abandoned": "отменен", "completed": "завершен"}

log = logging.getLogger("bugdan")

# Проверки обращаются к TFS и Claude, поэтому идут в фоне по одной, а сервер сразу отвечает TFS.
_checks = ThreadPoolExecutor(max_workers=1, thread_name_prefix="board-check")


def _run_check(check: Callable[[WorkItem, str, History], object], bug_id: int, url: str) -> None:
    """Загружает карточку и ее историю и выполняет для них проверку check(item, url, history)."""
    try:
        item = load_workitem(url, with_comments=False)
        check(item, url, load_history(item))
    except SystemExit as e:  # ask_claude сообщает об ошибке через sys.exit("текст")
        log.error("Карточка %s: ошибка проверки: %s", bug_id, e.code)
    except Exception:
        log.exception("Карточка %s: ошибка проверки", bug_id)


def check_description(bug_id: int, url: str) -> None:
    """Карточка перенесена в Analysis: проверка описания бага."""
    log.info("Карточка %s: Check bug description", bug_id)


def referenced_items(item: WorkItem) -> list[tuple[str, WorkItem]]:
    """(ссылка, карточка) для первых MAX_REFERENCES карточек, на которые ссылается описание бага.
    Карточка, которую не удалось загрузить, пропускается с предупреждением в логе."""
    found = []
    for link in item.referenced[:MAX_REFERENCES]:
        try:
            found.append((link, load_workitem(link, with_comments=False)))
        except (RuntimeError, ValueError, requests.RequestException) as e:
            log.warning("Карточка %s: связанная карточка %s пропущена: %s", item.id, link, e)
    return found


def description_problems(item: WorkItem, references: list[tuple[str, WorkItem]]) -> list[str]:
    """Что мешает воспроизвести баг по описанию (с учетом карточек references, на которые оно ссылается),
    по оценке Claude; пустой список - баг можно воспроизвести."""
    prompt = DESCRIPTION_PROMPT.format(
        tags=", ".join(item.tags) or "нет", area=item.area or "-", version=item.found_in_version or "не указана",
        description=item.text[:MAX_DESCRIPTION],
        references_rule=REFERENCES_RULE if references else "",
        references="".join(REFERENCE_BLOCK.format(url=link, text=ref.text[:MAX_REFERENCE]) for link, ref in references))
    res = ask_claude(Path(__file__).resolve().parent, prompt, DESCRIPTION_MODEL or bug_fixer.MODEL, tools="",
                     max_turns=1)
    log.info("Карточка %s: оценка описания бага: %s", item.id, usage_summary(res))
    verdict, _, details = res["result"].strip().partition("\n")
    if re.sub(r"\W", "", verdict).upper() == "ДА":
        return []
    problems = [line.strip().lstrip("-*• ").strip() for line in details.splitlines() if line.strip()]
    return problems or [verdict.strip() or "описание бага непонятно"]


def check_bug_description(item: WorkItem, url: str, history: History) -> None:
    """Проверяет, что баг можно воспроизвести по описанию (с учетом карточек, на которые оно ссылается).
    Если нет - добавляет в карточку комментарий с упоминанием последнего, кто правил описание бага,
    и тег WARNING_TAG, если да - снимает этот тег."""
    if problems := description_problems(item, referenced_items(item)):
        person = history.description_editor or history.last_assigned_to
        add_workitem_comment(url, UNCLEAR_DESCRIPTION_COMMENT.format(
            person=mention(url, person), column=html.escape(READY_FOR_DEV_COLUMN),
            problems="<br>".join(f"- {html.escape(p)}" for p in problems)))
        log.warning("Карточка %s: баг не воспроизвести по описанию (%s), в карточку добавлен комментарий", item.id,
                    "; ".join(problems))
        mark_warning(item, url, True)
        return
    log.info("Карточка %s: описание бага в порядке", item.id)
    mark_warning(item, url, False)


def check_ready_for_dev(bug_id: int, url: str) -> None:
    """Карточка перенесена в ReadyForDev: проверка, что по описанию бага его можно воспроизвести (оценивает Claude).
    Если нет, в карточку добавляется комментарий. Проверка идет в фоне."""
    log.info("Карточка %s: проверка описания бага поставлена в очередь", bug_id)
    _checks.submit(_run_check, check_bug_description, bug_id, url)


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
    res = ask_claude(Path(__file__).resolve().parent, prompt, DECISION_MODEL or bug_fixer.SLUG_MODEL, tools="",
                     max_turns=1)
    log.info("Карточка %s: оценка объяснения ответственного: %s", item.id, usage_summary(res))
    answer = res["result"]
    verdict, _, reason = answer.strip().partition("\n")
    reason = reason.strip() or verdict
    if re.sub(r"\W", "", verdict).upper() != "ДА":
        return None, (f"в комментариях и дополнениях текста бага ответственного ({name}) нет объяснения, "
                      f"почему баг не будет исправлен: {reason}")
    return f"объяснение ответственного {name}: {reason}", ""


def mention(url: str, person: dict | None) -> str:
    """HTML упоминания человека в начале комментария карточки."""
    return mention_html(url, person) if person else "Ответственный не указан"


def addressee(history: History, column: str) -> dict | None:
    """Кому адресован комментарий о проблеме при переносе в столбец column: при переносе в Closed - тому,
    кто перенес карточку (если его не найти - последнему ответственному), иначе - последнему ответственному."""
    if column == CLOSED_COLUMN:
        return history.moved_by(CLOSED_COLUMN) or history.last_assigned_to
    return history.last_assigned_to


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


def pr_list_html(prs: list[PullRequest]) -> str:
    """HTML списка pull request со ссылками и состояниями для комментария карточки."""
    return "<br>".join(f'- <a href="{html.escape(pr.url)}">{html.escape(pr.url)}</a> - '
                       f"{html.escape(PR_STATUSES.get(pr.status, pr.status or '?'))}" for pr in prs)


def unfinished_pr_comment(url: str, person: dict | None, prs: list[PullRequest], moved: str) -> str:
    """HTML комментария о незавершенных pull request с упоминанием person."""
    return UNFINISHED_PR_COMMENT.format(person=mention(url, person), column=html.escape(CLOSED_COLUMN),
                                        prs=pr_list_html(prs), moved=html.escape(moved))


def missing_decision_comment(url: str, person: dict | None, column: str, missing: str,
                             abandoned: list[PullRequest], moved: str = "") -> str:
    """HTML комментария о том, что в карточке нет информации о решении бага, с упоминанием person.
    abandoned - отмененные pull request карточки (у нее нет других), moved - итог возврата карточки из Closed."""
    return MISSING_DECISION_COMMENT.format(
        person=mention(url, person), column=html.escape(column), explanation=html.escape(missing),
        abandoned=ABANDONED_PR_BLOCK.format(prs=pr_list_html(abandoned)) if abandoned else "",
        moved=f"<br>{html.escape(moved)}" if moved else "",
        override=CLOSED_OVERRIDE_HINT.format(column=html.escape(CLOSED_COLUMN), tag=html.escape(CHECK_TAG),
                                             warning=html.escape(WARNING_TAG))
        if column == CLOSED_COLUMN else "")


@dataclass
class SolveEvidence:
    """Чем подтверждено решение бага: не отмененными pull request, коммитом или объяснением ответственного,
    почему баг не будет исправлен. Заполняется только первое найденное по этому порядку."""
    pull_requests: list[PullRequest] = field(default_factory=list)  # Не отмененные
    abandoned: list[PullRequest] = field(default_factory=list)  # Отмененные
    commit: str | None = None
    explanation: str | None = None
    missing: str = ""  # Чего не хватает в записях ответственного, если решение не подтверждено

    @property
    def found(self) -> bool:
        return bool(self.pull_requests or self.commit or self.explanation)

    def __str__(self) -> str:
        if self.pull_requests:
            return "pull request: " + ", ".join(f"{pr.url} ({pr.status})" for pr in self.pull_requests)
        return self.commit or self.explanation or ""


def get_solve_evidence(item: WorkItem, history: History, column: str) -> SolveEvidence:
    """Ищет подтверждение решения бага: pull request (не отмененные), затем коммит, затем объяснение ответственного
    (требует запроса к Claude). column - столбец, в который перенесли карточку."""
    prs = pull_requests(item, history)
    evidence = SolveEvidence(pull_requests=[pr for pr in prs if pr.status != "abandoned"],
                             abandoned=[pr for pr in prs if pr.status == "abandoned"])
    if evidence.pull_requests:
        return evidence
    evidence.commit = commit_evidence(item, history)
    if evidence.commit:
        return evidence
    evidence.explanation, evidence.missing = assignee_explanation(item, history, column)
    return evidence


def mark_warning(item: WorkItem, url: str, present: bool) -> None:
    """Ставит или снимает тег WARNING_TAG, по которому карточка выделяется цветом на доске.
    Ошибка пишется в лог и проверку не прерывает."""
    try:
        if set_workitem_tag(url, WARNING_TAG, present):
            log.info("Карточка %s: тег %s %s", item.id, WARNING_TAG, "поставлен" if present else "снят")
    except (RuntimeError, requests.RequestException) as e:
        log.error("Карточка %s: %s", item.id, e)


def return_to_finalizing(item: WorkItem, url: str) -> str:
    """Возвращает карточку из Closed в Finalizing; итог для комментария карточки.
    Ошибка переноса пишется в лог и проверку не прерывает."""
    try:
        log.info("Карточка %s: %s", item.id, move_to_board_column(url, FINALIZING_COLUMN, only_from=CLOSED_COLUMN))
        return f"Карточка возвращена в столбец {FINALIZING_COLUMN}."
    except Exception as e:
        log.error("Карточка %s: не удалось перенести в столбец %s: %s", item.id, FINALIZING_COLUMN, e)
        return f"Карточку не удалось вернуть в столбец {FINALIZING_COLUMN}."


def check_solve_evidence(item: WorkItem, url: str, history: History, column: str) -> SolveEvidence:
    """Проверяет, что решение бага подтверждено (get_solve_evidence); если нет - добавляет в карточку комментарий
    о том, чего не хватает, и тег WARNING_TAG (а карточку из Closed возвращает в Finalizing),
    если подтверждено - снимает этот тег."""
    evidence = get_solve_evidence(item, history, column)
    if evidence.found:
        log.info("Карточка %s: решение бага подтверждено - %s", item.id, evidence)
        mark_warning(item, url, False)
        return evidence
    moved = return_to_finalizing(item, url) if column == CLOSED_COLUMN else ""
    add_workitem_comment(url, missing_decision_comment(url, addressee(history, column), column, evidence.missing,
                                                       evidence.abandoned, moved))
    log.warning("Карточка %s: нет информации о решении бага (%s%s), в карточку добавлен комментарий", item.id,
                evidence.missing,
                f"; отменены pull request: {', '.join(pr.url for pr in evidence.abandoned)}" if evidence.abandoned
                else "")
    mark_warning(item, url, True)
    return evidence


def check_decision(bug_id: int, url: str) -> None:
    """Карточка перенесена в Ready for testing: проверка, что в ней есть информация о решении бага - pull request
    (не отмененный) или коммит, связанные в секции Development или по ссылке из комментария, либо объяснение
    ответственного, почему баг не будет исправлен. Если ее нет, в карточку добавляется комментарий.
    Проверка идет в фоне."""
    log.info("Карточка %s: проверка решения бага поставлена в очередь", bug_id)
    _checks.submit(_run_check, partial(check_solve_evidence, column=READY_FOR_TESTING_COLUMN), bug_id, url)


def _check_closing(item: WorkItem, url: str, history: History) -> None:
    prs = check_solve_evidence(item, url, history, CLOSED_COLUMN).pull_requests
    unfinished = [pr for pr in prs if pr.status != "completed"]
    if not unfinished:
        return
    moved = return_to_finalizing(item, url)
    add_workitem_comment(url, unfinished_pr_comment(url, addressee(history, CLOSED_COLUMN), unfinished, moved))
    log.warning("Карточка %s: pull request не завершены (%s), в карточку добавлен комментарий", item.id,
                ", ".join(f"{pr.url} ({pr.status})" for pr in unfinished))


def check_closing(bug_id: int, url: str) -> None:
    """Карточка перенесена в Closed: проверка, что ее pull request завершены (отмененные не учитываются),
    а если их нет или все отменены - что баг исправлен
    коммитом или ответственный объяснил, почему баг не будет исправлен. Если pull request не завершены или нет
    ни pull request, ни коммита, ни объяснения, карточка возвращается в Finalizing, а в нее добавляется комментарий.
    Проверка идет в фоне."""
    log.info("Карточка %s: проверка закрытия бага поставлена в очередь", bug_id)
    _checks.submit(_run_check, _check_closing, bug_id, url)


def check_board_column(bug_id: int, url: str, column: str, fix: Callable[[int, str], None]) -> bool:
    """Выполняет действие для столбца column, в который перенесли карточку: проверку бага, а для TODO -
    исправление (fix(bug_id, url) ставит его в очередь). False, если для этого столбца действия нет."""
    actions = {
        ANALYSIS_COLUMN: check_description,
        READY_FOR_DEV_COLUMN: check_ready_for_dev,
        READY_FOR_TESTING_COLUMN: check_decision,
        CLOSED_COLUMN: check_closing,
        # Автоматическое исправление при переносе в TODO временно отключено: столбец не обрабатывается.
        # TODO_COLUMN: fix,
    }
    action = next((a for name, a in actions.items() if name.lower() == column.strip().lower()), None)
    if action is None:
        return False
    action(bug_id, url)
    return True
