"""Devices as nodes: a camera feeds frames, a controller reads the current speed setting and commands an arm.

    python examples/graphs/camera_to_arm.py

Three kinds of edge are at work. Frames arrive as ``latest``: if the controller is busy, an older frame is
replaced, not queued. The speed setting is ``state``: it is read with every frame and never starts the
controller by itself. Commands to the arm are a ``call``: the controller waits for the arm's answer.

The camera and the arm here are stand-ins (a list of frames, a class that keeps a position). A real device
goes in the same place: ``read`` yields what the device produces, ``write`` sends it a command, ``connect``
and ``disconnect`` open and close it.
"""
from indeterminate.base import GraphBuilder, Port
from indeterminate.engine import Engine
from indeterminate.nodes import FunctionNode, SinkNode, SourceNode
from indeterminate.protocols import DEFAULT

FRAME = {"target": int}


class Arm:
    def __init__(self):
        self.position, self.log = 0, []

    def connect(self):
        self.log.append("connected")

    def move(self, command):
        self.position += command["by"]
        self.log.append(f"moved to {self.position}")
        return {"position": self.position}

    def disconnect(self):
        self.log.append("disconnected")


def control(ctx, frame, speed=1):
    """Move toward the target the frame shows, at most ``speed`` at a time, and say where the arm is now."""
    error = frame["target"] - control.position
    step = max(-speed, min(speed, error))
    if step:
        control.position = ctx.call("arm", {"by": step})["position"]
    return control.position


def build(arm, frames, speed=2):
    control.position = arm.position
    camera = SourceNode("camera", lambda: iter(frames), schema=FRAME)
    setting = FunctionNode("setting", lambda: speed, outputs=[Port("y", int)])
    controller = FunctionNode("controller", control, context=True, inputs=[Port("frame", FRAME), Port("speed", int, required=False)],
                              outputs=[Port("at", int), Port("arm", call=True)])
    hand = SinkNode("arm", arm.move, answers=True, connect=arm.connect, disconnect=arm.disconnect)
    builder = GraphBuilder().add(setting, camera, controller, hand)
    builder.connect("camera.out", "controller.frame", "latest").connect("setting.y", "controller.speed", "state")
    builder.connect("controller.arm", "arm.x", "call")
    return builder.output("at", "controller.at").compile(DEFAULT)


def main():
    arm = Arm()
    result = Engine(build(arm, [{"target": 5}] * 4), DEFAULT, workers=2).run()
    print(arm.log, result.outputs["at"])
    return arm, result


if __name__ == "__main__":
    main()
