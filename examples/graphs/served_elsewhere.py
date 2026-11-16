"""A node that lives in another process and stays there: here, a "sensor" that keeps its calibration.

    python examples/graphs/served_elsewhere.py

On the far side the node is served (``serve_node``, or ``interlacing node-serve module:name --listen
HOST:PORT`` from a shell on another machine). In the graph, ``RemoteNode`` stands for it, with the far
node's ports. This example serves it in this same process, so it runs as it stands; across machines only the
address changes, and a token is needed (``--token-env``).
"""
from interlacing import Chain, FunctionNode, Port, RemoteNode, run, serve_node


def sensor():
    """Opens once (a device, a loaded model...), then answers every activation with what it holds."""
    held = {"offset": None, "readings": 0}

    def read(ctx, scale):
        if held["offset"] is None:
            held["offset"] = 0.5                    # the slow, one-time setup
        held["readings"] += 1
        ctx.emit("value", scale * 10 + held["offset"])
        ctx.use(readings=1)

    return FunctionNode("sensor", read, inputs=[Port("scale")], outputs=[Port("value")], context=True)


def report(value):
    return f"measured {value}"


def main():
    with serve_node(sensor()) as server:
        far = RemoteNode("sensor", server.address)
        graph = Chain(far, report)
        first = run(graph, {"in": 1})
        second = run(graph, {"in": 2})
        print(server.address, first.last("out"), second.last("out"), second.used)
        return second


if __name__ == "__main__":
    main()
