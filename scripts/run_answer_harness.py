"""Prepare and run a bounded, private Harness2-inspired selection pilot.

Use ``prepare CASES OUTPUT``, then ``baseline OUTPUT``, then ``pilot OUTPUT``.
Only the existing guarded Kaggle provider is used. No application writes,
new model service, executable model edits, or automatic promotion are allowed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apps.api import answer_support
from apps.api import chat_selection as selection_protocol
from apps.api.chat_facts import render_postings
from apps.api.chat_selection import EvidenceItem
from apps.api.routers import chat
from packages.core import db
from packages.core.config import get_settings, settings_env_file
from packages.core.schemas import ChatReply
from packages.llm import answer_harness as pilot_method
from packages.llm import kaggle_connection
from packages.llm.answer_harness import (
    BudgetedCaller,
    FrozenSelectionCase,
    PilotSplit,
    SelectionRun,
    capture_selection,
    digest,
    make_split,
    run_pilot,
    selection_prompt,
    validate_baseline,
)
from packages.llm.kaggle import MODEL
from packages.llm.provider import LLMProvider
from packages.matching.retrieve import Retrieval

UPSTREAM = "1cfe254e8d25b15f37048c3730134686abe60335"
REPEATS = 3
PHASE_SECONDS = 3600


def runtime_identity() -> dict[str, str]:
    """Reject reuse after execution, validation, rendering or review code changes."""
    files = {
        "cli": __file__,
        "method": pilot_method.__file__,
        "selection_protocol": selection_protocol.__file__,
        "chat_renderer": chat.__file__,
        "answer_review": answer_support.__file__,
    }
    return {name: digest(Path(path).read_text()) for name, path in files.items()}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2)


def private_path(path: Path) -> Path:
    """Keep all experiment material in the selected installation's storage."""
    path = path.expanduser().absolute()
    root = Path(get_settings().storage_root).expanduser().resolve()
    if not path.resolve().is_relative_to(root) or path.is_symlink():
        raise ValueError("Experiment files must stay inside private storage_root")
    return path


def read_private(path: Path) -> Any:
    path = private_path(path)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW)) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Experiment input must be an owner-private regular file")
        return json.load(stream)


def write_private(path: Path, value: Any) -> None:
    path = private_path(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".answer-pilot-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(_json(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def selection_cases(cases: list[dict[str, Any]]) -> list[FrozenSelectionCase]:
    selected = []
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)) or any(not isinstance(i, str) or not i for i in ids):
        raise ValueError("Frozen case IDs must be nonempty and unique")
    for case in cases:
        if (
            case.get("include_mail", False) is not False
            or case.get("mail_in_context", False) is not False
        ):
            raise ValueError("Mail-bearing snapshots are excluded from this pilot")
        if case["mode"] not in {"facts", "selection", "prose"}:
            raise ValueError("Unknown frozen case mode")
        if case["mode"] == "facts":
            # Protected-question refusals return before context construction.
            # They have no prompt or retrieval state and must never call a model.
            ChatReply.model_validate(case["code_reply"])
            continue
        if case["system"] != chat.SYSTEM:
            raise ValueError("Frozen system differs from this checkout")
        if case["mode"] == "selection":
            selected.append(
                FrozenSelectionCase(
                    id=case["id"],
                    question=case["question"],
                    system=case["system"],
                    prompt=case["prompt"],
                    evidence=tuple(EvidenceItem.model_validate(v) for v in case["evidence"]),
                )
            )
        else:
            if not isinstance(case["prompt"], str):
                raise ValueError("Frozen prompt must be text")
        TypeAdapter(Retrieval).validate_python(case["found"])
    return selected


def prepare(source: Path, output: Path) -> dict[str, Any]:
    cases = read_private(source)
    selected = selection_cases(cases)
    split = make_split(selected)
    output = private_path(output)
    baseline_calls = REPEATS * len(selected) + sum(c["mode"] == "prose" for c in cases)
    pilot_calls = REPEATS * (3 * len(split.development) + len(split.holdout)) + 4
    if baseline_calls + pilot_calls > 200:
        raise ValueError("Frozen set exceeds the bounded pilot request ceiling")
    plan = {
        "schema_version": 1,
        "upstream_commit": UPSTREAM,
        "method": "Harness2-inspired; existing Jobrunner guarded provider",
        "source_sha256": digest(_json(cases)),
        "system_sha256": digest(chat.SYSTEM),
        "runtime_sha256": runtime_identity(),
        "split": asdict(split),
        "cases": len(cases),
        "selection_cases": len(selected),
        "repeats": REPEATS,
        "baseline_max_calls": baseline_calls,
        "pilot_max_calls": pilot_calls,
        "phase_max_seconds": PHASE_SECONDS,
        "provider": "kaggle",
        "model": MODEL,
        "share_mail": False,
        "chosen": "baseline",
        "promotion": "requires_owner_review",
    }
    plan["plan_sha256"] = digest(_json(plan))
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    write_private(output / "cases.json", cases)
    write_private(output / "plan.json", plan)
    return plan


def load_plan(run: Path) -> tuple[list[dict[str, Any]], dict[str, Any], PilotSplit]:
    plan = read_private(run / "plan.json")
    claimed = plan.pop("plan_sha256")
    if claimed != digest(_json(plan)):
        raise ValueError("Experiment plan changed")
    plan["plan_sha256"] = claimed
    cases = read_private(run / "cases.json")
    selected = selection_cases(cases)
    split = make_split(selected)
    expected_split = json.loads(_json(asdict(split)))
    if (
        plan["schema_version"] != 1
        or plan["upstream_commit"] != UPSTREAM
        or plan["runtime_sha256"] != runtime_identity()
        or plan["cases"] != len(cases)
        or plan["selection_cases"] != len(selected)
        or plan["baseline_max_calls"]
        != REPEATS * len(selected) + sum(c["mode"] == "prose" for c in cases)
        or plan["pilot_max_calls"]
        != REPEATS * (3 * len(split.development) + len(split.holdout)) + 4
        or plan["baseline_max_calls"] + plan["pilot_max_calls"] > 200
        or plan["phase_max_seconds"] != PHASE_SECONDS
        or plan["chosen"] != "baseline"
        or plan["promotion"] != "requires_owner_review"
        or plan["source_sha256"] != digest(_json(cases))
        or plan["system_sha256"] != digest(chat.SYSTEM)
        or plan["split"] != expected_split
        or plan["repeats"] != REPEATS
        or plan["provider"] != "kaggle"
        or plan["model"] != MODEL
        or plan["share_mail"] is not False
    ):
        raise ValueError("Frozen experiment identity changed")
    return cases, plan, split


def _ready(case: dict[str, Any]) -> chat.Ready:
    found = TypeAdapter(Retrieval).validate_python(case["found"])
    return chat.Ready(
        selected="kaggle",
        include_mail=False,
        prompt=case["prompt"],
        found=found,
        selection_evidence=tuple(EvidenceItem.model_validate(v) for v in case["evidence"])
        if case["mode"] == "selection"
        else None,
        selection_summary=render_postings(found, include_sources=False),
    )


class NotReady(RuntimeError):
    def __init__(self, state: str):
        self.state = state
        super().__init__("Kaggle readiness failed")


@asynccontextmanager
async def live_provider() -> AsyncIterator[LLMProvider]:
    status = await kaggle_connection.saved_status()
    if status.state != "ready":
        raise NotReady(status.state)
    provider, model = chat.provider_for("kaggle")
    if provider.name != "kaggle" or model != MODEL:
        raise ValueError("Pilot requires the existing Kaggle provider")
    engine = create_async_engine(
        get_settings().async_database_url,
        connect_args={"server_settings": {"default_transaction_read_only": "on"}},
        pool_size=1,
        max_overflow=0,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    original = db.get_sessionmaker
    db.get_sessionmaker = lambda: factory  # type: ignore[assignment]
    try:
        yield provider
    finally:
        db.get_sessionmaker = original
        await engine.dispose()


@contextmanager
def phase_files(run: Path, phase: str) -> Iterator[Any]:
    """Exclusive writer and private, durable checkpoints for one bounded phase."""
    lock = private_path(run / f".{phase}.lock")
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as owner:
        owner.write(json.dumps({"pid": os.getpid(), "phase": phase}) + "\n")
        owner.flush()
        os.fsync(owner.fileno())
    stream = None
    try:
        journal = private_path(run / f"{phase}.{uuid4().hex}.jsonl")
        journal_fd = os.open(journal, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        stream = os.fdopen(journal_fd, "w")

        def emit(event: dict[str, Any]) -> None:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

        yield emit
    finally:
        if stream is not None:
            stream.close()
        lock.unlink(missing_ok=True)


def _baseline_runs(payload: dict[str, Any]) -> list[SelectionRun]:
    return [SelectionRun(**{**row, "ids": tuple(row["ids"])}) for row in payload["selection_runs"]]


def _review(case: dict[str, Any], reply: ChatReply, key: str, prompt: str) -> dict[str, Any]:
    return answer_support.freeze(
        case_id=key,
        system=case["system"],
        prompt=prompt,
        answer=reply.reply,
        model=reply.model,
        labels=[v["source_label"] for v in case["evidence"] if v["source_label"]],
    ).model_dump(mode="json")


async def run_phase(run: Path, phase: str) -> dict[str, Any]:
    if phase not in {"baseline", "pilot"}:
        raise ValueError("Unknown inference phase")
    run = private_path(run)
    cases, plan, split = load_plan(run)
    selected = selection_cases(cases)
    baseline: dict[str, Any] | None = None
    if phase == "pilot":
        baseline = read_private(run / "baseline.json")
        if (
            baseline["status"] != "captured_ungraded"
            or baseline["plan_sha256"] != plan["plan_sha256"]
            or len(baseline["responses"])
            != sum(REPEATS if c["mode"] == "selection" else 1 for c in cases)
        ):
            raise ValueError("Complete matching baseline required before refinement")
        validate_baseline(selected, _baseline_runs(baseline))
    destination = run / f"{phase}.json"
    caller = None
    with phase_files(run, phase) as emit:
        # Inspect the previous state only after acquiring ownership. A stale
        # pre-lock read could otherwise replay a run another process completed.
        if destination.exists():
            old: dict[str, Any] = read_private(destination)
            if old["plan_sha256"] != plan["plan_sha256"]:
                raise ValueError("Existing phase belongs to another plan")
            if old["status"] == "captured_ungraded":
                return old
            if old["status"] != "blocked_provider" or old.get("model_calls", 0) != 0:
                raise ValueError("Partial inference exists; preserve it and prepare a new run")
            write_private(run / f"{phase}.previous.{uuid4().hex}.json", old)
        state: dict[str, Any] = {
            "status": "running",
            "phase": phase,
            "plan_sha256": plan["plan_sha256"],
            "started_at": datetime.now(UTC).isoformat(),
            "model_calls": 0,
            "selection_runs": [],
            "responses": [],
            "chosen": "baseline",
            "promotion": "requires_owner_review",
        }
        try:
            write_private(destination, state)

            def checkpoint(event: dict[str, Any]) -> None:
                emit(event)
                if event["kind"] == "request":
                    # Persist intent before dispatch, including a process crash
                    # during the first awaited model call.
                    state["model_calls"] = event["attempt"]
                    write_private(destination, state)

            async with live_provider() as provider:
                caller = BudgetedCaller(
                    provider,
                    max_calls=plan[f"{phase}_max_calls"],
                    max_seconds=PHASE_SECONDS,
                    emit=checkpoint,
                )
                if phase == "pilot":
                    assert baseline is not None
                    result = await run_pilot(selected, split, _baseline_runs(baseline), caller)
                    state["result"] = result
                    frozen = {case.id: case for case in selected}
                    originals = {case["id"]: case for case in cases}
                    reviews = []
                    for row in result["runs"]:
                        case = originals[row["case_id"]]
                        variant = row["stage"].split("_", 1)[1]
                        prompt = selection_prompt(frozen[case["id"]], result["guidance"][variant])
                        if digest(prompt) != row["prompt_sha256"]:
                            raise ValueError("Candidate review prompt differs from execution")
                        ready = replace(_ready(case), prompt=prompt)
                        reply = chat.selection_reply_for(ready, MODEL, row["raw"])
                        key = f"{case['id']}:{row['stage']}:{row['repeat']}"
                        reviews.append(_review(case, reply, key, prompt))
                    write_private(run / "candidate_reviews.json", reviews)
                else:
                    catalog = {case.id: case for case in selected}
                    reviews = []
                    for case in cases:
                        repeats = REPEATS if case["mode"] == "selection" else 1
                        for repeat in range(1, repeats + 1):
                            if case["mode"] == "facts":
                                reply = ChatReply.model_validate(case["code_reply"])
                            elif case["mode"] == "selection":
                                captured = await capture_selection(
                                    catalog[case["id"]], caller, repeat=repeat
                                )
                                state["selection_runs"].append(asdict(captured))
                                reply = chat.selection_reply_for(_ready(case), MODEL, captured.raw)
                            else:
                                raw = await caller.complete(
                                    case["system"],
                                    case["prompt"],
                                    stage="baseline_prose",
                                    max_tokens=chat.ANSWER_TOKENS,
                                    temperature=chat.ANSWER_TEMPERATURE,
                                )
                                reply = chat.reply_for(_ready(case), MODEL, raw)
                            key = f"{case['id']}:baseline:{repeat}"
                            state["responses"].append(
                                {
                                    "case_id": case["id"],
                                    "repeat": repeat,
                                    "reply": reply.model_dump(mode="json"),
                                }
                            )
                            if case["mode"] != "facts":
                                reviews.append(_review(case, reply, key, case["prompt"]))
                            state["model_calls"] = caller.calls
                            write_private(destination, state)
                    write_private(run / "baseline_reviews.json", reviews)
                    validate_baseline(selected, _baseline_runs(state))
                state["status"] = "captured_ungraded"
        except NotReady as exc:
            state.update(status="blocked_provider", readiness=exc.state)
        except Exception as exc:
            # Exceptions can quote a private prompt or URL. Record type only.
            state.update(status="failed", error_type=type(exc).__name__)
        finally:
            state["model_calls"] = caller.calls if caller is not None else 0
            state["updated_at"] = datetime.now(UTC).isoformat()
            write_private(destination, state)
    return state


def summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Only operational metadata goes to stdout; never prompts or model prose."""
    keys = ("status", "phase", "model_calls", "readiness", "error_type", "chosen", "promotion")
    return {key: payload[key] for key in keys if key in payload}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    initial = commands.add_parser("prepare")
    initial.add_argument("cases", type=lambda v: Path(v).expanduser().absolute())
    initial.add_argument("output", type=lambda v: Path(v).expanduser().absolute())
    for command in ("baseline", "pilot", "status"):
        commands.add_parser(command).add_argument(
            "run", type=lambda v: Path(v).expanduser().absolute()
        )
    args = parser.parse_args()
    original_directory = Path.cwd()
    try:
        # Existing storage/vault settings are relative to the selected installation,
        # including its shared daily quota. Do not create a separate worktree quota.
        os.chdir(settings_env_file().parent)
        if args.command == "prepare":
            plan = prepare(args.cases, args.output)
            result = {
                key: plan[key]
                for key in ("cases", "selection_cases", "baseline_max_calls", "pilot_max_calls")
            }
            result["split_counts"] = {
                key: len(plan["split"][key]) for key in ("development", "holdout", "reserved")
            }
            print(_json(result))
            return 0
        if args.command == "status":
            load_plan(args.run)
            print(
                _json(
                    {
                        phase: summary(read_private(args.run / f"{phase}.json"))
                        if (args.run / f"{phase}.json").exists()
                        else {"status": "not_started"}
                        for phase in ("baseline", "pilot")
                    }
                )
            )
            return 0
        payload = asyncio.run(run_phase(args.run, args.command))
        print(_json(summary(payload)))
        return 0 if payload["status"] == "captured_ungraded" else 2
    except Exception as exc:
        print(_json({"status": "refused", "error_type": type(exc).__name__}))
        return 2
    finally:
        os.chdir(original_directory)


if __name__ == "__main__":
    raise SystemExit(main())
