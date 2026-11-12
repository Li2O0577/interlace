"""The conversation itself: thinking depth per agent, pictures the user attaches, and rewinding the
conversation to an earlier message of the user's (to edit it or to regenerate the reply)."""

from __future__ import annotations

from .kinds import KindLookup
import base64

from .providers import THINKING_BUDGETS, THINKING_LEVELS
from .state import LEAD_ID, AgentError, AgentState


IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_MESSAGES = 3          # pictures are re-sent with the latest few user messages that carry them, older ones as paths
BUSY = ("running", "waiting_approval", "waiting_children", "waiting_parent", "waiting_user", "queued")


class ConversationMixin(KindLookup):
    # =================================================================== thinking depth

    def thinking_of(self, st: AgentState) -> str:
        """The agent's own choice, else the default for its kind (lead / sub-agents) from the settings."""
        level = st.rec.get("thinking")
        if level in THINKING_LEVELS:
            return level
        d = self._defaults()
        # Agents that solve and check the math think hard by default: a framework must not make them shallower.
        key = "thinking_" + self.kind(st).thinking
        level = d.get(key, "high" if key == "thinking_worker" else "default")
        return level if level in THINKING_LEVELS else "default"

    def _thinking_budgets(self) -> dict:
        raw = self._defaults().get("thinking_budgets") or {}
        return {k: int(raw.get(k) or v) for k, v in THINKING_BUDGETS.items()}

    def set_thinking(self, pid: str, aid: str, level: str) -> dict:
        """``level`` is one of THINKING_LEVELS, or "" to follow the settings again."""
        self.load(pid)
        st = self._get(pid, aid)
        if level and level not in THINKING_LEVELS:
            raise AgentError(f"思考深度只能是：{', '.join(THINKING_LEVELS)}")
        with self.guard:
            if level:
                st.rec["thinking"] = level
            else:
                st.rec.pop("thinking", None)
            self._save(st)
        self._emit(pid, "agent.thinking", {"agent": aid, "name": st.rec["name"], "level": self.thinking_of(st),
                                           "follow": not level, "by": "human"})
        return self.view(st)

    # =================================================================== pictures

    def _with_images(self, st: AgentState) -> tuple[list[dict], bool]:
        """Messages for the model call: the latest user messages with pictures get the picture data attached
        (not stored in the log). Returns (messages, whether any picture is attached)."""
        if st.rec.get("no_images") or not any(m.get("images") for m in st.messages):
            return st.messages, False
        root = self._root(st.project_id).resolve()
        recent = [i for i, m in enumerate(st.messages) if m.get("images")][-IMAGE_MESSAGES:]
        out, attached = list(st.messages), False
        for i in recent:
            parts = []
            for rel in st.messages[i]["images"]:
                path = (root / rel).resolve()
                mime = IMAGE_MIME.get(path.suffix.lower())
                if not mime or root not in path.parents or not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
                    continue
                parts.append((mime, base64.b64encode(path.read_bytes()).decode()))
            if parts:
                out[i] = {**st.messages[i], "image_data": parts}
                attached = True
        return out, attached

    # =================================================================== rewind / regenerate

    def rewind(self, pid: str, aid: str, seq: int, resend: bool = False) -> dict:
        """Drop the conversation from the user's message ``seq`` (a console event) on. With ``resend`` the same
        message is delivered again at once (regenerate); otherwise its text comes back for editing.
        Only the conversation is rewound: files and sub-agents stay as they are (files have git checkpoints)."""
        self.load(pid)
        st = self._get(pid, aid or LEAD_ID)
        if st.rec["status"] in BUSY:
            raise AgentError(f"「{st.rec['name']}」正在工作，先暂停或等它停下再回退")
        idx = next((i for i, m in enumerate(st.messages) if m["role"] == "user" and seq in (m.get("event_seqs") or [])), None)
        if idx is None:
            raise AgentError("找不到这条消息（可能更早的对话已被压缩），无法回退到这里")
        event = next((e for e in self.events_for(pid).replay(seq - 1) if e["seq"] == seq), None)
        data = (event or {}).get("data") or {}
        with st.io_lock:
            removed = len(st.messages) - idx
            st.messages = st.messages[:idx]
            self._rewrite_messages(st)
        with self.guard:
            st.rec["error"] = None
            st.rec.pop("no_images", None)
            if st.rec["status"] == "failed":
                st.rec["status"] = "idle"
            self._save(st)
        self._emit(pid, "agent.rewound", {"agent": st.rec["id"], "name": st.rec["name"], "seq": seq, "removed": removed,
                                          "resend": resend, "by": "human"})
        out = {"text": data.get("text", ""), "files": data.get("files") or [], "images": data.get("images") or [],
               "removed": removed}
        if resend and out["text"]:
            out["event"] = self.say(pid, st.rec["id"], out["text"], out["files"], out["images"])
        return out

    def say(self, pid: str, target: str, text: str, files: list | None = None, images: list | None = None) -> dict:
        """A message from the user typed in a conversation: logged as a console event (the timeline shows it,
        and the conversation can later be rewound to it), then delivered."""
        files, images = list(files or []), list(images or [])
        event = self.events_for(pid).append("human.message", {"text": text, "target": target,
                                                               **({"files": files} if files else {}),
                                                               **({"images": images} if images else {})}, source="human")
        if target == LEAD_ID:
            self.ensure_lead(pid)
        self.send(pid, "human", target, text, announce=False,
                  extra={"event_seq": event["seq"], **({"files": files} if files else {}), **({"images": images} if images else {})})
        return event
