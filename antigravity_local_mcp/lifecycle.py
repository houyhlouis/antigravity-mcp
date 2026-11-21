"""Task and turn transitions, including durable folder reservations."""
import time
import uuid

from .errors import BridgeError
from .security import preview

LOST = {"needs_resume", "uncertain"}
STALL_HINT = ("Antigravity does not forward a native subagent's questions or permission prompts over ACP. "
              "The subagent may be waiting. Send a follow-up asking the agent to check its subagents, interrupt, "
              "or close the task. To avoid this, use mode accept-edits or auto, or Antigravity allow rules.")


def background():
    return {"state": "none", "since": None, "open_tool_calls": [], "subagents_possibly_running": False, "hint": None}


def new_task(project, args, dirs):
    now = time.time()
    return dict(task_id=str(uuid.uuid4()), project_id=project["project_id"], workdir=project["path"],
                name=args.get("name") or project["path"], engine=args.get("engine", "acp"), add_dirs=dirs, agent=args.get("agent"),
                sandbox=args.get("sandbox", False), session_id=None, status="starting", activity="none",
                turn_id=None, turn_status=None, stop_reason=None, revision=0, summary="", title=None,
                mode=None, model=None, effort=None, config_options=[], available_modes=[], available_models=[],
                available_commands=[], context={}, usage={"last_turn": {}, "cumulative": {}}, files=[], commands=[],
                plan=[], subagents=[], background=background(), pending_requests=[], claims=[], error=None,
                process={"pid": None, "alive": False, "started_at": None, "exit_code": None}, session_open=False,
                tier=None, created_at=now, updated_at=now, capabilities={}, last_activity=now,
                worktree=({k: project[k] for k in ("repo", "branch", "base_commit", "git_common_dir", "checkout_id") if k in project}
                          if project["kind"] == "worktree" else None))


def claims(task):
    reasons = []
    if task["status"] in LOST:
        reasons.append(("recovery", "Verify the previous process and resume or close the task"))
    elif task["process"]["alive"] or task["status"] == "starting":
        if task["engine"] == "headless" and task["process"]["alive"]:
            reasons.append(("headless", "A native print-mode process is alive"))
        if task["status"] in ("starting", "interrupting") or task["activity"] == "turn":
            reasons.append(("turn", "A turn is starting, running or being interrupted"))
        if task["pending_requests"]:
            reasons.append(("request", "A native request is waiting for an answer"))
        if task["activity"] == "background" or task["background"]["open_tool_calls"]:
            reasons.append(("background", "Antigravity has active background work"))
        if task["background"]["subagents_possibly_running"]:
            reasons.append(("subagents", "Native subagents may still be running"))
    task["claims"] = [{"path": path, "kind": k, "reason": v}
                      for path in (task["workdir"], *task["add_dirs"]) for k, v in reasons]


def snapshot(t):
    return {k: t[k] for k in ("status", "turn_id", "turn_status", "activity")}


def ready(t):
    t.update(status="idle", activity="none", session_open=True, error=None)


def imported_turn(t, turn):
    # A replay contains content, but has no authoritative stop reason or timing.
    turn.update(started_at=None, ended_at=None, status="unknown", evidence_complete=False, evidence_lost=True)
    t.update(status="starting", activity="none", turn_status="unknown")


def opening(t):
    t.update(status="starting", activity="none", session_open=False)


def next_turn_id(t):
    number = int(t["turn_id"][5:]) + 1 if t["turn_id"] else 1
    return f"turn-{number}"


def start_turn(t, prompt):
    t.update(status="running", activity="turn", turn_status="running", turn_id=next_turn_id(t),
             stop_reason=None, error=None, summary="")
    return dict(turn_id=t["turn_id"], prompt_preview=preview(prompt, 500), started_at=time.time(),
                ended_at=None, stop_reason=None, usage={}, final_message="", files=[], commands=[], steers=[],
                background_segments=[], denied_actions=[], tool_errors=[], structured_output=None,
                evidence_complete=False, evidence_lost=False, status="running")


def add_usage(total, delta):
    for key, value in delta.items():
        if type(value) in (int, float):
            total[key] = total.get(key, 0) + value


def finish_turn(t, turn, reason, usage, error=None):
    status, ts = "completed", "completed"
    if error:
        status, ts = "failed", "failed"
    elif reason in ("cancelled", "closed", "broker_stopped"):
        status, ts = "interrupted", "cancelled"
    elif reason == "refusal":
        status, ts, error = "failed", "refused", BridgeError("agent_refused", "Antigravity refused this turn")
    elif reason in ("max_tokens", "max_turn_requests"):
        status, ts = "failed", "truncated"
        error = BridgeError("turn_truncated", "The turn reached its limit", next_steps=["Send a follow-up to continue."])
    elif reason != "end_turn":
        status, ts, error = "failed", "unknown", BridgeError("native_rejected", f"Unknown stop reason: {reason}")
    t.update(status=status, activity="none", turn_status=ts, stop_reason=reason, error=error.to_dict() if error else None)
    turn.update(ended_at=time.time(), stop_reason=reason, usage=usage, status=ts,
                evidence_complete=not turn["evidence_lost"] and ts != "unknown")
    t["usage"]["last_turn"] = usage
    add_usage(t["usage"]["cumulative"], usage)


def waiting(t):
    if t["status"] in LOST or t["status"] == "interrupting":
        return
    if t["pending_requests"]:
        if t["status"] != "waiting":
            t["before_wait"] = t["status"]
        t["status"] = "waiting"
    elif t["status"] == "waiting":
        t["status"] = t.pop("before_wait", "running" if t["activity"] == "turn" else "completed")


def interrupt(t):
    t["status"] = "interrupting"


def background_state(t, active):
    t["activity"] = "background" if active else "none"
    t["background"].update(state="active" if active else "settled", since=time.time() if active else None)


def lost(t, turn=None, uncertain=False):
    t.setdefault("before_loss", snapshot(t))
    t["previous_pid"] = t["process"]["pid"]
    t["previous_start_identity"] = t["process"].get("start_identity")
    t.update(status="uncertain" if uncertain else "needs_resume", activity="none", session_open=False)
    if turn and not turn["ended_at"]:
        turn.update(status="unknown", ended_at=time.time(), evidence_complete=False, evidence_lost=True)
        t["turn_status"] = "unknown"


def restored(t, turn, replayed):
    if turn and turn["status"] == "unknown" and replayed:
        turn.update(status="completed", final_message=replayed, evidence_complete=False)
        t.update(turn_status="completed", summary=preview(replayed, 16384))
    t.pop("before_loss", None)
    previous = t["background"]
    t["background"] = {**background(), "subagents_possibly_running": previous["subagents_possibly_running"],
                       "hint": previous["hint"]}
    ready(t)


def failed(t, error):
    t.update(status="failed", error=error.to_dict(), activity="none")


def closed(t, archive=False, preserve_interrupted=False):
    t.update(session_open=False, activity="none", background=background())
    if archive:
        t["status"] = "archived"
    elif not preserve_interrupted:
        t["status"] = "closed"
