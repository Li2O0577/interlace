"""A model plans in space, a controller moves the arm, and the two go round until the task is done.

    python examples/graphs/planner_and_arm.py

The shape is that of a physical agent built on a multimodal model: observations come in, the model works
out where the gripper should go and says so as points in the world frame, the plan is checked before
anything moves, a controller turns it into motion, and the next observation decides what happens next.

    observe -> plan -> act -+
       ^                    |      until the block is where it should be, at most ten plans
       +--------------------+

Everything outside the graph is a stand-in here: the robot is a few numbers, the model is a script, the
"motion planner" moves the gripper in a straight line. What is real is what the kernel does:

* the model measures before it plans, through a tool that is an ordinary node joined by a call edge;
* a plan that breaks the action contract is given back to the model with the reason, and it corrects it;
* nothing reaches the controller that has not passed the check;
* plans and seconds are counted against a budget, and the conversation is kept to a bounded window.
"""
import json
import math

from inter1ace import AgentNode, Engine, FunctionNode, GraphBuilder, Loop, Port, Tool
from inter1ace.models import ChatResult, FakeProvider
from inter1ace.nodes import GraphNode
from inter1ace.protocols import DEFAULT

GOAL = [0.40, 0.10, 0.02]


class Bench:
    """The stand-in for the robot and its cameras."""

    def __init__(self):
        self.gripper, self.block, self.holding, self.moves = [0.0, 0.0, 0.30], [0.20, -0.10, 0.02], False, 0

    def observe(self):
        return {"instruction": "put the block on the mark", "gripper": self.gripper, "block": self.block,
                "mark": GOAL, "holding": self.holding}

    def move(self, target, jaws):
        self.moves += 1
        self.gripper = list(target)
        if jaws == "close" and math.dist(self.gripper, self.block) < 0.01:
            self.holding = True
        if self.holding:
            self.block = list(self.gripper)
        if jaws == "open":
            self.holding = False


def valid_plan(plan):
    """The action contract. What it returns is what the model is told when its plan is refused."""
    if not isinstance(plan, dict) or not isinstance(plan.get("groups"), list) or not 1 <= len(plan["groups"]) <= 2:
        return "a plan has one or two action groups under 'groups'"
    for number, group in enumerate(plan["groups"], 1):
        try:
            p0, p1 = group["target_points"]
            apart = math.dist(p0, p1)
            behind = math.dist(group["orientation_point"], [(a + b) / 2 for a, b in zip(p0, p1)])
        except (KeyError, TypeError, ValueError):
            return f"group {number} needs target_points [p0, p1] and orientation_point, each [x, y, z] in metres"
        if not 0.005 <= apart <= 0.085:
            return f"group {number}: p0 and p1 are {apart * 1000:.0f} mm apart, and must be 5 to 85 mm apart"
        if behind < 0.005:
            return f"group {number}: orientation_point is on the grasp point; put it at least 5 mm behind"
        if group.get("gripper") not in ("open", "close", "hold"):
            return f"group {number}: gripper must be open, close or hold"
    return None


def build(bench, model):
    def observe(x):
        return bench.observe()

    def measure(request):
        """What the model's Python tools would work out from the images: here, read off the bench."""
        return {"block": bench.block, "mark": GOAL, "finger_gap_mm": 40}

    def act(ctx, plan):
        for group in plan["groups"]:
            p0, p1 = group["target_points"]
            bench.move([(a + b) / 2 for a, b in zip(p0, p1)], group["gripper"])
            ctx.use(seconds=2.0)                        # what the motion took
        ctx.use(plans=1)
        return bench.observe()

    def recent(messages):
        return messages[:1] + messages[-6:] if len(messages) > 7 else messages      # the first turn and the latest ones

    planner = AgentNode("plan", model, json=True, memory=recent, max_steps=8,
                        system="Give the gripper pose as three world-frame points. Measure before you plan.",
                        tools=[Tool("measure", "Measure the scene from the current images.")],
                        outputs=[Port("plan", dict, check=valid_plan, repairs=2)])
    planning = (GraphBuilder().add(planner, FunctionNode("measure", measure, inputs=[Port("request")], outputs=[Port("y")]))
                .connect("plan.measure", "measure.request", "call").input("in", "plan.task").output("out", "plan.plan").compile(DEFAULT))
    return Loop(FunctionNode("observe", observe, inputs=[Port("x")], outputs=[Port("seen", dict)]),
                GraphNode("planning", planning, DEFAULT),
                FunctionNode("act", act, context=True, inputs=[Port("plan", dict)], outputs=[Port("seen", dict)]),
                until=lambda seen: math.dist(seen["block"], GOAL) < 0.01 and not seen["holding"], max_rounds=10).build(DEFAULT)


def scripted():
    """A script in the place of the model: it measures, makes one plan that breaks the contract, puts it
    right when told why, and then finishes the job."""
    def group(point, gripper, gap=0.04):
        x, y, z = point
        return {"target_points": [[x, y - gap / 2, z], [x, y + gap / 2, z]], "orientation_point": [x, y, z + 0.05], "gripper": gripper}

    def policy(system, messages, tools):
        last = messages[-1]
        if last["role"] == "user" and "not accepted" in last["content"]:
            seen = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "tool"))
            return ChatResult(text=json.dumps({"groups": [group(seen["block"], "close")]}), input_tokens=900, output_tokens=120)
        if last["role"] != "tool":
            return ChatResult(tool_calls=[{"id": "m", "name": "measure", "args": {}}], input_tokens=800, output_tokens=20)
        seen = json.loads(last["content"])
        if math.dist(seen["block"], GOAL) > 0.01 and not any("close" in m["content"] for m in messages if m["role"] == "assistant"):
            return ChatResult(text=json.dumps({"groups": [group(seen["block"], "close", gap=0.2)]}), input_tokens=900, output_tokens=120)
        above = [GOAL[0], GOAL[1], GOAL[2] + 0.10]
        return ChatResult(text=json.dumps({"groups": [group(above, "hold"), group(GOAL, "open")]}), input_tokens=900, output_tokens=160)
    return FakeProvider(policy)


def main():
    bench = Bench()
    told = []
    engine = Engine(build(bench, scripted()), DEFAULT, time_limit=60, budget={"plans": 10, "seconds": 600},
                    observe=lambda event: told.append(event) if event["event"] == "rejected" else None)
    result = engine.run({"in": "start"})
    print("the block is at", result.last("out")["block"], "after", bench.moves, "moves")
    print("given back to the model:", [event["problem"] for event in told])
    print("used:", result.used)
    return result, bench


if __name__ == "__main__":
    main()
