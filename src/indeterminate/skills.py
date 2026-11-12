"""Skills: experience manuals the team reads on demand (how to write an abstract, how problem types are
usually attacked, ready-to-adapt method code).

A skill is a folder with a ``SKILL.md`` (the Claude skill format: YAML-style front matter with ``name`` and
``description``, then Markdown) and optional extra files such as code templates. Built-in skills live in
``knowledge/skills/``; the user's own (imported or edited) in ``~/.mch/skills/`` and win over a built-in
with the same id. Every skill can be switched off globally, and each project can override the global switch.

Agents only see a one-line index of the skills that are on; the lead picks skills for each sub-agent when it
spawns one (their full text goes into that agent's prompt), and any agent can read one with ``skill.load``.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import threading
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

BUILTIN_DIR: Path | None = None    # the host sets this (or passes ``builtin``) when it ships skills of its own
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_SKILL_BYTES = 200_000          # SKILL.md
MAX_ZIP_BYTES = 2_000_000          # an imported skill folder, unpacked
PROMPT_BODY_CHARS = 8000           # one chosen skill inside an agent's prompt
PROMPT_TOTAL_CHARS = 16000         # all chosen skills together


class SkillError(Exception):
    pass


@dataclass
class Skill:
    id: str
    title: str
    description: str
    roles: list[str] = field(default_factory=list)
    stages: list[str] = field(default_factory=list)
    source: str = "builtin"        # builtin | user
    dir: Path = Path()
    overrides_builtin: bool = False

    def public(self) -> dict:
        return {"id": self.id, "title": self.title, "description": self.description, "roles": self.roles,
                "stages": self.stages, "source": self.source, "overrides_builtin": self.overrides_builtin,
                "files": self.files()}

    def files(self) -> list[str]:
        return sorted(p.relative_to(self.dir).as_posix() for p in self.dir.rglob("*")
                      if p.is_file() and p.name != "SKILL.md" and not p.name.startswith("."))

    def body(self) -> str:
        return split_front_matter(read_text(self.dir / "SKILL.md"))[1].strip()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


# --------------------------------------------------------------------------- front matter

def _value(raw: str):
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        return [x.strip().strip("'\"") for x in raw[1:-1].split(",") if x.strip()]
    return raw.strip("'\"")


def split_front_matter(text: str) -> tuple[dict, str]:
    """The small subset of YAML that skill headers use: ``key: value``, ``[a, b]`` lists, ``- item`` lists
    and folded (``>``/``|``) or indented continuation lines."""
    if not text.startswith("---"):
        return {}, text
    end = re.search(r"^---\s*$", text[3:], re.M)
    if not end:
        return {}, text
    head, body = text[3:3 + end.start()], text[3 + end.end():]
    meta: dict = {}
    key = None
    for line in head.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if m and not line[0].isspace():
            key, val = m.group(1), m.group(2)
            meta[key] = "" if val.strip() in (">", "|", ">-", "|-") else _value(val)
            continue
        if key is None:
            continue
        item = line.strip()
        if item.startswith("- "):
            cur = meta.get(key)
            meta[key] = (cur if isinstance(cur, list) else []) + [item[2:].strip().strip("'\"")]
        elif isinstance(meta.get(key), str):
            meta[key] = (meta[key] + " " + item).strip()
    return meta, body


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "-", text.strip().lower()).strip("-")
    return s[:64]


def _as_list(v) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in str(v or "").split(",") if x.strip()]


def parse(text: str, fallback_id: str = "") -> tuple[str, dict, str]:
    """(id, header, body) of a SKILL.md; raises SkillError when it is not a usable skill."""
    meta, body = split_front_matter(text)
    name = str(meta.get("name") or "").strip()
    sid = name if ID_RE.match(name or "") else slug(name) or fallback_id
    if not sid or not ID_RE.match(sid):
        raise SkillError("技能需要一个英文 ID：在开头的 --- 头部写 name: 例如 name: paper-writing")
    if not str(meta.get("description") or "").strip():
        raise SkillError("技能需要一句 description，说明什么时候用它（Agent 只靠这一句决定要不要读全文）")
    if not body.strip():
        raise SkillError("技能正文是空的")
    return sid, meta, body


# --------------------------------------------------------------------------- store

class SkillStore:
    """Built-in + user skills, and the global on/off switch (``~/.mch/skills.json``)."""

    def __init__(self, home: Path, builtin: Path | None = None):
        self.user_dir = home / "skills"
        self.state_path = home / "skills.json"
        self.builtin_dir = Path(builtin or BUILTIN_DIR or home / ".no-builtin-skills")
        self._lock = threading.RLock()
        self._cache: tuple[tuple, dict[str, Skill]] | None = None

    # ---- catalog
    def _scan(self, base: Path, source: str) -> dict[str, Skill]:
        out: dict[str, Skill] = {}
        if not base.is_dir():
            return out
        for md in sorted(base.glob("*/SKILL.md")):
            try:
                sid, meta, _ = parse(read_text(md), md.parent.name)
            except (SkillError, OSError, UnicodeDecodeError):
                continue
            sid = md.parent.name if ID_RE.match(md.parent.name) else sid
            out[sid] = Skill(sid, str(meta.get("title") or meta.get("name") or sid), str(meta["description"]).strip(),
                             _as_list(meta.get("roles")), _as_list(meta.get("stages")), source, md.parent)
        return out

    def _signature(self) -> tuple:
        sig = []
        for base in (self.builtin_dir, self.user_dir):
            if base.is_dir():
                for md in base.glob("*/SKILL.md"):
                    try:
                        sig.append((str(md), md.stat().st_mtime_ns))
                    except OSError:
                        pass
        return tuple(sorted(sig))

    def catalog(self) -> dict[str, Skill]:
        with self._lock:
            sig = self._signature()
            if self._cache is None or self._cache[0] != sig:
                skills = self._scan(self.builtin_dir, "builtin")
                for sid, sk in self._scan(self.user_dir, "user").items():
                    sk.overrides_builtin = sid in skills
                    skills[sid] = sk
                self._cache = (sig, skills)
            return dict(self._cache[1])

    def get(self, sid: str) -> Skill:
        sk = self.catalog().get(sid)
        if not sk:
            raise SkillError(f"没有技能 {sid}")
        return sk

    # ---- switches
    def disabled(self) -> set[str]:
        try:
            return set(json.loads(self.state_path.read_text(encoding="utf-8")).get("disabled", []))
        except (OSError, ValueError):
            return set()

    def set_enabled(self, sid: str, enabled: bool) -> None:
        self.get(sid)
        with self._lock:
            off = self.disabled()
            (off.discard if enabled else off.add)(sid)
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({"disabled": sorted(off)}, ensure_ascii=False, indent=2), encoding="utf-8")

    def listing(self, overrides: dict | None = None) -> list[dict]:
        """Every skill with its global switch, the project's override (None: follows the global switch) and
        whether agents of that project see it."""
        off, overrides = self.disabled(), overrides or {}
        out = []
        for sid, sk in sorted(self.catalog().items(), key=lambda kv: (kv[1].source != "builtin", kv[0])):
            glob = sid not in off
            proj = overrides.get(sid)
            out.append({**sk.public(), "enabled": glob, "project": proj if isinstance(proj, bool) else None,
                        "active": proj if isinstance(proj, bool) else glob})
        return out

    def active(self, overrides: dict | None = None) -> list[Skill]:
        cat = self.catalog()
        return [cat[s["id"]] for s in self.listing(overrides) if s["active"]]

    # ---- editing (user skills only)
    def save(self, text: str, sid: str | None = None) -> Skill:
        if len(text.encode("utf-8")) > MAX_SKILL_BYTES:
            raise SkillError("技能文件太大（上限 200KB），把细节拆到附带文件里")
        new_id, _, _ = parse(text, sid or "")
        sid = sid or new_id
        if not ID_RE.match(sid):
            raise SkillError(f"技能 ID 不合法：{sid}")
        with self._lock:
            target = self.user_dir / sid
            if not target.exists() and sid in self.catalog() and self.catalog()[sid].source == "builtin":
                # editing a built-in: copy its extra files so the user's version stays complete
                shutil.copytree(self.catalog()[sid].dir, target, ignore=shutil.ignore_patterns("SKILL.md"))
            target.mkdir(parents=True, exist_ok=True)
            (target / "SKILL.md").write_text(text, encoding="utf-8")
        return self.get(sid)

    def import_zip(self, data: bytes) -> Skill:
        """A zipped skill folder (SKILL.md at the top or inside one folder)."""
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise SkillError("不是有效的 zip 文件") from exc
        names = [n for n in zf.namelist() if not n.endswith("/") and "__MACOSX" not in n]
        md = sorted((n for n in names if n.rsplit("/", 1)[-1] == "SKILL.md"), key=lambda n: n.count("/"))
        if not md:
            raise SkillError("压缩包里没有 SKILL.md")
        prefix = md[0][: -len("SKILL.md")]
        if sum(zf.getinfo(n).file_size for n in names) > MAX_ZIP_BYTES:
            raise SkillError("技能文件夹太大（上限 2MB）")
        text = zf.read(md[0]).decode("utf-8-sig")
        sid, _, _ = parse(text, slug(prefix.strip("/").rsplit("/", 1)[-1]))
        with self._lock:
            target = self.user_dir / sid
            if target.exists():
                shutil.rmtree(target)
            for n in names:
                if not n.startswith(prefix):
                    continue
                rel = Path(n[len(prefix):])
                if rel.is_absolute() or ".." in rel.parts:
                    continue
                dest = target / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(n))
        return self.get(sid)

    def delete(self, sid: str) -> None:
        sk = self.get(sid)
        if sk.source != "user":
            raise SkillError("内置技能不能删除，可以关闭")
        with self._lock:
            shutil.rmtree(sk.dir)

    def read(self, sid: str, file: str = "") -> str:
        sk = self.get(sid)
        if not file:
            return read_text(sk.dir / "SKILL.md")
        path = (sk.dir / file).resolve()
        if sk.dir.resolve() not in path.parents or not path.is_file():
            raise SkillError(f"技能 {sid} 里没有文件 {file}")
        return read_text(path)


_stores: dict[tuple, SkillStore] = {}


def store_for(home: Path, builtin: Path | None = None) -> SkillStore:
    key = (Path(home), Path(builtin) if builtin else BUILTIN_DIR)
    if key not in _stores:
        _stores[key] = SkillStore(Path(home), builtin)
    return _stores[key]


def project_overrides(root: Path) -> dict:
    try:
        v = json.loads((root / "project.json").read_text(encoding="utf-8")).get("skills") or {}
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------- prompts

def audience(sk: Skill, role_name=lambda r: r) -> str:
    """The tag the lead sees: whether a skill is for itself or which sub-agent roles it suits."""
    from .agents.kinds import Lead
    subs = [r for r in sk.roles if r != Lead.role]
    parts = (["主控自用"] if Lead.role in sk.roles else []) + ([f"适合派给：{'、'.join(role_name(r) for r in subs)}"] if subs else [])
    return "；".join(parts) or "通用"


def _kind(rec: dict, kind=None):
    if kind is None:
        from .agents import kinds
        kind = kinds.DEFAULT.of(rec.get("role"))
    return kind


def granted(rec: dict, active: set[str], kind=None) -> list[str]:
    """Skills an agent may read: the controller every active one; a sub-agent only those it was given."""
    if _kind(rec, kind).controller:
        return sorted(active)
    return [s for s in rec.get("skills") or [] if s in active]


def prompt_section(store: SkillStore, root: Path, rec: dict, role_name=lambda r: r, kind=None) -> str:
    """The skills part of an agent's prompt. The lead sees an index of every active skill, tagged with who it
    is for; a sub-agent sees nothing but the full text of the skills its parent (or the user) gave it."""
    active = {s.id: s for s in store.active(project_overrides(root))}
    kind = _kind(rec, kind)
    if kind.controller:
        if not active:
            return ""
        lines = ["## 技能（经验手册）",
                 "下面是已开启的技能，只有你能看到这份目录。标“主控自用”的需要时用 skill.load 读全文；"
                 "派子 Agent 时在 agent.spawn 的 skills 里写上它用得到的技能 ID——子 Agent 只能读你给它的技能，"
                 "一开始就会读到全文（不要把技能内容抄进任务里）。"]
        lines += [f"- {s.id}（{s.title}；{audience(s, role_name)}）：{s.description}" for s in active.values()]
        return "\n".join(lines)
    chosen = [active[s] for s in granted(rec, set(active), kind)]
    if not chosen:
        return ""
    if kind.skill_index:
        # Someone solving a whole question reads the problem first; manuals stay one call away.
        lines = ["## 技能（上级为你选的经验手册，供参考）", "需要时用 skill.load 读全文；与这一问无关的不必读。"]
        lines += [f"- {s.id}（{s.title}）：{s.description}" for s in chosen]
        return "\n".join(lines)
    out = "## 技能（上级为你选的经验手册，按它做）"
    budget = PROMPT_TOTAL_CHARS
    for s in chosen:
        body = s.body()
        cut = body[:max(0, min(PROMPT_BODY_CHARS, budget))]
        budget -= len(cut)
        files = s.files()
        out += (f"\n\n### {s.title}（{s.id}）\n" + (cut or "（篇幅原因未放入，用 skill.load 读取）")
                + ("\n……（后文用 skill.load 读取）" if cut and len(cut) < len(body) else "")
                + (f"\n附带文件：{', '.join(files)}（用 skill.load 的 file 参数读取）" if files else ""))
    return out
