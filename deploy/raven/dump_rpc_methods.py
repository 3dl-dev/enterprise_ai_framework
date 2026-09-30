"""Print the JSON-RPC method names Raven's served page registers, as JSON.

Run INSIDE the Raven image (`python /opt/eaf/dump_rpc_methods.py`, or piped to `python -`).
It performs the two registration calls `raven.rpc.bootstrap.build_rpc_stack` makes on the
page's dispatcher (register_system_methods + register_aligned_methods_except_system) on a
fresh Dispatcher and prints `Dispatcher.methods()`: the image's own answer to "what can the
WebUI socket call", not a hand-kept list. The console proxy's write lock
(control-plane/app/raven_write_lock.json) must classify every name this prints; the
registrations fixture its tests read is this output, and tests-live/test_raven_write_lock.py
re-runs it against the live pod to catch drift.
"""
import json

from raven.rpc.approval_broker import ApprovalBroker
from raven.rpc.confirm_broker import ConfirmBroker
from raven.rpc.dispatcher import Dispatcher
from raven.rpc.methods import register_aligned_methods_except_system
from raven.rpc.methods.system import register_system_methods
from raven.rpc.question_broker import QuestionBroker
from raven.rpc.subscriptions import SubscriptionEmitter


async def send_frame(frame):  # the page's broadcast; never called while registering
    return None


# The keyword arguments build_rpc_stack passes. Several registrations are conditional on them
# (turn.* on the emitter, approval.*/confirm.*/clarify.* on their brokers, browser.watch on
# send_frame), so each is given a real object of the kind the served page gives it.
d = Dispatcher()
register_system_methods(d, send_frame=send_frame)
register_aligned_methods_except_system(
    d,
    emitter=SubscriptionEmitter(send_frame=send_frame),
    agent_loop_factory=lambda: None,
    approval_broker=ApprovalBroker(send_frame=send_frame),
    confirm_broker=ConfirmBroker(send_frame=send_frame),
    question_broker=QuestionBroker(send_frame=send_frame),
    scheduler=None,
    turn_ids={},
    direct_targets={},
    build_error=None,
    send_frame=send_frame,
    default_channel="tui",
    ensure_stack=None,
)
print(json.dumps(sorted(d.methods())))
