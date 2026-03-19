"""Backward-compatible aliases for old module name.

This project originally automated pCloud. The implementation now targets Coda.io.
"""

from coda_automation import (  # noqa: F401
    CodaAutomationError as PcloudAutomationError,
    RunStats,
    ensure_input_files,
    get_pending_emails,
    get_sent_history,
    run_job,
)
