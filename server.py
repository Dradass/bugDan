"""HTTP-сервер для service hook TFS: исправляет баг (bug_fixer.fix_bug), когда на его карточку ставят тег.

Подписка в TFS: Project Settings -> Service hooks -> Web Hooks, событие "Work item updated",
фильтры: тег AIFix, измененное поле Tags. Какой тег запускает обработку, задает только фильтр подписки.
    python server.py --repo-path D:\\Projects\\master\\RX
"""
import argparse, base64, hmac, json, logging, os, queue, threading, traceback
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

from bug_fixer import fix_bug

HERE = Path(__file__).resolve().parent
HOST, PORT = "0.0.0.0", 8080
HOOK_PATH = "/propose-fix"  # Путь, на который TFS отправляет события
WORK_ITEM_TYPES = {"bug"}  # Типы карточек, в нижнем регистре
LOG_DIR = HERE / "logs"  # Логи исправлений и общий лог сервера
MAX_BODY = 5 * 1024 * 1024

log = logging.getLogger("bugdan")


def parse_tags(value) -> set[str]:
    """Теги из поля System.Tags ("a; b; AIFix"). Теги в TFS не различают регистр."""
    return {t.strip().lower() for t in (value or "").split(";") if t.strip()}


def added_tags(fields: dict) -> set[str]:
    """Теги, появившиеся в этом изменении карточки (resource.fields события workitem.updated).
    Какой тег запускает обработку, задает фильтр подписки в TFS."""
    change = fields.get("System.Tags")
    if not isinstance(change, dict):
        return set()
    # Когда тегов не было или их все удалили, oldValue / newValue в событии отсутствуют.
    return parse_tags(change.get("newValue")) - parse_tags(change.get("oldValue"))


def bug_url(resource: dict) -> str:
    """Ссылка на карточку вида <коллекция>/<проект>/_workitems/edit/<id>, которую понимает bug_fixer."""
    # resource.url: <коллекция>/_apis/wit/workItems/<id>/updates/<n>
    collection = resource["url"].split("/_apis/", 1)[0]
    project = resource["revision"]["fields"]["System.TeamProject"]
    return f"{collection}/{quote(project)}/_workitems/edit/{resource['workItemId']}"


def select_bug(event: dict) -> tuple[int | None, str]:
    """Возвращает (номер карточки, ссылка), если событие должно запустить исправление, иначе (None, причина пропуска)."""
    if event.get("eventType") != "workitem.updated":
        return None, f"событие {event.get('eventType')!r} не обрабатывается"
    resource = event.get("resource") or {}
    fields = (resource.get("revision") or {}).get("fields") or {}
    item_type = fields.get("System.WorkItemType", "")
    if item_type.lower() not in WORK_ITEM_TYPES:
        return None, f"тип карточки {item_type!r} не обрабатывается"
    if not added_tags(resource.get("fields") or {}):
        return None, "теги не добавлялись"
    try:
        return int(resource["workItemId"]), bug_url(resource)
    except (KeyError, TypeError, ValueError) as e:
        return None, f"в событии нет данных карточки: {e!r}"


class Runner:
    """Очередь исправлений: по одному за раз, чтобы параллельные git fetch не мешали друг другу в одном репозитории."""

    def __init__(self, repo_path: str, log_dir: Path):
        self.repo_path, self.log_dir = repo_path, log_dir
        self.jobs: queue.Queue[tuple[int, str]] = queue.Queue()
        self.pending: set[int] = set()
        self.lock = threading.Lock()
        threading.Thread(target=self._work, daemon=True).start()

    def submit(self, bug_id: int, url: str) -> bool:
        """Ставит карточку в очередь; False, если она уже ждет или обрабатывается (повтор события)."""
        with self.lock:
            if bug_id in self.pending:
                return False
            self.pending.add(bug_id)
        self.jobs.put((bug_id, url))
        return True

    def _work(self):
        while True:
            bug_id, url = self.jobs.get()
            try:
                self._run(bug_id, url)
            except Exception:
                log.exception("Карточка %s: ошибка запуска", bug_id)
            finally:
                with self.lock:
                    self.pending.discard(bug_id)

    def _run(self, bug_id: int, url: str):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_file = self.log_dir / f"{bug_id}-{datetime.now():%Y%m%d-%H%M%S}.log"
        log.info("Карточка %s: запуск, лог %s", bug_id, log_file)
        # Вывод bug_fixer (print) пишется в лог запуска. Запуски идут по одному, поэтому подмена stdout не мешает другим.
        with log_file.open("w", encoding="utf-8") as f, redirect_stdout(f), redirect_stderr(f):
            print(f"bug-url: {url}\nrepo-path: {self.repo_path}\n")
            try:
                fix_bug(url, self.repo_path)
                error = None
            except SystemExit as e:  # bug_fixer сообщает об ошибке через sys.exit("текст")
                error = e.code
                print(error)
            except Exception:
                error = traceback.format_exc()
                print(error)
        if error:
            log.error("Карточка %s: ошибка, подробности в %s", bug_id, log_file)
        else:
            log.info("Карточка %s: готово", bug_id)


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
        bug_id, detail = select_bug(event)
        if bug_id is None:
            log.debug("Событие пропущено: %s", detail)
            return self.reply(200, {"status": "ignored", "reason": detail})
        if not self.server.runner.submit(bug_id, detail):
            log.info("Карточка %s уже в очереди, повтор события пропущен", bug_id)
            return self.reply(200, {"status": "duplicate", "bug": bug_id})
        log.info("Карточка %s поставлена в очередь: %s", bug_id, detail)
        # TFS ждет ответ недолго и повторяет запрос при ошибке, поэтому отвечаем сразу, а исправление идет в фоне.
        self.reply(202, {"status": "queued", "bug": bug_id})

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
    p.add_argument("--repo-path", required=True, help="Путь до репозитория решения")
    p.add_argument("--verbose", action="store_true", help="Писать в лог пропущенные события и HTTP-запросы")
    cfg = p.parse_args()
    cfg.credentials = os.environ.get("BUGDAN_HOOK_USER", ""), os.environ.get("BUGDAN_HOOK_PASSWORD", "")

    # Консоль службы Windows пишет в кодировке ANSI, поэтому основной лог дублируется в файл в UTF-8.
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.DEBUG if cfg.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(LOG_DIR / "server.log", encoding="utf-8")])
    if not cfg.credentials[1]:
        log.warning("BUGDAN_HOOK_PASSWORD не задан: запросы принимаются без проверки Basic-аутентификации")

    server = HookServer((HOST, PORT), cfg, Runner(cfg.repo_path, LOG_DIR))
    log.info("Жду события на http://%s:%s%s, репозиторий %s", HOST, PORT, HOOK_PATH, cfg.repo_path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
