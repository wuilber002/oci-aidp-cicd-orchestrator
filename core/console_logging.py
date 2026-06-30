#!/usr/bin/env python3
"""Shared console rendering and log-file setup for the CI/CD scripts."""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import sys
import threading
import time
import traceback
from typing import Any, Callable, Dict, Optional, TypeVar

from core.settings import MAX_LOG_FILES_PER_COMMAND


LOGGER_NAME = "cicd-orchestrator"
log = logging.getLogger(LOGGER_NAME)
T = TypeVar("T")


def format_elapsed_br(elapsed_secs: float) -> str:
    total = max(int(elapsed_secs), 0)
    if total < 60:
        return "{}s".format(total)
    minutes, seconds = divmod(total, 60)
    return "{}min {:02d}s".format(minutes, seconds)


def format_remaining_br(remaining_secs: float) -> str:
    return format_elapsed_br(remaining_secs)


def run_logged_action(
    step_label: str,
    action: Callable[[], T],
    *,
    emit_initial: bool = True,
    logger: Optional[logging.Logger] = None,
) -> T:
    active_logger = logger or log
    if emit_initial:
        active_logger.info(step_label)
    started_at = time.time()
    elapsed = 0
    last_reported_elapsed = -1
    result: Dict[str, T] = {}
    failure: Dict[str, BaseException] = {}

    def runner() -> None:
        try:
            result["value"] = action()
        except BaseException as exc:  # pragma: no cover - re-raised in caller thread
            failure["error"] = exc

    thread = threading.Thread(target=runner, name="cicd-logged-action", daemon=True)
    thread.start()
    while thread.is_alive():
        if elapsed != last_reported_elapsed:
            active_logger.info("%s: %s elapsed", step_label, format_elapsed_br(elapsed))
            last_reported_elapsed = elapsed
        thread.join(timeout=1)
        elapsed = int(max(time.time() - started_at, 0))
    elapsed = int(max(time.time() - started_at, 0))
    if "error" in failure:
        raise failure["error"]
    active_logger.info("%s: completed in %s", step_label, format_elapsed_br(elapsed))
    return result.get("value")  # type: ignore[return-value]


def poll_with_progress(
    purpose: str,
    *,
    timeout_secs: float,
    fetch_interval_secs: float,
    fetch_fn: Callable[[], T],
    success_fn: Callable[[T], bool],
    progress_suffix_fn: Optional[Callable[[T], str]] = None,
    failure_message_fn: Optional[Callable[[T, int], Optional[str]]] = None,
    timeout_message_fn: Optional[Callable[[T, int], str]] = None,
    checkpoint_interval_secs: int = 0,
    checkpoint_message_fn: Optional[Callable[[T, int], Optional[str]]] = None,
    logger: Optional[logging.Logger] = None,
) -> T:
    active_logger = logger or log
    deadline = time.time() + timeout_secs
    started_at = time.time()
    last_logged_elapsed = -1
    last_checkpoint_elapsed = -1
    next_fetch_at = started_at

    def _run_fetch_with_progress(current_value: Optional[T]) -> T:
        result_box: Dict[str, Any] = {"done": False, "value": current_value, "error": None}

        def _target() -> None:
            try:
                result_box["value"] = fetch_fn()
            except Exception as exc:  # pragma: no cover - passthrough wrapper
                result_box["error"] = exc
            finally:
                result_box["done"] = True

        thread = threading.Thread(target=_target, name="cicd-poll-fetch", daemon=True)
        thread.start()
        nonlocal last_logged_elapsed
        while not result_box["done"]:
            elapsed = int(max(time.time() - started_at, 0))
            if elapsed != last_logged_elapsed:
                suffix = progress_suffix_fn(result_box["value"]).strip() if progress_suffix_fn and result_box["value"] is not None else ""
                if suffix:
                    active_logger.info("%s: %s elapsed, %s", purpose, format_elapsed_br(elapsed), suffix)
                else:
                    active_logger.info("%s: %s elapsed", purpose, format_elapsed_br(elapsed))
                last_logged_elapsed = elapsed
            thread.join(timeout=1)
        if result_box["error"] is not None:
            raise result_box["error"]
        return result_box["value"]

    last_value = _run_fetch_with_progress(None)
    while time.time() < deadline:
        now = time.time()
        if now >= next_fetch_at:
            last_value = _run_fetch_with_progress(last_value)
            next_fetch_at = now + max(fetch_interval_secs, 1)
        elapsed = int(max(time.time() - started_at, 0))
        if elapsed != last_logged_elapsed:
            suffix = progress_suffix_fn(last_value).strip() if progress_suffix_fn else ""
            if suffix:
                active_logger.info("%s: %s elapsed, %s", purpose, format_elapsed_br(elapsed), suffix)
            else:
                active_logger.info("%s: %s elapsed", purpose, format_elapsed_br(elapsed))
            last_logged_elapsed = elapsed
        if (
            checkpoint_interval_secs > 0
            and checkpoint_message_fn
            and elapsed > 0
            and elapsed % checkpoint_interval_secs == 0
            and elapsed != last_checkpoint_elapsed
        ):
            checkpoint_message = checkpoint_message_fn(last_value, elapsed)
            if checkpoint_message:
                active_logger.warning(checkpoint_message)
            last_checkpoint_elapsed = elapsed
        if success_fn(last_value):
            active_logger.info("%s: completed in %s", purpose, format_elapsed_br(elapsed))
            return last_value
        if failure_message_fn:
            failure_message = failure_message_fn(last_value, elapsed)
            if failure_message:
                raise RuntimeError(failure_message)
        time.sleep(1)
    elapsed = int(max(time.time() - started_at, 0))
    if timeout_message_fn:
        raise TimeoutError(timeout_message_fn(last_value, elapsed))
    raise TimeoutError("{}: timed out after {}".format(purpose, format_elapsed_br(elapsed)))


def log_phase_header(index: int, title: str, total: int) -> None:
    log.info(
        "== Phase %s: %s ==",
        index,
        title,
        extra={"tree_last": bool(total > 0 and index == (total - 1))},
    )


class ConsoleIndentFormatter(logging.Formatter):
    RESET = "\033[0m"
    STATUS_BLANK = "    "
    STATUS_PREFIX_WIDTH = 4
    COLORS = {
        "ERROR": "\033[97;41m",
        "WARNING": "\033[30;43m",
        "INFO_OK": "\033[97;42m",
        "INFO_OK_EXISTING": "\033[97;44m",
        "INFO": "\033[90m",
        "TITLE": "\033[1;36m",
        "ACCENT": "\033[1;34m",
        "ACTION": "\033[35m",
    }

    def __init__(self, fmt: str, use_color: bool = True):
        super().__init__(fmt)
        self._use_color = use_color
        self._display_root = os.path.realpath(os.getcwd())
        self._current_stage_kind = ""
        self._current_stage_seq = 0
        self._current_phase_seq = 0
        self._current_phase_last = False
        self._current_summary_list_seq = 0

    def _ok_prefix(self) -> str:
        if self._use_color:
            return "{} OK {}".format(self.COLORS["INFO_OK"], self.RESET)
        return " OK "

    def _ok_existing_prefix(self) -> str:
        if self._use_color:
            return "{} OK {}".format(self.COLORS["INFO_OK_EXISTING"], self.RESET)
        return " OK "

    def _info_prefix(self) -> str:
        if self._use_color:
            return "{}INFO{}".format(self.COLORS["INFO"], self.RESET)
        return "INFO"

    def _is_existing_result_message(self, lowered: str) -> bool:
        existing_markers = (
            "already exists",
            "already correct",
            "already absent",
            "updated",
            "identified by local content/metadata",
            "reports associated",
            "already associated",
            "existing git folder",
            "kept",
        )
        return any(marker in lowered for marker in existing_markers)

    def _is_success_result_message(self, lowered: str) -> bool:
        exact_markers = (
            "git credential validated",
            "workspace ready for use",
            "workspace created",
            "workspace already absent",
            "workspace removed",
            "virtualenv created",
            "virtualenv already exists",
            "virtualenv ready for use",
            "python dependencies already satisfied",
            "dependency consistency validated",
            "aidp sdk already installed",
            "aidp sdk installed",
            "official aidp sdk downloaded",
            "sdk wheel extracted",
            "local sdk cache updated",
            "temporary bootstrap workflow created",
            "file uploaded",
            "cluster ready for reference",
            "cluster resource created",
            "job resource created",
            "git folder credential already correct",
            "git folder ready for use",
            "ready for use",
            "path released",
            "target released",
            "content removed, root preserved",
            "copy completed",
            "commit and push completed",
            "bundle recreated successfully",
            "post-deploy reconciliation completed",
            "cleanup completed",
            "seed completed",
            "deploy bundle already exists",
            "deploy bundle identified by local content/metadata",
            "broken local folder removed before recreating the git folder",
            "bundle removed",
        )
        if any(marker in lowered for marker in exact_markers):
            return True

        regex_markers = (
            r": directory created\b",
            r": directory already exists\b",
            r": workspace removed\b",
            r": workspace already absent\b",
            r"\bremoved from bundle\b",
            r"\bremoving .* completed\b",
        )
        return any(re.search(pattern, lowered) for pattern in regex_markers)

    def _is_in_progress_message(self, lowered: str) -> bool:
        if any(marker in lowered for marker in (" elapsed", " remaining", "poll ", "attempt ")):
            return True
        prefixes = (
            "validating ",
            "installing ",
            "creating ",
            "downloading ",
            "extracting ",
            "ensuring ",
            "preparing ",
            "updating ",
            "triggering ",
            "removing ",
            "cleaning ",
            "copying ",
        )
        return lowered.startswith(prefixes)

    def _correlation_key(self, raw: str) -> Optional[tuple[str, str]]:
        text = raw.strip()
        normalized_progress = re.sub(
            r":\s+(?:\d+s|\d+min\s+\d+s)\s+(?:elapsed|remaining)$",
            "",
            text,
            flags=re.IGNORECASE,
        )
        normalized_progress = re.sub(
            r":\s+completed in\s+.+$",
            "",
            normalized_progress,
            flags=re.IGNORECASE,
        )
        if normalized_progress != text:
            return ("progress_label", normalized_progress.strip().lower())
        if text.lower().startswith(
            (
                "validating ",
                "installing ",
                "creating ",
                "downloading ",
                "extracting ",
                "ensuring ",
                "preparing ",
                "updating ",
                "triggering ",
                "removing ",
                "cleaning ",
                "copying ",
            )
        ):
            return ("progress_label", text.lower())
        fixed_patterns = (
            (r"^Validating workspace Git credential$", ("git_credential", "workspace")),
            (r"^Git credential validated:\s+(.+)$", ("git_credential", "workspace")),
        )
        for pattern, key in fixed_patterns:
            if re.match(pattern, text, flags=re.IGNORECASE):
                return key
        patterns = (
            (r"^Creating\s+.+\s+in workspace:\s+(.+)$", "workspace_file"),
            (r"^File uploaded:\s+(.+)$", "workspace_file"),
            (r"^(creating|updating)\s+cluster resource:\s+(.+)$", "cluster_resource"),
            (r"^Cluster resource (created|updated|ready):\s+(.+)$", "cluster_resource"),
            (r"^(creating|updating)\s+job resource:\s+(.+)$", "job_resource"),
            (r"^Job resource (created|updated|ready):\s+(.+)$", "job_resource"),
            (r"^Ensuring workspace\s+(.+)$", "workspace"),
            (r"^Creating workspace\s+(.+)$", "workspace"),
            (r"^Workspace (created|ready for use):\s+(.+)$", "workspace"),
            (r"^Ensuring base directory\s+(.+)$", "base_directory"),
            (r"^Base directory setup:\s+.+ at\s+(.+)$", "base_directory"),
        )
        for pattern, kind in patterns:
            match = re.match(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            value = match.group(match.lastindex or 0) if match.lastindex else ""
            return kind, str(value).strip().lower()
        return None

    def should_overlay_messages(self, pending: Dict[str, Any], current: Dict[str, Any]) -> bool:
        pending_group = pending.get("sibling_group")
        current_group = current.get("sibling_group")
        if not pending_group or pending_group != current_group:
            return False
        pending_raw = str(pending.get("raw") or "")
        current_raw = str(current.get("raw") or "")
        pending_key = self._correlation_key(pending_raw)
        current_key = self._correlation_key(current_raw)
        if not pending_key or not current_key:
            return False
        return pending_key == current_key

    def _prefix(self, record: logging.LogRecord, message: str) -> str:
        if record.levelno >= logging.ERROR:
            if self._use_color:
                return "{}ERROR{}".format(self.COLORS["ERROR"], self.RESET)
            return "ERROR"
        if record.levelno >= logging.WARNING:
            if self._use_color:
                return "{}WARN{}".format(self.COLORS["WARNING"], self.RESET)
            return "WARN"
        lowered = message.lower()
        if "promotion summary" in lowered or "prepare summary" in lowered or "bootstrap summary" in lowered:
            return self._ok_prefix()
        if "completed in" in lowered:
            return self._ok_prefix()
        if self._is_in_progress_message(lowered):
            return self.STATUS_BLANK
        if self._is_existing_result_message(lowered):
            return self._ok_existing_prefix()
        if self._is_success_result_message(lowered):
            return self._ok_prefix()
        return self._info_prefix()

    def format(self, record: logging.LogRecord) -> str:
        prepared = self.prepare_record(record)
        if not prepared:
            return ""
        return self.render_prepared(prepared, is_last=True)

    def _clean_header(self, message: str) -> str:
        text = message.strip()
        if text.startswith("=="):
            text = text[2:].strip()
        if text.endswith("=="):
            text = text[:-2].strip()
        return text

    def _phase_title(self, message: str) -> str:
        text = self._clean_header(message)
        if ":" in text:
            text = text.split(":", 1)[1].strip()
        return text.replace("_", " ").title()

    def prepare_record(self, record: logging.LogRecord) -> Optional[Dict[str, Any]]:
        raw_message = super().format(record)
        if not raw_message or raw_message.startswith("Using "):
            return None
        raw = raw_message.strip()
        meta: Dict[str, Any] = {
            "record": record,
            "raw": raw,
            "text": raw,
            "static_prefix": "",
            "branch_nonlast": "",
            "branch_last": "",
            "sibling_group": None,
            "buffered": False,
            "suppress_status": False,
        }
        if raw.startswith("Detailed log:"):
            self._current_stage_kind = ""
            self._current_phase_seq = 0
            self._current_phase_last = False
            self._current_summary_list_seq = 0
            meta["text"] = raw
            meta["static_prefix"] = ">> "
        elif raw.startswith("== Stage"):
            self._current_stage_seq += 1
            self._current_stage_kind = "etapa"
            self._current_phase_seq = 0
            self._current_phase_last = False
            self._current_summary_list_seq = 0
            meta["text"] = self._clean_header(raw)
            meta["static_prefix"] = "+-> "
        elif raw.startswith("== Phase"):
            self._current_phase_seq += 1
            self._current_phase_last = bool(getattr(record, "tree_last", False))
            meta["text"] = self._phase_title(raw)
            meta["static_prefix"] = "|   `-> " if self._current_phase_last else "|   |-> "
        elif raw.startswith("==") and "summary" in raw.lower():
            self._current_stage_kind = "summary"
            self._current_phase_seq = 0
            self._current_phase_last = False
            self._current_summary_list_seq = 0
            meta["text"] = self._clean_header(raw)
            meta["static_prefix"] = "`-> "
        elif raw in ("Reconciled jobs:", "Reconciled clusters:"):
            self._current_summary_list_seq += 1
            meta["buffered"] = True
            meta["suppress_status"] = True
            meta["sibling_group"] = ("summary_children", self._current_stage_seq or 1)
            meta["branch_nonlast"] = "    |-> "
            meta["branch_last"] = "    `-> "
        elif raw.startswith("- "):
            meta["text"] = raw[2:].strip()
            meta["buffered"] = True
            meta["suppress_status"] = True
            meta["sibling_group"] = ("summary_list_items", self._current_summary_list_seq)
            meta["branch_nonlast"] = "        |-> "
            meta["branch_last"] = "        `-> "
        elif raw.startswith("Promotion completed successfully") or raw.startswith("Prepare completed successfully"):
            meta["buffered"] = True
            meta["suppress_status"] = True
            meta["sibling_group"] = ("summary_children", self._current_stage_seq or 1)
            meta["branch_nonlast"] = "    |-> "
            meta["branch_last"] = "    `-> "
        elif self._current_stage_kind == "summary":
            meta["buffered"] = True
            meta["suppress_status"] = True
            meta["sibling_group"] = ("summary_children", self._current_stage_seq or 1)
            meta["branch_nonlast"] = "    |-> "
            meta["branch_last"] = "    `-> "
        elif self._current_phase_seq:
            meta["buffered"] = True
            meta["sibling_group"] = ("phase_children", self._current_stage_seq, self._current_phase_seq)
            if self._current_phase_last:
                meta["branch_nonlast"] = "|       |-> "
                meta["branch_last"] = "|       `-> "
            else:
                meta["branch_nonlast"] = "|   |   |-> "
                meta["branch_last"] = "|   |   `-> "
        elif self._current_stage_kind == "etapa":
            meta["buffered"] = True
            meta["sibling_group"] = ("stage_children", self._current_stage_seq)
            meta["branch_nonlast"] = "|   |-> "
            meta["branch_last"] = "|   `-> "
        else:
            meta["text"] = raw
            meta["static_prefix"] = ">> "
        return meta

    def render_message(self, prepared: Dict[str, Any], is_last: bool, force_nonlast: bool = False) -> str:
        message = prepared["text"]
        if self._use_color:
            if prepared.get("buffered"):
                branch = prepared["branch_nonlast"] if force_nonlast or not is_last else prepared["branch_last"]
                message = branch + message
            else:
                message = prepared["static_prefix"] + message
        message = self._suppress_paths_display(message)
        message = self._simplify_console_message(message)
        if self._use_color:
            message = self._highlight_keywords(message)
        return message

    def render_prepared(self, prepared: Dict[str, Any], is_last: bool, force_nonlast: bool = False) -> str:
        message = self.render_message(prepared, is_last=is_last, force_nonlast=force_nonlast)
        prefix = self.STATUS_BLANK if prepared.get("suppress_status") else self._prefix(prepared["record"], message)
        return "{} {}".format(prefix, message)

    def _highlight_keywords(self, message: str) -> str:
        replacements = (
            ("bundle", self.COLORS["ACCENT"]),
            ("deploy", self.COLORS["ACCENT"]),
            ("git", self.COLORS["ACCENT"]),
            ("delete", self.COLORS["ACTION"]),
            ("DELETE", self.COLORS["ACTION"]),
            ("cleanup", self.COLORS["ACTION"]),
        )
        highlighted = message
        for raw, color in replacements:
            pattern = r"(?<![\\w/.-])({})(?![\\w/.-])".format(re.escape(raw))
            highlighted = re.sub(pattern, "{}\\1{}".format(color, self.RESET), highlighted)
        return highlighted

    def _shorten_path_token(self, token: str) -> str:
        value = token.strip()
        try:
            candidate = os.path.realpath(value.rstrip("/"))
            common = os.path.commonpath([self._display_root, candidate])
            if common == self._display_root:
                relative = os.path.relpath(candidate, self._display_root)
                if relative == ".":
                    return "."
                return relative
        except (OSError, ValueError):
            pass
        if value.startswith("/Workspace/"):
            normalized = value.rstrip("/")
            parts = normalized.split("/")
            if len(parts) >= 2:
                return ".../{}".format("/".join(parts[-2:]))
        if value.startswith("/home/") or value.startswith("/run/") or value.startswith("/tmp/"):
            normalized = value.rstrip("/")
            base = os.path.basename(normalized)
            parent = os.path.basename(os.path.dirname(normalized))
            if parent:
                return ".../{}/{}".format(parent, base)
            return ".../{}".format(base)
        if value.startswith("cicd/") or value.startswith("Workspace/"):
            normalized = value.rstrip("/")
            parts = normalized.split("/")
            if len(parts) >= 2:
                return ".../{}".format("/".join(parts[-2:]))
        return token

    def _suppress_paths_display(self, message: str) -> str:
        if "Detailed log:" in message:
            return message

        def repl(match: re.Match[str]) -> str:
            return self._shorten_path_token(match.group(0))

        suppressed = message
        suppressed = re.sub(r"/Workspace/[^\s,)]+", repl, suppressed)
        suppressed = re.sub(r"/home/[^\s,)]+", repl, suppressed)
        suppressed = re.sub(r"/run/[^\s,)]+", repl, suppressed)
        suppressed = re.sub(r"/tmp/[^\s,)]+", repl, suppressed)
        suppressed = re.sub(r"(?<!/)\bcicd/[^\s,)]+", repl, suppressed)
        return suppressed

    def _simplify_console_message(self, message: str) -> str:
        simplified = message
        for old, new in (
            (" via legacy listing", ""),
            (" via legacy DELETE", ""),
            (" legacy DELETE", " DELETE"),
            ("fallback REST", "fallback"),
        ):
            simplified = simplified.replace(old, new)
        simplified = re.sub(r"Removing bundle root:\s+", "Removing bundle root ", simplified)
        simplified = re.sub(r"Removed from bundle:\s+", "Removed from bundle ", simplified)
        return simplified


class ConsoleProgressHandler(logging.StreamHandler):
    CLEAR_LINE = "\r\033[2K"
    FRAMES = "/-\\|"
    SPINNER_INTERVAL_SECS = 0.12
    STATUS_CELL = "[{}]  "

    def __init__(self, stream=None) -> None:
        super().__init__(stream)
        self._progress_active = False
        self._interactive = bool(getattr(self.stream, "isatty", lambda: False)())
        self._frame_index = 0
        self._progress_message = ""
        self._pending_prepared: Optional[Dict[str, Any]] = None
        self._started = False
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._spinner_thread = None

    def _console_formatter(self) -> Optional[ConsoleIndentFormatter]:
        formatter = self.formatter
        if isinstance(formatter, ConsoleIndentFormatter):
            return formatter
        return None

    def _flush_pending(self, current_prepared: Optional[Dict[str, Any]] = None) -> None:
        formatter = self._console_formatter()
        if not formatter or not self._pending_prepared:
            return
        pending = self._pending_prepared
        self._pending_prepared = None
        is_last = True
        if current_prepared and pending.get("sibling_group") and pending.get("sibling_group") == current_prepared.get("sibling_group"):
            is_last = False
        self.stream.write(formatter.render_prepared(pending, is_last=is_last) + self.terminator)
        super().flush()

    def _start_spinner(self) -> None:
        if not self._interactive or (self._spinner_thread and self._spinner_thread.is_alive()):
            return
        self._stop_event.clear()
        self._spinner_thread = threading.Thread(target=self._spinner_loop, name="aidp-console-spinner", daemon=True)
        self._spinner_thread.start()

    def _stop_spinner(self) -> None:
        thread = self._spinner_thread
        if not thread:
            return
        self._stop_event.set()
        thread.join(timeout=0.5)
        self._spinner_thread = None

    def _spinner_loop(self) -> None:
        while not self._stop_event.wait(self.SPINNER_INTERVAL_SECS):
            with self._lock:
                if not self._progress_active or not self._progress_message:
                    continue
                frame = self.FRAMES[self._frame_index % len(self.FRAMES)]
                self._frame_index += 1
                self.stream.write(self.CLEAR_LINE + "{}{}".format(self.STATUS_CELL.format(frame), self._progress_message))
                self.flush()

    def close(self) -> None:
        try:
            with self._lock:
                self._flush_pending()
            self._stop_spinner()
        finally:
            super().close()

    def flush(self) -> None:
        try:
            with self._lock:
                self._flush_pending()
                super().flush()
        except Exception:
            pass

    def _is_transient_progress(self, rendered_message: str) -> bool:
        lowered = rendered_message.strip().lower()
        if any(marker in lowered for marker in ("awaiting completion", "poll ", "attempt ", "elapsed", "remaining")):
            return True
        return lowered.startswith(
            (
                "validating ",
                "installing ",
                "creating ",
                "downloading ",
                "extracting ",
                "ensuring ",
                "preparing ",
                "updating ",
                "triggering ",
                "removing ",
                "cleaning ",
                "copying ",
            )
        )

    def _normalize_progress_message(self, message: str) -> str:
        normalized = message.rstrip()
        indent_length = len(normalized) - len(normalized.lstrip(" "))
        indent = normalized[:indent_length]
        content = re.sub(r":\s*awaiting completion\b", "", normalized[indent_length:], flags=re.IGNORECASE)
        return (indent + content).rstrip()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            formatter = self._console_formatter()
            if formatter:
                prepared = formatter.prepare_record(record)
                if not prepared:
                    return
                msg_body = formatter.render_message(prepared, is_last=False, force_nonlast=True)
                msg = formatter.render_prepared(prepared, is_last=False, force_nonlast=True)
            else:
                prepared = None
                msg_body = self.format(record)
                msg = self.format(record)
            if not msg:
                return
            is_progress = self._is_transient_progress(msg_body)
            should_stop_spinner = False
            with self._lock:
                if not self._started:
                    self.stream.write(self.terminator)
                    self._started = True
                if self._interactive and is_progress:
                    if prepared:
                        self._flush_pending(prepared)
                    self._progress_message = self._normalize_progress_message(msg_body)
                    self._progress_active = True
                    self._start_spinner()
                    frame = self.FRAMES[self._frame_index % len(self.FRAMES)]
                    self._frame_index += 1
                    self.stream.write(self.CLEAR_LINE + "{}{}".format(self.STATUS_CELL.format(frame), self._progress_message))
                    self.flush()
                    return
                if self._interactive and self._progress_active:
                    self._progress_active = False
                    self._progress_message = ""
                    should_stop_spinner = True
                    self.stream.write(self.CLEAR_LINE)
                if prepared and formatter and prepared.get("buffered"):
                    if self._pending_prepared and formatter.should_overlay_messages(self._pending_prepared, prepared):
                        self._pending_prepared = prepared
                        return
                    self._flush_pending(prepared)
                    self._pending_prepared = prepared
                else:
                    self._flush_pending(prepared)
                    self.stream.write(msg + self.terminator)
                    self.flush()
            if should_stop_spinner:
                self._stop_spinner()
        except RecursionError:
            raise
        except Exception:
            self.handleError(record)


def prune_old_logs(logs_dir: str, command_name: str, keep: int = MAX_LOG_FILES_PER_COMMAND) -> None:
    prefix = "{}-".format(command_name)
    candidates = []
    for name in os.listdir(logs_dir):
        if name.startswith(prefix) and name.endswith(".log"):
            path = os.path.join(logs_dir, name)
            if os.path.isfile(path):
                candidates.append(path)
    if len(candidates) <= keep:
        return
    candidates.sort(key=os.path.getmtime, reverse=True)
    for stale_path in candidates[keep:]:
        try:
            os.remove(stale_path)
        except OSError:
            log.debug("Could not remove old log file %s", stale_path)


def setup_logging(command_name: str) -> str:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(logging.DEBUG)
    logs_dir = os.path.abspath("logs")
    os.makedirs(logs_dir, exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = os.path.join(logs_dir, "{}-{}.log".format(command_name, timestamp))
    console = ConsoleProgressHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(ConsoleIndentFormatter("%(message)s", use_color=bool(getattr(console.stream, "isatty", lambda: False)())))
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(console)
    root.addHandler(file_handler)
    logging.getLogger("oci").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    prune_old_logs(logs_dir, command_name)
    log.debug("Detailed log: %s", log_path)
    return log_path


def run_with_logged_errors(main_fn, *args, **kwargs) -> int:
    try:
        return int(main_fn(*args, **kwargs))
    except SystemExit:
        raise
    except Exception as exc:
        log.debug("Unhandled exception during script execution\n%s", traceback.format_exc())
        log.error("Execution failed: %s", exc)
        return 1
