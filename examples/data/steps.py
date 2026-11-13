"""The functions that release.json names. A graph written as data can only name functions in modules the
host allows, here with ``--allow steps``."""


def draft(topic):
    return f"A note about {topic}."


def publish(note, verdict):
    return f"published: {note}" if verdict.get("approved") else f"held back: {note}"


def lead(ctx, task=None, reply=None):
    """The coordinator of scouts.json: one scout for each direction it was given, then all their reports."""
    if task is not None:
        _WAITING.clear()
        _WAITING.update(task)
        for direction in task:
            ctx.emit("send", {"to": direction, "task": f"explore {direction}"})
        return
    _REPORTS[reply["from"]] = reply["reply"]
    if _WAITING <= _REPORTS.keys():
        ctx.emit("out", {name: _REPORTS[name] for name in sorted(_WAITING)})


_WAITING: set = set()
_REPORTS: dict = {}
