"""Загрузка карточки (work item) из TFS / Azure DevOps Server в виде текста для промпта."""
import json
import os
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse, parse_qs, unquote

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
                        title: str, description: str, template: str = "", draft: bool = True) -> str:
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


def add_workitem_comment(url: str, html: str) -> None:
    """Добавляет комментарий (HTML) в карточку по ссылке на нее."""
    collection, _, item_id = parse_workitem_url(url)
    # POST .../comments появился только в API 5.1; запись в System.History работает во всех версиях TFS
    # и попадает в Discussion карточки как обычный комментарий.
    r = _session().patch(f"{collection}/_apis/wit/workitems/{item_id}",
                         params={"api-version": API_VERSION},
                         data=json.dumps([{"op": "add", "path": "/fields/System.History", "value": html}]),
                         headers={"Content-Type": "application/json-patch+json"})
    if not r.ok:
        raise RuntimeError(f"Не удалось добавить комментарий в карточку #{item_id}: "
                           f"HTTP {r.status_code} {r.text[:500]}")


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


def _get_item(s: requests.Session, collection: str, item_id: int) -> dict:
    r = s.get(f"{collection}/_apis/wit/workitems/{item_id}",
              params={"api-version": API_VERSION, "$expand": "relations"})
    r.raise_for_status()
    return r.json()


def _get_comments(s: requests.Session, collection: str, project: str, item_id: int) -> list[tuple[str, str]]:
    """(автор, текст) комментариев карточки, от старых к новым."""
    r = s.get(f"{collection}/{project}/_apis/wit/workItems/{item_id}/comments",
              params={"api-version": COMMENTS_API_VERSION})
    r.raise_for_status()
    comments = sorted(r.json().get("comments", []), key=lambda c: c.get("id", 0))
    return [((c.get("createdBy") or c.get("revisedBy") or {}).get("displayName") or "?",
             html_to_text(c.get("text", ""))) for c in comments]


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
                    area=fields.get("System.AreaPath", ""))


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


def find_base_branch(item: WorkItem, repo_url: str, exists) -> tuple[str, str]:
    """Определяет ветку, в которой ведется работа по карточке. Возвращает (ветка, откуда она взята).

    Порядок: родительская User Story/Feature (секция Development, затем комментарии),
    сама карточка (так же), иначе master. exists(имя) проверяет, что ветка есть в репозитории.
    """
    # Репозиторий и карточки могут быть на разных серверах, а HttpNegotiateAuth запоминает хост
    # первого запроса - поэтому для каждого сервера своя сессия.
    collection, project, repo = parse_git_url(repo_url)
    r = _session().get(f"{collection}/{project}/_apis/git/repositories/{repo}", params={"api-version": API_VERSION})
    r.raise_for_status()
    repo_id = r.json()["id"]

    parent = _find_parent(_session(), item)
    for card in ([parent] if parent else []) + [item]:
        label = f"{card.type} #{card.id}"
        for branch in _linked_branches(card.relations, repo_id):
            if exists(branch):
                return branch, f"{label}, секция Development"
        for branch in _comment_branches(card.comments):
            if exists(branch):
                return branch, f"{label}, комментарий"
    return "master", "по умолчанию"
