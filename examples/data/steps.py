"""The functions that release.json names. A graph written as data can only name functions in modules the
host allows, here with ``--allow steps``."""


def draft(topic):
    return f"A note about {topic}."


def publish(note, verdict):
    return f"published: {note}" if verdict.get("approved") else f"held back: {note}"
