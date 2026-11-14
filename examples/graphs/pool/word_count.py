import os


def word_count(path: str):
    """Count the words in a text file of the workspace. The path is relative to the top of the workspace."""
    top = os.environ.get("INTERLACING_WORKSPACE", ".")
    with open(os.path.join(top, path), encoding="utf-8") as file:
        return len(file.read().split())
