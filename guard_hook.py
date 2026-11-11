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
import re
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


# How many independent sources must surface a tool-supplied value before it may drive
# this argument. One feed (say, the one the repo author controls) is not enough.
CORROBORATE = {"symbol": 2}


def session(transcript_path: str) -> tuple[str, list]:
    """(what the human typed, what each tool returned) for this session.

    Tool output is kept apart from the user's words, by block type: tool results
    are stored as user-role messages too, and counting them as the user's words
    would let a feed "account for" its own values. Each tool segment keeps its
    source (the MCP server) and the arguments the agent called it with.
    """
    parts, calls, segments = [], {}, []
    path = Path(transcript_path or "")
    if not path.is_file():
        return "", []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("isMeta") or entry.get("isSidechain"):
            continue
        content = (entry.get("message") or {}).get("content")
        if entry.get("type") == "assistant" and isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    calls[b.get("id")] = (b.get("name", ""), b.get("input") or {})
        if entry.get("type") != "user":
            continue
        if isinstance(content, str):
            parts.append(content)
            continue
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif b.get("type") == "tool_result":
                name, args = calls.get(b.get("tool_use_id"), ("", {}))
                body = b.get("content")
                if isinstance(body, list):
                    body = "\n".join(x.get("text", "") for x in body if isinstance(x, dict))
                segments.append({"source": _server(name), "text": str(body or ""),
                                 "asked": json.dumps(args)})
    return "\n".join(parts), segments


def operator_text(transcript_path: str) -> str:
    """Everything the human typed in this session, and nothing a tool returned."""
    return session(transcript_path)[0]


def _server(tool_name: str) -> str:
    # mcp__<server>__<tool>: the server is the source; two tools on one server are
    # one source, not two
    bits = tool_name.split("__")
    return bits[1] if len(bits) >= 3 and bits[0] == "mcp" else tool_name


def _mentions(value, text: str) -> bool:
    v = str(value).strip()
    return bool(v) and re.search(r"(?<!\w)" + re.escape(v) + r"(?!\w)", text, re.I) is not None


def sources_for(value, segments: list) -> list:
    """Sources that surfaced this value on their own.

    A source that only echoed the value back because the agent asked it about that
    value (a quote for PUMP mentions PUMP) did not surface it, so it is not counted.
    Without this, the agent could "corroborate" anything by looking it up.
    """
    found = []
    for seg in segments:
        if _mentions(value, seg["text"]) and not _mentions(value, seg["asked"]):
            if seg["source"] not in found:
                found.append(seg["source"])
    return found


def decide(tool_input: dict, text: str, accounts: list,
           segments: list | None = None) -> tuple[str, str, list]:
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
    if contradicted:
        return ("deny", "UBAG provenance: you asked for a different "
                f"{', '.join(contradicted)} than this order uses. Order blocked.", origins)

    # A value nobody typed that a tool supplied: which sources stand behind it?
    single = []
    for name, need in CORROBORATE.items():
        if dict(origins).get(name) != UNSOURCED or name not in args:
            continue
        found = sources_for(args[name], segments or [])
        origins = [(n, f"TOOL({'+'.join(found)})" if n == name and found else o)
                   for n, o in origins]
        if found and len(found) < need:
            single.append(f"{name} (only from {', '.join(found)})")
    if single:
        return ("deny", "UBAG provenance: this order's "
                f"{'; '.join(single)} is supported by fewer than "
                f"{max(CORROBORATE.values())} independent sources, so one data feed alone "
                "would be deciding a trade. Order blocked.", origins)

    invented = [n for n, o in origins if o == UNSOURCED or o.startswith("TOOL(")]
    if invented:
        return ("ask", "UBAG provenance: nobody in this conversation asked for this "
                f"{', '.join(invented)}; it came from tool data. Confirm it yourself.",
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
    text, segments = session(event.get("transcript_path", ""))
    decision, reason, origins = decide(event.get("tool_input") or {}, text, accounts, segments)
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
