"""An application that knows nothing of interlacing: it reads a JSON object and writes one."""
import collections
import json
import os
import sys

given = json.load(sys.stdin)
words = given["text"].lower().split()
print(f"counting {len(words)} words")      # free to print: the answer goes to the file below
answer = {"common": collections.Counter(words).most_common(given.get("top", 3)), "total": len(words)}
with open(os.environ["INTERLACING_OUT"], "w", encoding="utf-8") as out:
    json.dump(answer, out)
