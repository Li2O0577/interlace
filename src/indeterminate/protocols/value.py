"""``value``: one piece of data per envelope, checked against the sending port's schema, queued in order."""
from ..base import Delivery, Protocol


class Value(Protocol):
    name = "value"
    delivery = Delivery.QUEUE
