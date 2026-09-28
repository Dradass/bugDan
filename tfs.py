"""Загрузка карточки (work item) из TFS / Azure DevOps Server в виде текста для промпта."""
import os
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse, parse_qs

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
                        title: str, description: str, draft: bool = True) -> str:
    """Создает pull request и возвращает ссылку на него."""
    collection, project, repo = parse_git_url(repo_url)
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


@dataclass
class WorkItem:
    id: int
    title: str
    type: str
    text: str


def load_workitem(url: str, with_comments: bool = True) -> WorkItem:
    """Загружает карточку: номер, заголовок и содержимое в виде текста."""
    collection, project, item_id = parse_workitem_url(url)
    s = _session()

    r = s.get(f"{collection}/_apis/wit/workitems/{item_id}",
              params={"api-version": API_VERSION})
    r.raise_for_status()
    fields = r.json()["fields"]

    lines = [
        f"{fields.get('System.WorkItemType', 'Work item')} #{item_id}: {fields.get('System.Title', '')}",
        f"Состояние: {fields.get('System.State', '-')}, "
        f"серьезность: {fields.get('Microsoft.VSTS.Common.Severity', '-')}",
    ]
    for name, caption in TEXT_FIELDS.items():
        value = fields.get(name)
        if value:
            lines += ["", f"## {caption}", html_to_text(value)]

    if with_comments and fields.get("System.CommentCount"):
        r = s.get(f"{collection}/{project}/_apis/wit/workItems/{item_id}/comments",
                  params={"api-version": COMMENTS_API_VERSION})
        r.raise_for_status()
        comments = r.json().get("comments", [])
        if comments:
            lines += ["", "## Комментарии"]
            for c in comments:
                author = (c.get("createdBy") or c.get("revisedBy") or {}).get("displayName") or "?"
                lines.append(f"- {author}: {html_to_text(c.get('text', ''))}")

    return WorkItem(id=item_id,
                    title=fields.get("System.Title", ""),
                    type=fields.get("System.WorkItemType", ""),
                    text="\n".join(lines))
