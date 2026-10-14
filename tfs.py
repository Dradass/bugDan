"""Загрузка карточки (work item) из TFS / Azure DevOps Server в виде текста для промпта."""
import difflib
import json
import os
import re
from dataclasses import dataclass, field
from html import escape, unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote, unquote

import requests

API_VERSION = "5.0"
COMMENTS_API_VERSION = "5.0-preview.2"

# Поля карточки, которые попадают в промпт (в указанном порядке).
TEXT_FIELDS = {
    "Microsoft.VSTS.TCM.ReproSteps": "Шаги воспроизведения",
    "System.Description": "Описание",
    "Microsoft.VSTS.Common.AcceptanceCriteria": "Критерии приемки",
    "Microsoft.VSTS.TCM.SystemInfo": "Системная информация",
}


class _HtmlToText(HTMLParser):
    """Простое преобразование HTML полей TFS в читаемый текст."""

    BLOCK_TAGS = {"p", "div", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.lists = []  # стек: None для <ul>, счётчик для <ol>

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "ul":
            self.lists.append(None)
        elif tag == "ol":
            self.lists.append(0)
        elif tag == "li":
            indent = "  " * max(len(self.lists) - 1, 0)
            if self.lists and self.lists[-1] is not None:
                self.lists[-1] += 1
                self.parts.append(f"\n{indent}{self.lists[-1]}. ")
            else:
                self.parts.append(f"\n{indent}- ")
        elif tag == "img":
            self.parts.append(f"[изображение: {dict(attrs).get('src', '')}]")
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in ("ul", "ol") and self.lists:
            self.lists.pop()
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data.replace("\xa0", " "))

    def text(self):
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()


def html_to_text(html: str) -> str:
    parser = _HtmlToText()
    parser.feed(html)
    return parser.text()


def parse_workitem_url(url: str) -> tuple[str, str, int]:
    """Возвращает (url коллекции, проект, id) из ссылки на карточку.

    Поддерживаются ссылки вида .../<Коллекция>/<Проект>/_boards/...?workitem=ID
    и .../<Коллекция>/<Проект>/_workitems/edit/ID.
    """
    u = urlparse(url)
    segments = [s for s in u.path.split("/") if s]
    idx = next((i for i, s in enumerate(segments) if s.startswith("_")), None)
    if idx is None or idx < 2:
        raise ValueError(f"Не удалось разобрать ссылку на карточку: {url}")

    collection = f"{u.scheme}://{u.netloc}/" + "/".join(segments[:idx - 1])
    project = segments[idx - 1]

    query = parse_qs(u.query)
    if "workitem" in query:
        item_id = query["workitem"][0]
    elif segments[idx] == "_workitems" and segments[-1].isdigit():
        item_id = segments[-1]
    else:
        raise ValueError(f"В ссылке нет номера карточки: {url}")
    return collection, project, int(item_id)


def area_team(area: str) -> str | None:
    r"""Команда из Area карточки: уровень сразу под проектом (SmartInstruments\Vega\UI -> Vega);
    None, если Area - корень проекта."""
    parts = [s for s in area.split("\\") if s.strip()]
    return parts[1].strip() if len(parts) > 1 else None


def parse_team(url: str) -> str | None:
    """Команда из ссылки на доску вида .../_boards/board/t/<Команда>/...; None, если ее в ссылке нет."""
    segments = [s for s in urlparse(url).path.split("/") if s]
    if "t" in segments and segments.index("t") + 1 < len(segments):
        return unquote(segments[segments.index("t") + 1])
    return None


def _session() -> requests.Session:
    """PAT из переменной TFS_PAT, иначе Windows-аутентификация текущего пользователя."""
    s = requests.Session()
    pat = os.environ.get("TFS_PAT")
    if pat:
        s.auth = ("", pat)
    else:
        try:
            from requests_negotiate_sspi import HttpNegotiateAuth
        except ImportError:
            raise SystemExit("Задайте TFS_PAT или установите пакет requests-negotiate-sspi "
                             "для входа под доменной учетной записью.")
        s.auth = HttpNegotiateAuth()
    return s


PR_DESCRIPTION_LIMIT = 4000  # ограничение TFS на длину описания pull request


def parse_git_url(url: str) -> tuple[str, str, str]:
    """Возвращает (url коллекции, проект, репозиторий) из ссылки вида .../<Коллекция>/<Проект>/_git/<Репозиторий>."""
    u = urlparse(url)
    segments = [s for s in u.path.split("/") if s]
    if "_git" not in segments or segments.index("_git") + 1 >= len(segments):
        raise ValueError(f"Не удалось разобрать ссылку на git-репозиторий: {url}")
    idx = segments.index("_git")
    repo = segments[idx + 1].removesuffix(".git")
    if idx >= 2:
        collection_path, project = segments[:idx - 1], segments[idx - 1]
    else:  # .../<Коллекция>/_git/<Репозиторий>: проект называется так же, как репозиторий
        collection_path, project = segments[:idx], repo
    return f"{u.scheme}://{u.netloc}/" + "/".join(collection_path), project, repo


def create_pull_request(repo_url: str, source_branch: str, target_branch: str,
                        title: str, description: str, template: str = "", draft: bool = False) -> str:
    """Создает pull request и возвращает ссылку на него.
    template - шаблон описания из репозитория, добавляется после description. Если описание не помещается
    в лимит TFS, в первую очередь обрезается description, но ему остается не меньше половины лимита."""
    collection, project, repo = parse_git_url(repo_url)
    tail = "\n\n---\n\n" + template.strip() if template.strip() else ""
    limit = max(PR_DESCRIPTION_LIMIT - len(tail), PR_DESCRIPTION_LIMIT // 2)
    if len(description) > limit:
        description = description[:limit - 20].rstrip() + "\n\n[...обрезано]"
    description += tail
    if len(description) > PR_DESCRIPTION_LIMIT:
        description = description[:PR_DESCRIPTION_LIMIT - 20].rstrip() + "\n\n[...обрезано]"
    r = _session().post(
        f"{collection}/{project}/_apis/git/repositories/{repo}/pullrequests",
        params={"api-version": API_VERSION},
        json={
            "sourceRefName": f"refs/heads/{source_branch}",
            "targetRefName": f"refs/heads/{target_branch}",
            "title": title,
            "description": description,
            "isDraft": draft,
        },
    )
    if not r.ok:
        raise RuntimeError(f"Не удалось создать pull request: HTTP {r.status_code} {r.text[:500]}")
    return f"{collection}/{project}/_git/{repo}/pullrequest/{r.json()['pullRequestId']}"


def _identity_id(s: requests.Session, collection: str, person: dict) -> str:
    """id пользователя TFS по IdentityRef из карточки."""
    if person.get("id"):
        return person["id"]
    # Старые версии TFS отдают в поле карточки только строку "Имя <DOMAIN\login>"
    r = s.get(f"{collection}/_apis/identities",
              params={"api-version": API_VERSION, "searchFilter": "General", "filterValue": person["uniqueName"]})
    if not r.ok:
        raise RuntimeError(f"Не удалось найти пользователя {person['uniqueName']}: "
                           f"HTTP {r.status_code} {r.text[:500]}")
    found = r.json().get("value") or []
    if not found:
        raise RuntimeError(f"Пользователь {person['uniqueName']} не найден в TFS")
    return found[0]["id"]


def add_required_reviewer(pr_url: str, person: dict) -> None:
    """Добавляет в pull request обязательного рецензента. person - IdentityRef из карточки (WorkItem.assigned_to)."""
    collection, project, repo = parse_git_url(pr_url)
    pr_id = pr_url.rstrip("/").rsplit("/", 1)[-1]
    s = _session()
    reviewer_id = _identity_id(s, collection, person)
    r = s.put(f"{collection}/{project}/_apis/git/repositories/{repo}/pullRequests/{pr_id}/reviewers/{reviewer_id}",
              params={"api-version": API_VERSION}, json={"id": reviewer_id, "vote": 0, "isRequired": True})
    if not r.ok:
        raise RuntimeError(f"Не удалось добавить рецензента {person.get('displayName')} в pull request {pr_id}: "
                           f"HTTP {r.status_code} {r.text[:500]}")


def _team_board(s: requests.Session, collection: str, project: str, team: str | None, fields: dict) -> dict | None:
    """Доска команды team (None - команда проекта по умолчанию), на которой находится карточка:
    в карточке заполнено поле столбца этой доски (WEF_<guid>_Kanban.Column)."""
    base = f"{collection}/{project}" + (f"/{team}" if team else "")
    r = s.get(f"{base}/_apis/work/boards", params={"api-version": API_VERSION})
    if r.status_code == 404:  # Команды с таким именем нет
        return None
    r.raise_for_status()
    for ref in r.json().get("value", []):
        board = s.get(ref["url"], params={"api-version": API_VERSION})
        board.raise_for_status()
        board = board.json()
        column_field = ((board.get("fields") or {}).get("columnField") or {}).get("referenceName")
        if column_field and fields.get(column_field):
            return board
    return None


def move_to_board_column(url: str, column: str, only_from: str | None = None) -> str:
    """Переносит карточку в столбец column доски ее команды; состояние карточки меняется на связанное со столбцом.
    only_from - переносить, только если карточка сейчас в этом столбце. Возвращает итог для лога."""
    collection, _, item_id = parse_workitem_url(url)
    s = _session()
    fields = _get_item(s, collection, item_id)["fields"]
    project = fields["System.TeamProject"]
    # Команда - из Area карточки, а если такой команды нет или карточки нет на ее досках - команда по умолчанию.
    teams = dict.fromkeys([area_team(fields.get("System.AreaPath", "")), None])
    board = next((b for t in teams if (b := _team_board(s, collection, project, t, fields))), None)
    if board is None:
        raise RuntimeError(f"Не найдена доска, на которой находится карточка #{item_id}")

    board_fields = board["fields"]
    column_field = board_fields["columnField"]["referenceName"]
    current = fields.get(column_field, "")
    if current.lower() == column.lower():
        return f"карточка #{item_id} уже в столбце {current}"
    if only_from and current.lower() != only_from.lower():
        return f"карточка #{item_id} в столбце {current}, а не {only_from}, - не переносится"
    target = next((c for c in board.get("columns", []) if c["name"].lower() == column.lower()), None)
    if target is None:
        raise RuntimeError(f"На доске {board.get('name')} нет столбца {column}")

    ops = [{"op": "add", "path": f"/fields/{column_field}", "value": target["name"]}]
    done_field = (board_fields.get("doneField") or {}).get("referenceName")
    if done_field and target.get("isSplit"):  # В разделенном столбце карточка попадает в Doing
        ops.append({"op": "add", "path": f"/fields/{done_field}", "value": False})
    state = (target.get("stateMappings") or {}).get(fields.get("System.WorkItemType"))
    if state == fields.get("System.State"):
        state = None
    if state:
        ops.append({"op": "add", "path": "/fields/System.State", "value": state})
    r = s.patch(f"{collection}/_apis/wit/workitems/{item_id}", params={"api-version": API_VERSION},
                data=json.dumps(ops), headers={"Content-Type": "application/json-patch+json"})
    if not r.ok:
        raise RuntimeError(f"Не удалось перенести карточку #{item_id} в столбец {target['name']}: "
                           f"HTTP {r.status_code} {r.text[:500]}")
    return (f"карточка #{item_id} перенесена из столбца {current} в {target['name']}"
            + (f", состояние {state}" if state else ""))


COMMENT_PREFIX = "[Bugdan]: "  # Начало каждого комментария, который бот пишет в карточку


def mention_html(url: str, person: dict) -> str:
    """HTML упоминания пользователя в комментарии карточки url: TFS отправит ему уведомление.
    Если id пользователя определить не удалось, - просто @имя (ошибка в RuntimeError не выбрасывается)."""
    name = escape("@" + (person.get("displayName") or person.get("uniqueName") or "?"))
    try:
        identity = _identity_id(_session(), parse_workitem_url(url)[0], person)
    except (RuntimeError, requests.RequestException):
        return name
    return f'<a href="#" data-vss-mention="version:2.0,{escape(identity)}">{name}</a>'


def add_workitem_comment(url: str, html: str) -> None:
    """Добавляет комментарий (HTML) в карточку по ссылке на нее. Комментарий начинается с COMMENT_PREFIX."""
    collection, _, item_id = parse_workitem_url(url)
    html = COMMENT_PREFIX + html
    # POST .../comments появился только в API 5.1; запись в System.History работает во всех версиях TFS
    # и попадает в Discussion карточки как обычный комментарий.
    r = _session().patch(f"{collection}/_apis/wit/workitems/{item_id}",
                         params={"api-version": API_VERSION},
                         data=json.dumps([{"op": "add", "path": "/fields/System.History", "value": html}]),
                         headers={"Content-Type": "application/json-patch+json"})
    if not r.ok:
        raise RuntimeError(f"Не удалось добавить комментарий в карточку #{item_id}: "
                           f"HTTP {r.status_code} {r.text[:500]}")


EDIT_COMMENTS_API_VERSION = "7.1-preview.4"  # Новый API комментариев: создание с номером и правка комментария


def create_workitem_comment(url: str, html: str) -> int:
    """Добавляет комментарий (HTML) в карточку и возвращает его номер для update_workitem_comment.
    Комментарий начинается с COMMENT_PREFIX."""
    collection, project, item_id = parse_workitem_url(url)
    r = _session().post(f"{collection}/{project}/_apis/wit/workItems/{item_id}/comments",
                        params={"api-version": EDIT_COMMENTS_API_VERSION}, json={"text": COMMENT_PREFIX + html})
    if not r.ok:
        raise RuntimeError(f"Не удалось добавить комментарий в карточку #{item_id}: "
                           f"HTTP {r.status_code} {r.text[:500]}")
    return r.json()["id"]


def update_workitem_comment(url: str, comment_id: int, html: str) -> None:
    """Заменяет текст комментария карточки, созданного create_workitem_comment. Текст начинается с COMMENT_PREFIX."""
    collection, project, item_id = parse_workitem_url(url)
    r = _session().patch(f"{collection}/{project}/_apis/wit/workItems/{item_id}/comments/{comment_id}",
                         params={"api-version": EDIT_COMMENTS_API_VERSION}, json={"text": COMMENT_PREFIX + html})
    if not r.ok:
        raise RuntimeError(f"Не удалось изменить комментарий {comment_id} карточки #{item_id}: "
                           f"HTTP {r.status_code} {r.text[:500]}")


def set_workitem_tag(url: str, tag: str, present: bool) -> bool:
    """Ставит (present=True) или снимает тег карточки по ссылке на нее; теги не различают регистр.
    Возвращает False, если тег уже был в нужном состоянии и карточка не менялась."""
    collection, _, item_id = parse_workitem_url(url)
    s = _session()
    tags = parse_tags(_get_item(s, collection, item_id)["fields"].get("System.Tags"))
    has = any(t.lower() == tag.lower() for t in tags)
    if has == present:
        return False
    tags = tags + [tag] if present else [t for t in tags if t.lower() != tag.lower()]
    r = s.patch(f"{collection}/_apis/wit/workitems/{item_id}", params={"api-version": API_VERSION},
                data=json.dumps([{"op": "add", "path": "/fields/System.Tags", "value": "; ".join(tags)}]),
                headers={"Content-Type": "application/json-patch+json"})
    if not r.ok:
        raise RuntimeError(f"Не удалось {'поставить' if present else 'снять'} тег {tag} карточки #{item_id}: "
                           f"HTTP {r.status_code} {r.text[:500]}")
    return True


@dataclass
class WorkItem:
    id: int
    title: str
    type: str
    text: str
    collection: str
    project: str
    relations: list[dict]
    comments: list[str]  # текст комментариев, от старых к новым
    area: str = ""  # System.AreaPath: SmartInstruments\Vega
    assigned_to: dict | None = None  # IdentityRef ответственного (System.AssignedTo); None - не назначен
    tags: list[str] = field(default_factory=list)  # System.Tags
    referenced: list[str] = field(default_factory=list)  # Ссылки на другие карточки из полей TEXT_FIELDS


# Ссылка на карточку: .../_workitems/edit/<номер> или ссылка на доску с ?workitem=<номер>
WORKITEM_LINK_RE = re.compile(r"https?://[^\s\"'<>]+?(?:/_workitems/edit/\d+|[?&]workitem=\d+)", re.IGNORECASE)


def _referenced_workitems(html_fields: list[str], item_id: int) -> list[str]:
    """Ссылки на другие карточки (кроме item_id) в HTML полей, по одной на номер карточки, в порядке появления."""
    found = {}
    for link in WORKITEM_LINK_RE.findall(unescape("\n".join(html_fields))):
        try:
            ref_id = parse_workitem_url(link)[2]
        except ValueError:
            continue
        if ref_id != item_id:
            found.setdefault(ref_id, link)
    return list(found.values())


def parse_tags(value: str | None) -> list[str]:
    """Теги из поля System.Tags ("тег1; тег2")."""
    return [t.strip() for t in (value or "").split(";") if t.strip()]


def _identity_ref(value) -> dict | None:
    """IdentityRef из поля карточки. В API 5.0 это объект, в старых версиях TFS - строка "Имя <DOMAIN\\login>"."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    m = re.fullmatch(r"\s*(.*?)\s*<([^<>]+)>\s*", value)
    name, unique = (m.group(1), m.group(2)) if m else (value.strip(), value.strip())
    return {"displayName": name, "uniqueName": unique}


def _get_item(s: requests.Session, collection: str, item_id: int) -> dict:
    r = s.get(f"{collection}/_apis/wit/workitems/{item_id}",
              params={"api-version": API_VERSION, "$expand": "relations"})
    r.raise_for_status()
    return r.json()


def _comment_records(s: requests.Session, collection: str, project: str, item_id: int) -> list[tuple[dict, str]]:
    """(автор - IdentityRef, HTML) комментариев карточки, от старых к новым."""
    r = s.get(f"{collection}/{project}/_apis/wit/workItems/{item_id}/comments",
              params={"api-version": COMMENTS_API_VERSION})
    r.raise_for_status()
    comments = sorted(r.json().get("comments", []), key=lambda c: c.get("id", 0))
    return [(_identity_ref(c.get("createdBy") or c.get("revisedBy")) or {}, c.get("text", "")) for c in comments]


def _get_comments(s: requests.Session, collection: str, project: str, item_id: int) -> list[tuple[str, str]]:
    """(автор, текст) комментариев карточки, от старых к новым."""
    return [(author.get("displayName") or "?", html_to_text(text))
            for author, text in _comment_records(s, collection, project, item_id)]


def load_workitem(url: str, with_comments: bool = True) -> WorkItem:
    """Загружает карточку: номер, заголовок, содержимое в виде текста, связи и комментарии."""
    collection, project, item_id = parse_workitem_url(url)
    s = _session()

    item = _get_item(s, collection, item_id)
    fields = item["fields"]
    project = fields.get("System.TeamProject", project)

    lines = [
        f"{fields.get('System.WorkItemType', 'Work item')} #{item_id}: {fields.get('System.Title', '')}",
        f"Состояние: {fields.get('System.State', '-')}, "
        f"серьезность: {fields.get('Microsoft.VSTS.Common.Severity', '-')}",
    ]
    for name, caption in TEXT_FIELDS.items():
        value = fields.get(name)
        if value:
            lines += ["", f"## {caption}", html_to_text(value)]

    comments = _get_comments(s, collection, project, item_id) if fields.get("System.CommentCount") else []
    if with_comments and comments:
        lines += ["", "## Комментарии"]
        lines += [f"- {author}: {text}" for author, text in comments]

    return WorkItem(id=item_id,
                    title=fields.get("System.Title", ""),
                    type=fields.get("System.WorkItemType", ""),
                    text="\n".join(lines),
                    collection=collection,
                    project=project,
                    relations=item.get("relations") or [],
                    comments=[text for _, text in comments],
                    area=fields.get("System.AreaPath", ""),
                    assigned_to=_identity_ref(fields.get("System.AssignedTo")),
                    tags=parse_tags(fields.get("System.Tags")),
                    referenced=_referenced_workitems([fields.get(name) or "" for name in TEXT_FIELDS], item_id))


def same_person(a: dict | None, b: dict | None) -> bool:
    """Один ли это пользователь: IdentityRef сравниваются по id, иначе по uniqueName, иначе по displayName."""
    if not a or not b:
        return False
    for key in ("id", "uniqueName", "displayName"):
        if a.get(key) and b.get(key):
            return a[key].lower() == b[key].lower()
    return False


PR_ARTIFACT_PREFIX = "vstfs:///git/pullrequestid/"  # Связь с pull request в секции Development
COMMIT_ARTIFACT_PREFIX = "vstfs:///git/commit/"  # Связь с коммитом в секции Development


def _linked_artifacts(relations: list[dict], prefix: str) -> list[str]:
    """Ссылки vstfs:///... связей карточки в секции Development, которые начинаются с prefix."""
    return [rel["url"] for rel in relations
            if rel.get("rel") == "ArtifactLink" and rel.get("url", "").lower().startswith(prefix)]


def linked_pull_requests(relations: list[dict]) -> list[str]:
    """Pull request, связанные с карточкой в секции Development: vstfs:///Git/PullRequestId/<проект>%2F<репозиторий>%2F<номер>."""
    return _linked_artifacts(relations, PR_ARTIFACT_PREFIX)


def linked_commits(relations: list[dict]) -> list[str]:
    """Коммиты, связанные с карточкой в секции Development: vstfs:///Git/Commit/<проект>%2F<репозиторий>%2F<sha>."""
    return _linked_artifacts(relations, COMMIT_ARTIFACT_PREFIX)


@dataclass
class PullRequest:
    url: str  # Ссылка на pull request в веб-интерфейсе
    status: str  # Состояние в TFS: active, abandoned или completed


def get_pull_request(link: str, collection: str) -> PullRequest:
    """Pull request по связи из секции Development (vstfs:///Git/PullRequestId/..., collection - коллекция карточки)
    или по ссылке вида .../<проект>/_git/<репозиторий>/pullrequest/<номер>."""
    if link.lower().startswith(PR_ARTIFACT_PREFIX):
        project, repo, pr_id = unquote(link[len(PR_ARTIFACT_PREFIX):]).split("/")
    else:
        m = re.match(r"(.+?/_git/[^/?#]+)/pullrequest/(\d+)", link, re.IGNORECASE)
        if m is None:
            raise ValueError(f"Не удалось разобрать ссылку на pull request: {link}")
        (collection, project, repo), pr_id = parse_git_url(m[1]), m[2]
    # Pull request может быть на другом сервере, а HttpNegotiateAuth запоминает хост - поэтому новая сессия.
    r = _session().get(f"{collection}/{project}/_apis/git/repositories/{repo}/pullrequests/{pr_id}",
                       params={"api-version": API_VERSION})
    if not r.ok:
        raise RuntimeError(f"Не удалось получить pull request {link}: HTTP {r.status_code} {r.text[:500]}")
    data = r.json()
    repository = data.get("repository") or {}
    project_name = (repository.get("project") or {}).get("name") or unquote(project)
    url = (f"{collection}/{quote(project_name)}/_git/{quote(repository.get('name') or unquote(repo))}"
           f"/pullrequest/{data.get('pullRequestId', pr_id)}")
    return PullRequest(url, str(data.get("status", "")).lower())


@dataclass
class TextEdit:
    author: dict  # IdentityRef того, кто изменил поле
    field: str  # Подпись поля из TEXT_FIELDS
    added: str  # Строки текста, которые добавило это изменение


@dataclass
class History:
    comments: list[tuple[dict, str]]  # (автор - IdentityRef, HTML комментария), от старых к новым
    text_edits: list[TextEdit]  # Дополнения текстовых полей карточки, от старых к новым
    last_assigned_to: dict | None  # Последний, кто был указан в Assigned To (сейчас поле может быть пустым)
    description_editor: dict | None = None  # Последний, кто изменял текстовые поля карточки (TEXT_FIELDS)


def _added_text(old_html: str, new_html: str) -> str:
    """Строки текста поля, которые появились при его изменении с old_html на new_html."""
    old, new = html_to_text(old_html or "").splitlines(), html_to_text(new_html or "").splitlines()
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    added = [line for tag, _, _, j1, j2 in matcher.get_opcodes() if tag in ("replace", "insert") for line in new[j1:j2]]
    return "\n".join(line for line in added if line.strip())


def _get_updates(s: requests.Session, collection: str, item_id: int) -> list[dict]:
    """Все изменения карточки (история), от старых к новым."""
    updates, page_size = [], 200
    while True:
        r = s.get(f"{collection}/_apis/wit/workitems/{item_id}/updates",
                  params={"api-version": API_VERSION, "$top": page_size, "$skip": len(updates)})
        r.raise_for_status()
        page = r.json().get("value", [])
        updates += page
        if len(page) < page_size:
            return updates


def load_history(item: WorkItem) -> History:
    """Комментарии карточки с авторами, дополнения ее текстовых полей (TEXT_FIELDS), последний ответственный
    и последний, кто правил текстовые поля, по истории изменений."""
    s = _session()
    edits, assigned, editor = [], None, None
    for update in _get_updates(s, item.collection, item.id):
        author = _identity_ref(update.get("revisedBy")) or {}
        fields = update.get("fields") or {}
        for name, caption in TEXT_FIELDS.items():
            change = fields.get(name)
            if isinstance(change, dict) and author and change.get("oldValue") != change.get("newValue"):
                editor = author
            if isinstance(change, dict) and (added := _added_text(change.get("oldValue"), change.get("newValue"))):
                edits.append(TextEdit(author, caption, added))
        # Когда ответственного убирают, newValue пустое, а oldValue - тот, кто был указан последним.
        if isinstance(change := fields.get("System.AssignedTo"), dict):
            assigned = _identity_ref(change.get("newValue")) or _identity_ref(change.get("oldValue")) or assigned
    return History(_comment_records(s, item.collection, item.project, item.id), edits, item.assigned_to or assigned,
                   editor)


@dataclass
class Attachment:
    name: str  # Имя файла во вложении карточки
    size: int  # Размер в байтах (из связи карточки или по факту скачивания)
    path: Path | None  # Куда сохранен; None - не скачан
    skipped: str = ""  # Причина, по которой файл не скачан


def _safe_name(name: str, used: set[str]) -> str:
    """Имя файла без путей и недопустимых в Windows символов; при совпадении добавляется (2), (3)..."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .") or "attachment"
    stem, dot, ext = name.rpartition(".") if "." in name else (name, "", "")
    candidate, n = name, 2
    while candidate.lower() in used:
        candidate = f"{stem} ({n}){dot}{ext}"
        n += 1
    used.add(candidate.lower())
    return candidate


def download_attachments(item: WorkItem, dest: Path, max_size: int) -> list[Attachment]:
    """Скачивает вложения карточки (связи AttachedFile) размером до max_size байт в папку dest.
    Вложения, которые больше или не скачались, возвращаются с причиной в skipped."""
    s, used, result = _session(), set(), []
    for rel in item.relations:
        if rel.get("rel") != "AttachedFile":
            continue
        attrs = rel.get("attributes") or {}
        name = attrs.get("name") or rel["url"].rstrip("/").rsplit("/", 1)[-1]
        size = int(attrs.get("resourceSize") or 0)
        if size > max_size:
            result.append(Attachment(name, size, None, f"больше {max_size / 1024 / 1024:g} МБ"))
            continue
        try:
            with s.get(rel["url"], params={"api-version": API_VERSION, "download": "true"}, stream=True) as r:
                r.raise_for_status()
                data = b""
                for chunk in r.iter_content(64 * 1024):
                    data += chunk
                    if len(data) > max_size:  # Размер в связи может отличаться от фактического
                        raise ValueError(f"больше {max_size / 1024 / 1024:g} МБ")
        except (requests.RequestException, ValueError) as e:
            result.append(Attachment(name, size, None, str(e)))
            continue
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / _safe_name(name, used)
        path.write_bytes(data)
        result.append(Attachment(name, len(data), path))
    return result


# Типы родительских карточек, в которых ищется ветка разработки.
PARENT_TYPES = {"User Story", "Feature"}

# Имя ветки в комментарии: "branch vega/43383", "Ветка: vega/43383", "в ветви master" и т.п.
COMMENT_BRANCH_RE = re.compile(r"(?<!\w)(?:branch|ветк|ветв)\w*(?:\s*[:=\-–—]\s*|\s+)[\"'`«]?([\w][\w./-]*)",
                               re.IGNORECASE)


def _linked_branches(relations: list[dict], repo_id: str) -> list[str]:
    """Ветки репозитория repo_id, связанные с карточкой в секции Development (новые первыми)."""
    branches = []
    for rel in relations:
        url = rel.get("url", "")
        if rel.get("rel") != "ArtifactLink" or not url.lower().startswith("vstfs:///git/ref/"):
            continue
        # vstfs:///Git/Ref/<id проекта>%2F<id репозитория>%2FGB<ветка>
        parts = unquote(url[len("vstfs:///Git/Ref/"):]).split("/", 2)
        if len(parts) == 3 and parts[1].lower() == repo_id.lower() and parts[2].startswith("GB"):
            branches.append(parts[2][2:])
    return branches[::-1]


def _comment_branches(comments: list[str]) -> list[str]:
    """Имена веток, упомянутые в комментариях после слов branch/ветка/ветвь (новые первыми)."""
    names = []
    for text in reversed(comments):
        for m in reversed(COMMENT_BRANCH_RE.findall(text)):
            names.append(m.removeprefix("refs/heads/").rstrip("./"))
    return names


def _find_parent(s: requests.Session, item: WorkItem) -> WorkItem | None:
    """Ближайший предок карточки с типом из PARENT_TYPES."""
    relations, seen = item.relations, {item.id}
    while True:
        parent = next((r for r in relations if r.get("rel") == "System.LinkTypes.Hierarchy-Reverse"), None)
        if parent is None:
            return None
        parent_id = int(parent["url"].rstrip("/").rsplit("/", 1)[-1])
        if parent_id in seen:
            return None
        seen.add(parent_id)
        data = _get_item(s, item.collection, parent_id)
        fields, relations = data["fields"], data.get("relations") or []
        if fields.get("System.WorkItemType") in PARENT_TYPES:
            project = fields.get("System.TeamProject", item.project)
            comments = _get_comments(s, item.collection, project, parent_id) if fields.get("System.CommentCount") else []
            return WorkItem(id=parent_id, title=fields.get("System.Title", ""),
                            type=fields["System.WorkItemType"], text="",
                            collection=item.collection, project=project,
                            relations=relations, comments=[text for _, text in comments])


def find_base_branch(item: WorkItem, repo_url: str, exists) -> tuple[str, str] | None:
    """Определяет ветку, в которой ведется работа по карточке. Возвращает (ветка, откуда она взята)
    или None, если ветка не найдена.

    Порядок: сама карточка (секция Development, затем комментарии), родительская User Story/Feature (так же).
    exists(имя) проверяет, что ветка есть в репозитории.
    """
    # Репозиторий и карточки могут быть на разных серверах, а HttpNegotiateAuth запоминает хост
    # первого запроса - поэтому для каждого сервера своя сессия.
    collection, project, repo = parse_git_url(repo_url)
    r = _session().get(f"{collection}/{project}/_apis/git/repositories/{repo}", params={"api-version": API_VERSION})
    r.raise_for_status()
    repo_id = r.json()["id"]

    parent = _find_parent(_session(), item)
    for card in [item] + ([parent] if parent else []):
        label = f"{card.type} #{card.id}"
        for branch in _linked_branches(card.relations, repo_id):
            if exists(branch):
                return branch, f"{label}, секция Development"
        for branch in _comment_branches(card.comments):
            if exists(branch):
                return branch, f"{label}, комментарий"
    return None
