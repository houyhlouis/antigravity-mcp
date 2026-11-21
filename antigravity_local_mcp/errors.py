"""Structured errors shared by every module.

Every refusal or failure that reaches the MCP client carries a stable ``code``
from ``CODES``. Decide what to do from the code, never from the message text.
"""

CODES = {
    # Problems with the call
    "invalid_arguments": "An argument is missing, unknown, out of range, or does not belong to the chosen action",
    "invalid_response": "The answer does not fit the pending request",
    "not_found": "Unknown task, request, project, worktree or artifact",
    "operation_conflict": "The operation key was already used with different arguments",
    "result_too_large": "The requested page does not fit in one result",
    # The task is not ready for this action
    "state_conflict": "The task's current state does not allow the action",
    "stale_turn": "expected_turn_id is no longer the current turn",
    "stale_request": "The pending request was already answered, expired or is being answered",
    "human_review_required": "Only a person may approve this request",
    "workspace_busy": "Another task still reserves this folder",
    "too_many_tasks": "The limit of live Antigravity sessions is reached",
    # The outcome is unknown
    "operation_uncertain": "An earlier attempt with this key may or may not have taken effect",
    "native_timeout": "Antigravity did not answer in time",
    "transport_unavailable": "The connection to the broker or to Antigravity was lost",
    "internal_error": "An unexpected failure inside the server",
    # Antigravity refused or lacks a feature
    "native_rejected": "Antigravity refused the request",
    "optional_capability_unavailable": "The installed Antigravity CLI does not offer this feature",
    "not_signed_in": "Antigravity CLI is not signed in for this OS account",
    "quota_exhausted": "The account's Antigravity quota for this model is used up",
    "rate_limited": "Antigravity asked to slow down",
    "agent_refused": "The agent refused the turn",
    "turn_truncated": "Antigravity reached the turn limit",
    "turn_timeout": "The turn exceeded its time limit",
    # Installation and stored data
    "agy_not_found": "The Antigravity CLI executable was not found",
    "agy_unsupported": "The installed Antigravity CLI is too old or lacks ACP",
    "broker_identity_mismatch": "A broker from a different version or configuration is running",
    "state_newer_schema": "The state database was written by a newer version",
    "state_invalid": "Stored data failed a consistency check",
    "git_unavailable": "Git is missing or too old for worktrees",
    "worktree_refused": "The worktree action was refused to protect unsaved work or the repository",
    "bridge_error": "Any other refusal",
}


class RedactedText(str):
    """A preview whose contents were replaced before serialization."""


class BridgeError(Exception):
    """A refusal or failure with a stable code and machine-readable hints."""

    def __init__(self, code, message, *, retryable=False, uncertain=False, next_steps=None, ids=None, **extra):
        if code not in CODES:
            raise ValueError(f"unknown error code {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = bool(retryable)
        self.uncertain = bool(uncertain)
        self.next_steps = list(next_steps or [])
        self.ids = dict(ids or {})
        self.extra = extra

    def to_dict(self):
        error = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "uncertain": self.uncertain,
            "next_steps": self.next_steps,
            "ids": self.ids,
        }
        error.update(self.extra)
        if isinstance(self.message, RedactedText):
            error["redacted"] = True
        return error

    @classmethod
    def from_dict(cls, data):
        return cls(**data)


def native_error(error):
    from .security import redact
    native = error.get("code")
    message = redact(str(error.get("message", "Antigravity rejected the request")))
    folded = message.casefold()
    steps = []
    if native == -32000 and "authenticat" in folded:
        code = "not_signed_in"
        steps = ["Run agy in a terminal on this machine as the same OS account and sign in.",
                 "Over SSH, sign in interactively with agy acp --login as that same account."]
    elif native == -32602:
        code = "native_rejected"
    elif any(marker in folded for marker in ("resource_exhausted", "quota", "weekly limit", "spend cap")):
        code = "quota_exhausted"
    elif native == 429 or any(marker in folded for marker in ("429", "rate limit", "rate_limit")):
        code = "rate_limited"
    else:
        code = "native_rejected"
    return BridgeError(code, message, native_code=native, next_steps=steps)
