"""UBAG provenance guard as a Claude Code PreToolUse hook, in front of a broker connector.

Claude Code calls this before every matching tool call and passes the call on
stdin, including the path of the session transcript. The guard asks one question
of the proposed order: did each value come from the USER'S OWN WORDS (or the
user's own system), or did it only appear in data a tool returned?

    symbol / quantity the user typed        -> allowed
    account the user owns (accounts.json)   -> allowed (state)
    a value the user gave differently       -> DENIED  (500 where they said 10)
    a value nobody gave                     -> ASK the human (a symbol that only
                                               appeared in a research note)

Operator text is ONLY what the human typed. Tool results are recorded in the
transcript as user-role messages too, and including them would let the attacker's
own note "account for" the attacker's own values, so they are skipped by block type.

No broker cooperation is needed: the check sits in the client, which is the one
place that sees both the request and the tool output. Deterministic, no model, no
network. The log keeps field names and origins only, never the user's request or
the argument values (UBAG does not store intent).

Fails closed: any error here blocks the order (exit code 2).
"""
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# the provenance matcher is UBAG's, from github.com/mohameduk/UBAG (clone it next to
# this folder, or point UBAG_CORE at its ubag-core directory)
UBAG_CORE = Path(os.environ.get("UBAG_CORE", HERE.parent / "UBAG" / "ubag-core"))
sys.path.insert(0, str(UBAG_CORE))

from ubag_core.provenance import MISMATCH, UNSOURCED, trace_arguments  # noqa: E402

# Words a person uses for each side. "purchase 10 AAPL" is a buy the user asked for;
# without this the side would be reported as unsourced on ordinary wording.
SIDE_WORDS = {"buy": ("buy", "purchase", "acheter", "achète"),
              "sell": ("sell", "vendre", "vends")}


def operator_text(transcript_path: str) -> str:
    """Everything the human typed in this session, and nothing a tool returned."""
    parts = []
    path = Path(transcript_path or "")
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            # tool_result blocks are the attacker's channel: never count them
            parts.extend(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return "\n".join(parts)


def decide(tool_input: dict, text: str, accounts: list) -> tuple[str, str, list]:
    """(decision, reason, origins) for one proposed order."""
    args = dict(tool_input)
    side = str(args.pop("side", "")).lower()
    report = trace_arguments(args, operator_text=text,
                             state_values=[a["id"] for a in accounts])
    origins = [(o.name, o.origin) for o in report.origins]

    folded = text.lower()
    if side and any(w in folded for w in SIDE_WORDS.get(side, (side,))):
        origins.append(("side", "OPERATOR"))
    else:
        origins.append(("side", UNSOURCED))

    contradicted = [n for n, o in origins if o == MISMATCH]
    invented = [n for n, o in origins if o == UNSOURCED]
    if contradicted:
        return ("deny", "UBAG provenance: you asked for a different "
                f"{', '.join(contradicted)} than this order uses. Order blocked.", origins)
    if invented:
        return ("ask", "UBAG provenance: nobody in this conversation asked for this "
                f"{', '.join(invented)}; it appeared only in tool data. Confirm it yourself.",
                origins)
    return ("allow", "UBAG provenance: every value in this order came from you "
            "or your own accounts.", origins)


def log(run_dir: Path, tool: str, decision: str, origins: list) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "guard_log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"time": time.strftime("%H:%M:%S"), "tool": tool,
                            "decision": decision,
                            "origins": dict(origins)}) + "\n")


def main() -> None:
    event = json.load(sys.stdin)
    accounts = json.loads((HERE / "accounts.json").read_text(encoding="utf-8"))
    text = operator_text(event.get("transcript_path", ""))
    decision, reason, origins = decide(event.get("tool_input") or {}, text, accounts)
    log(Path(os.environ.get("DEMO_RUN_DIR", HERE / "runs" / "scratch")),
        event.get("tool_name", ""), decision, origins)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason}}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # fail closed: a broken guard must not let orders through
        print(f"UBAG provenance guard failed, order blocked: {exc}", file=sys.stderr)
        sys.exit(2)
