import asyncio
import itertools
import logging

from six import string_types
from transitions.core import (Condition, Event, EventData, Machine,
                              MachineError, State, Transition)


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class AsyncState(State):

    @asyncio.coroutine
    def enter(self, event_data):
        logger.debug("%sEntering state %s. Processing callbacks...", event_data.machine.id, self.name)
        for oe in self.on_enter:
            yield from event_data.machine._callback(oe, event_data)
        logger.info("%sEntered state %s", event_data.machine.id, self.name)

    @asyncio.coroutine
    def exit(self, event_data):
        logger.debug("%sExiting state %s. Processing callbacks...", event_data.machine.id, self.name)
        for oe in self.on_exit:
            yield from event_data.machine._callback(oe, event_data)
        logger.info("%sExited state %s", event_data.machine.id, self.name)


class AsyncCondition(Condition):

    @asyncio.coroutine
    def check(self, event_data):
        return (yield from super(AsyncCondition, self).check(event_data))

    @asyncio.coroutine
    def _condition_check(self, statement):
        if asyncio.iscoroutine(statement):
            statement = yield from statement

        return statement == self.target


class AsyncTransition(Transition):

    condition_cls = AsyncCondition

    @asyncio.coroutine
    def execute(self, event_data):

        logger.debug("%sInitiating transition from state %s to state %s...",
                     event_data.machine.id, self.source, self.dest)
        machine = event_data.machine

        for func in self.prepare:
            yield from machine._callback(func, event_data)
            logger.debug("Executed callback '%s' before conditions." % func)

        for c in self.conditions:
            if not (yield from c.check(event_data)):
                logger.debug("%sTransition condition failed: %s() does not " +
                             "return %s. Transition halted.", event_data.machine.id, c.func, c.target)
                return False
        for func in itertools.chain(machine.before_state_change, self.before):
            yield from machine._callback(func, event_data)
            logger.debug("%sExecuted callback '%s' before transition.", event_data.machine.id, func)

        yield from self._change_state(event_data)

        for func in itertools.chain(self.after, machine.after_state_change):
            yield from machine._callback(func, event_data)
            logger.debug("%sExecuted callback '%s' after transition.", event_data.machine.id, func)
        return True

    @asyncio.coroutine
    def _change_state(self, event_data):
        yield from event_data.machine.get_state(self.source).exit(event_data)
        event_data.machine.set_state(self.dest, event_data.model)
        event_data.update(event_data.model)
        yield from event_data.machine.get_state(self.dest).enter(event_data)


class AsyncEvent(Event):

    @asyncio.coroutine
    def trigger(self, *args, **kwargs):
        return (yield from super(AsyncEvent, self).trigger(*args, **kwargs))

    @asyncio.coroutine
    def _trigger(self, model, *args, **kwargs):
        state = self.machine.get_state(model.state)
        if state.name not in self.transitions:
            msg = "%sCan't trigger event %s from state %s!" % (self.machine.id, self.name,
                                                               state.name)
            if state.ignore_invalid_triggers:
                return False
            else:
                raise MachineError(msg)
        event_data = EventData(state, self, self.machine, model, args=args, kwargs=kwargs)

        for func in self.machine.prepare_event:
            yield from self.machine._callback(func, event_data)
            logger.debug("Executed machine preparation callback '%s' before conditions." % func)

        try:
            for t in self.transitions[state.name]:
                event_data.transition = t
                transition_result = yield from t.execute(event_data)

                if transition_result:
                    event_data.result = True
                    break
        except Exception as e:
            event_data.error = e
            raise
        finally:
            for func in self.machine.finalize_event:
                yield from self.machine._callback(func, event_data)
                logger.debug("Executed machine finalize callback '%s'." % func)
        return event_data.result


class AsyncMachine(Machine):

    state_cls = AsyncState
    transition_cls = AsyncTransition
    event_cls = AsyncEvent

    @asyncio.coroutine
    def _callback(self, func, event_data):
        if isinstance(func, string_types):
            func = getattr(event_data.model, func)

        if self.send_event:
            callback = func(event_data)
        else:
            callback = func(*event_data.args, **event_data.kwargs)

        if asyncio.iscoroutine(callback):
            yield from callback

    @asyncio.coroutine
    def _process(self, trigger):
        if not self.has_queue:
            if not self._transition_queue:
                return (yield from trigger())
            else:
                raise MachineError(
                    "Attempt to process events synchronously while transition queue is not empty!"
                )

        self._transition_queue.append(trigger)
        if len(self._transition_queue) > 1:
            return True

        while self._transition_queue:
            try:
                callback = self._transition_queue[0]()

                if asyncio.iscoroutine(callback):
                    yield from callback

                self._transition_queue.popleft()
            except Exception:
                self._transition_queue.clear()
                raise
        return True
