"""A node takes back what it sent: a quick estimate first, then the careful one in its place.

    python examples/graphs/corrected_estimate.py

The surveyor sends a quick estimate of a floor's area at once, so that work can start, and measures with care
after. Once those who read the estimate are done with it, it corrects it (``ctx.correct``). Each reader deals
with the correction as its ``on_correction`` says: the price, a program, runs again with the corrected area,
and its quote is put right in place (``rerun``, the default); the writer, an agent, is told that what it read
was corrected and writes again (``tell``, an agent's default). Had nobody read the estimate yet, the
correction would simply have taken its place. The model is a scripted stand-in.
"""
from interlacing import AgentNode, Engine, FunctionNode, GraphBuilder, Port
from interlacing.models import ChatResult, FakeProvider

ROOMS = [(4.0, 3.0), (3.5, 2.8), (2.0, 1.5)]


def survey(ctx, site):
    quick = ctx.emit("area", round(sum(w * d for w, d in site[:2]) * 1.2, 1))     # two rooms, and a guess
    ctx.receive()          # nothing comes to it: this waits until the others have done with the estimate
    ctx.correct(quick, round(sum(w * d for w, d in site), 1))
    return None


def price(area):
    return round(area * 35.0, 2)


def writer(system, messages, tools):
    said = messages[-1]["content"]
    area = said.rsplit("\n", 1)[-1]
    if "corrects" in said:
        return ChatResult(text=f"Revised: the floor is {area} m², measured room by room.")
    return ChatResult(text=f"First look: about {area} m².")


def main():
    surveyor = FunctionNode("survey", survey, inputs=[Port("site")], outputs=[Port("area")], context=True)
    pricing = FunctionNode("price", price, inputs=[Port("area")], outputs=[Port("quote")])
    note = AgentNode("writer", FakeProvider(writer), inputs=[Port("area")], outputs=[Port("note")])
    graph = (GraphBuilder().add(surveyor, pricing, note)
             .connect("survey.area", "price.area").connect("survey.area", "writer.area")
             .input("site", "survey.site").output("quote", "price.quote").output("note", "writer.note").compile())
    result = Engine(graph).run({"site": ROOMS})
    print("quote:", result.outputs["quote"])
    print("notes:", *result.outputs["note"], sep="\n  ")
    return result


if __name__ == "__main__":
    main()
