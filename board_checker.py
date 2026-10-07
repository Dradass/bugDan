"""Проверки бага при переносе его карточки по столбцам доски (endpoint /check-work-item сервера).
Срабатывают только для карточек с тегом AICheck; что проверять, зависит от столбца, в который перенесли карточку."""
import logging
from typing import Callable

from bug_fixer import TODO_COLUMN

CHECK_TAG = "AICheck"  # Тег карточки, при котором выполняются проверки
ANALYSIS_COLUMN, READY_FOR_TESTING_COLUMN, CLOSED_COLUMN = "Analysis", "Ready for testing", "Closed"

log = logging.getLogger("bugdan")


def check_description(bug_id: int, url: str) -> None:
    """Карточка перенесена в Analysis: проверка описания бага."""
    log.info("Карточка %s: Check bug description", bug_id)


def check_decision(bug_id: int, url: str) -> None:
    """Карточка перенесена в Ready for testing: проверка решения бага."""
    log.info("Карточка %s: Check bug decision", bug_id)


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
