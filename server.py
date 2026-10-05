"""HTTP-сервер для service hook TFS: исправляет баг (bug_fixer.fix_bug), когда на его карточке оказываются
тег AIFix и тег репозитория. Теги репозиториев, пути до них и необязательные файлы правил Claude задаются
в config.json: {"tags": {"<тег>": {"repo": "<путь>", "rule": "<файл правила>"}}}.

Подписка в TFS: Project Settings -> Service hooks -> Web Hooks, событие "Work item updated",
фильтры: тег AIFix, измененное поле Tags. Тег репозитория проверяет сервер.
    python server.py [--config config.json]
"""
import argparse, base64, hmac, json, logging, os, queue, re, threading, traceback
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple
from urllib.parse import parse_qs, quote, unquote, urlsplit

from bug_fixer import fix_bug

HERE = Path(__file__).resolve().parent
HOST, PORT = "0.0.0.0", 8080
HOOK_PATH = "/propose-fix"  # Путь, на который TFS отправляет события
WORK_ITEM_TYPES = {"bug"}  # Типы карточек
TRIGGER_TAG = "AIFix"  # Тег, по которому срабатывает подписка TFS
DEFAULT_CONFIG = HERE / "config.json"  # Конфиг с тегами репозиториев (--config)
LOG_DIR = HERE / "logs"
FIX_LOG_DIR = LOG_DIR / "fix_logs"  # Логи исправлений, по файлу на запуск
SERVER_LOG_DIR = LOG_DIR / "server"  # Общий лог сервера, по файлу на день
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"  # Дата и время в начале каждой строки логов
MAX_BODY = 5 * 1024 * 1024
GUID_RE = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)
DUPLICATE_PARAM ="allow-duplicate"  # ?allow-duplicate=1 в URL: создать новую ветку, даже если ветка бага уже есть

log = logging.getLogger("bugdan")


def parse_tags(value) -> set[str]:
    """Теги из поля System.Tags ("a; b; AIFix"). Теги в TFS не различают регистр."""
    return {t.strip().lower() for t in (value or "").split(";") if t.strip()}


def tags_change(fields: dict) -> tuple[set[str], set[str]] | None:
    """Теги до и после этого изменения карточки (resource.fields события workitem.updated);
    None, если теги не менялись."""
    change = fields.get("System.Tags")
    if not isinstance(change, dict):
        return None
    # Когда тегов не было или их все удалили, oldValue / newValue в событии отсутствуют.
    return parse_tags(change.get("oldValue")), parse_tags(change.get("newValue"))


def triggered_tags(fields: dict, repo_tags: set[str]) -> tuple[list[str], str | None]:
    """Теги репозиториев, по которым нужно запустить исправление, и причина пропуска, если таких нет.
    Запуск по тегу репозитория - когда в этом изменении на карточке впервые оказались он и AIFix
    (неважно, какой из них поставлен последним)."""
    change = tags_change(fields)
    if change is None:
        return [], "теги не менялись"
    old, new = change
    trigger = TRIGGER_TAG.lower()
    if trigger not in new:
        return [], f"нет тега {trigger}"
    present = sorted(repo_tags & new)
    if not present:
        return [], "нет ни одного из тегов " + ", ".join(sorted(repo_tags))
    tags = [t for t in present if not {trigger, t} <= old]
    if not tags:
        return [], f"теги {trigger}, " + ", ".join(present) + " стояли и до изменения"
    return tags, None


def bug_url(resource: dict) -> str:
    """Ссылка на карточку вида <коллекция>/<проект>/_workitems/edit/<id>, которую понимает bug_fixer."""
    # resource.url: <коллекция>/_apis/wit/workItems/<id>/updates/<n>, но TFS может добавить
    # к коллекции id или имя проекта: <коллекция>/<проект>/_apis/...
    collection = resource["url"].split("/_apis/", 1)[0]
    project = resource["revision"]["fields"]["System.TeamProject"]
    head, _, last = collection.rpartition("/")
    if GUID_RE.fullmatch(last) or unquote(last).lower() == project.lower():
        collection = head
    return f"{collection}/{quote(project)}/_workitems/edit/{resource['workItemId']}"


def select_bug(event: dict, repo_tags: set[str]) -> tuple[int | None, str, list[str]]:
    """Возвращает (номер карточки, ссылка, теги репозиториев), если событие должно запустить исправление,
    иначе (None, причина пропуска, [])."""
    if event.get("eventType") != "workitem.updated":
        return None, f"событие {event.get('eventType')!r} не обрабатывается", []
    resource = event.get("resource") or {}
    fields = (resource.get("revision") or {}).get("fields") or {}
    item_type = fields.get("System.WorkItemType", "")
    if item_type.lower() not in WORK_ITEM_TYPES:
        return None, f"тип карточки {item_type!r} не обрабатывается", []
    tags, reason = triggered_tags(resource.get("fields") or {}, repo_tags)
    if reason:
        return None, reason, []
    try:
        return int(resource["workItemId"]), bug_url(resource), tags
    except (KeyError, TypeError, ValueError) as e:
        return None, f"в событии нет данных карточки: {e!r}", []


def read_rule(path: Path) -> str:
    """Текст файла с правилом написания кода для Claude."""
    try:
        return path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeDecodeError) as e:
        raise SystemExit(f"Не удалось прочитать файл правила {path}: {e}")


class TimestampFormatter(logging.Formatter):
    """Ставит дату и время в начало каждой строки записи, в том числе строк traceback."""

    def __init__(self):
        super().__init__("%(levelname)s %(message)s")

    def format(self, record):
        stamp = datetime.fromtimestamp(record.created).strftime(TIME_FORMAT)
        return "\n".join(f"{stamp} {line}" for line in super().format(record).splitlines())


class DailyFileHandler(logging.FileHandler):
    """Пишет лог в <папка>/<ГГГГ-ММ-ДД>.log: каждый день в отдельный файл."""

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory, self.day = directory, date.today()
        super().__init__(self._path(self.day), encoding="utf-8")

    def _path(self, day: date) -> Path:
        return self.directory / f"{day:%Y-%m-%d}.log"

    def emit(self, record):
        # emit вызывается под блокировкой обработчика, поэтому смена файла не пересекается с записью из других потоков.
        day = datetime.fromtimestamp(record.created).date()
        if day != self.day:
            if self.stream:
                self.stream.close()
                self.stream = None  # FileHandler откроет новый файл при записи
            self.day, self.baseFilename = day, str(self._path(day))
        super().emit(record)


class TimestampWriter:
    """Файл для redirect_stdout: в начало каждой строки ставит дату и время, когда она начала выводиться."""

    def __init__(self, f):
        self.f, self.line_start = f, True

    def write(self, text: str) -> int:
        for line in text.splitlines(keepends=True):
            if self.line_start:
                self.f.write(f"{datetime.now():{TIME_FORMAT}} ")
            self.f.write(line)
            self.line_start = line.endswith("\n")
        self.f.flush()  # Лог запуска можно смотреть, пока он идет
        return len(text)

    def flush(self):
        self.f.flush()


class Repo(NamedTuple):
    path: Path  # Путь до репозитория решения
    rule: Path | None  # Файл с правилом написания кода для Claude; None - без правила


def read_repos(path: Path) -> dict[str, Repo]:
    """Репозитории по тегам (в нижнем регистре) из секции "tags" конфига:
    {"<тег>": {"repo": "<путь>", "rule": "<файл правила, необязательно>"}}.
    Относительный путь до файла правила считается от папки конфига."""
    try:
        tags = json.loads(path.read_text(encoding="utf-8-sig"))["tags"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SystemExit(f"Не удалось прочитать теги репозиториев из {path}: {e!r}")
    if not isinstance(tags, dict) or not tags:
        raise SystemExit(f'В {path} секция "tags" должна быть непустым словарем {{"<тег>": {{"repo": "<путь>"}}}}')
    repos = {}
    for tag, item in tags.items():
        key = tag.strip().lower()
        if not key or ";" in key or key == TRIGGER_TAG.lower() or key in repos:
            raise SystemExit(f"В {path} недопустимый или повторяющийся тег {tag!r}")
        if not isinstance(item, dict):
            raise SystemExit(f'В {path} для тега {tag!r} нужен словарь {{"repo": "<путь>", "rule": "<файл правила>"}}')
        repo = item.get("repo")
        if not isinstance(repo, str) or not Path(repo).is_dir():
            raise SystemExit(f'В {path} для тега {tag!r} в "repo" указан несуществующий путь {repo!r}')
        rule = item.get("rule") or None
        if rule is not None:
            if not isinstance(rule, str):
                raise SystemExit(f'В {path} для тега {tag!r} в "rule" должен быть путь до файла')
            rule = (path.parent / rule).resolve()
            read_rule(rule)  # Ошибка в пути видна сразу при запуске, а не при первом баге
        repos[key] = Repo(Path(repo), rule)
    return repos


class Runner:
    """Очередь исправлений: по одному за раз, чтобы параллельные git fetch не мешали друг другу в одном репозитории."""

    def __init__(self, repos: dict[str, Repo], log_dir: Path):
        self.repos, self.log_dir = repos, log_dir
        self.jobs: queue.Queue[tuple[int, str, str, bool]] = queue.Queue()
        self.pending: set[tuple[int, str]] = set()
        self.lock = threading.Lock()
        threading.Thread(target=self._work, daemon=True).start()

    def submit(self, bug_id: int, url: str, tag: str, allow_duplicate: bool = False) -> bool:
        """Ставит карточку в очередь на исправление в репозитории тега; False, если она уже ждет
        или обрабатывается в этом репозитории (повтор события)."""
        with self.lock:
            if (bug_id, tag) in self.pending:
                return False
            self.pending.add((bug_id, tag))
        self.jobs.put((bug_id, url, tag, allow_duplicate))
        return True

    def _work(self):
        while True:
            bug_id, url, tag, allow_duplicate = self.jobs.get()
            try:
                self._run(bug_id, url, tag, allow_duplicate)
            except Exception:
                log.exception("Карточка %s (%s): ошибка запуска", bug_id, tag)
            finally:
                with self.lock:
                    self.pending.discard((bug_id, tag))

    def _run(self, bug_id: int, url: str, tag: str, allow_duplicate: bool):
        repo = self.repos[tag]
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_file = self.log_dir / f"{bug_id}-{tag}-{datetime.now():%Y%m%d-%H%M%S}.log"
        log.info("Карточка %s (%s): запуск в %s, лог %s", bug_id, tag, repo.path, log_file)
        # Вывод bug_fixer (print) пишется в лог запуска. Запуски идут по одному, поэтому подмена stdout не мешает другим.
        with log_file.open("w", encoding="utf-8") as f, redirect_stdout(out := TimestampWriter(f)), redirect_stderr(out):
            print(f"bug-url: {url}\ntag: {tag}\nrepo-path: {repo.path}\nallow-duplicate: {allow_duplicate}\n"
                  f"claude-rule: {repo.rule or '-'}\n")
            try:
                # Файл правила читается при каждом запуске, чтобы его правки применялись без перезапуска сервера.
                rules = read_rule(repo.rule) if repo.rule else None
                fix_bug(url, str(repo.path), allow_duplicate, rules)
                error = None
            except SystemExit as e:  # bug_fixer сообщает об ошибке через sys.exit("текст")
                error = e.code
                print(error)
            except Exception:
                error = traceback.format_exc()
                print(error)
        if error:
            log.error("Карточка %s (%s): ошибка, подробности в %s", bug_id, tag, log_file)
        else:
            log.info("Карточка %s (%s): готово", bug_id, tag)


class HookServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, cfg, runner: Runner):
        super().__init__(address, HookHandler)
        self.cfg, self.runner = cfg, runner


class HookHandler(BaseHTTPRequestHandler):
    server: HookServer

    def do_GET(self):
        if handler := self.route({"/health": self.health}):
            handler()

    def do_POST(self):
        """Общие проверки POST-запросов; обработчик endpoint получает разобранное JSON-тело."""
        handler = self.route({HOOK_PATH: self.propose_fix})
        if handler is None:
            return
        if not self.authorized():
            return self.reply(401, {"error": "unauthorized"}, {"WWW-Authenticate": 'Basic realm="bugDan"'})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self.reply(413, {"error": "payload too large"})
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            return self.reply(400, {"error": "invalid json"})
        handler(body)

    def route(self, endpoints: dict):
        """Обработчик endpoint по пути запроса (без query string); для неизвестного пути отвечает 404 и возвращает None."""
        handler = endpoints.get(urlsplit(self.path).path)
        if handler is None:
            self.reply(404, {"error": "not found"})
        return handler

    def health(self):
        self.reply(200, {"status": "ok", "queued": self.server.runner.jobs.qsize()})

    def propose_fix(self, event: dict):
        """Событие service hook TFS: ставит карточку в очередь на исправление."""
        runner = self.server.runner
        bug_id, detail, tags = select_bug(event, set(runner.repos))
        if bug_id is None:
            log.debug("Событие пропущено: %s", detail)
            return self.reply(200, {"status": "ignored", "reason": detail})
        allow_duplicate = self.query_flag(DUPLICATE_PARAM)
        queued = []
        for tag in tags:
            if not runner.submit(bug_id, detail, tag, allow_duplicate):
                log.info("Карточка %s (%s) уже в очереди, повтор события пропущен", bug_id, tag)
                continue
            queued.append(tag)
            log.info("Карточка %s (%s) поставлена в очередь%s: %s", bug_id, tag,
                     " (разрешен дубликат ветки)" if allow_duplicate else "", detail)
        if not queued:
            return self.reply(200, {"status": "duplicate", "bug": bug_id, "tags": tags})
        # TFS ждет ответ недолго и повторяет запрос при ошибке, поэтому отвечаем сразу, а исправление идет в фоне.
        self.reply(202, {"status": "queued", "bug": bug_id, "tags": queued, DUPLICATE_PARAM: allow_duplicate})

    def query_flag(self, name: str) -> bool:
        """Логический параметр query string: ?name, ?name=1, ?name=true или ?name=yes."""
        values = parse_qs(urlsplit(self.path).query, keep_blank_values=True).get(name)
        return bool(values) and values[-1].strip().lower() in {"", "1", "true", "yes"}

    def authorized(self) -> bool:
        user, password = self.server.cfg.credentials
        if not password:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            given = base64.b64decode(header[6:], validate=True)
        except ValueError:
            return False
        return hmac.compare_digest(given, f"{user}:{password}".encode("utf-8"))

    def reply(self, code: int, body: dict, headers: dict | None = None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                   help=f"Путь до конфига с тегами репозиториев (по умолчанию {DEFAULT_CONFIG.name} рядом с server.py)")
    p.add_argument("--verbose", action="store_true", help="Писать в лог пропущенные события и HTTP-запросы")
    cfg = p.parse_args()
    repos = read_repos(cfg.config.resolve())
    cfg.credentials = os.environ.get("BUGDAN_HOOK_USER", ""), os.environ.get("BUGDAN_HOOK_PASSWORD", "")

    # Консоль службы Windows пишет в кодировке ANSI, поэтому основной лог дублируется в файл в UTF-8.
    handlers = [logging.StreamHandler(), DailyFileHandler(SERVER_LOG_DIR)]
    for handler in handlers:
        handler.setFormatter(TimestampFormatter())
    logging.basicConfig(level=logging.DEBUG if cfg.verbose else logging.INFO, handlers=handlers)
    if not cfg.credentials[1]:
        log.warning("BUGDAN_HOOK_PASSWORD не задан: запросы принимаются без проверки Basic-аутентификации")

    server = HookServer((HOST, PORT), cfg, Runner(repos, FIX_LOG_DIR))
    log.info("Жду события на http://%s:%s%s, тег %s и теги репозиториев: %s", HOST, PORT, HOOK_PATH, TRIGGER_TAG,
             "; ".join(f"{t} -> {r.path} (правило Claude {r.rule or 'не задано'})" for t, r in repos.items()))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
