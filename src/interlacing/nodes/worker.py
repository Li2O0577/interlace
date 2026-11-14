"""An agent that works freely inside a workspace: it reads, writes and changes files there and runs commands."""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from ..base import NodeError
from .agent import AgentNode, Tool
from .workspace import Area, Workspace


class WorkerNode(AgentNode):
    """An ``AgentNode`` with a place to work. ``area`` is an ``Area`` of a workspace, or a directory, which
    then is all the node's own. The model is given tools for the files there (``list_files``, ``read_file``,
    ``write_file``, ``edit_file``, ``delete_file``, ``move_file``) and, with ``commands`` (the default),
    ``run_command``: one line for the shell, run in the node's own directory for at most ``command_timeout``
    seconds. It is told where it works, and what it may read and write.

    Everything else is an agent's: the same inputs and outputs, the same checks on what it sends, the same
    tools from the graph or from a tool pool beside its own, the same limits on steps, time and budget. A
    file it sends out of a port that carries files must be one its area lets it read.

    The file tools stay inside the area. A command does not have to: it runs with the rights of this
    process, so ``commands`` is for a node that is trusted with them.
    """

    kind = "worker"

    def __init__(self, id: str, model: Any, area, *, system: str = "", tools: Iterable[Tool] = (),
                 commands: bool = True, command_timeout: float | None = 120.0, **settings):
        if not isinstance(area, Area):
            area = Workspace(area).whole(id)
        if command_timeout is not None and not command_timeout > 0:
            raise NodeError(f"{id}: command_timeout must be a positive number of seconds")
        own = area.tools(commands=bool(commands), timeout=command_timeout)
        more = tuple(tools)
        taken = {tool.name for tool in own} & {tool.name for tool in more}
        if taken:
            raise NodeError(f"{id}: {', '.join(sorted(taken))} are names of a worker's own tools")
        settings.setdefault("files", area.root)
        super().__init__(id, model, system=f"{system}\n\n{area.about()}".strip(), tools=[*own, *more], area=area, **settings)
        self.brief, self.more = system, more
        self.commands, self.command_timeout = bool(commands), command_timeout

    def config(self) -> Mapping:
        return {**super().config(), "system": self.brief,
                "tools": [tool.describe() if tool.run is not None else tool.spec() for tool in self.more],
                "commands": self.commands, "command_timeout": self.command_timeout}

    def open(self, ctx) -> None:
        self.area.home.mkdir(parents=True, exist_ok=True)
