# -*- encoding: utf-8 -*-
from functools import partial
import sys
from types import ModuleType  # noqa:F401
import typing  # noqa:F401

from ddtrace.internal._unpatched import _threading as ddtrace_threading
from ddtrace.internal.datadog.profiling import stack_v2
from ddtrace.internal.module import ModuleWatchdog
from ddtrace.internal.utils import get_argument_value
from ddtrace.internal.wrapping import wrap
from ddtrace.settings.profiling import config

from . import _threading


THREAD_LINK = None  # type: typing.Optional[_threading._ThreadLink]


def current_task(loop=None):
    return None


def all_tasks(loop=None):
    return []


def _task_get_name(task):
    return "Task-%d" % id(task)


@ModuleWatchdog.after_module_imported("asyncio")
def _(asyncio):
    # type: (ModuleType) -> None
    global THREAD_LINK

    if hasattr(asyncio, "current_task"):
        globals()["current_task"] = asyncio.current_task
    elif hasattr(asyncio.Task, "current_task"):
        globals()["current_task"] = asyncio.Task.current_task

    if hasattr(asyncio, "all_tasks"):
        globals()["all_tasks"] = asyncio.all_tasks
    elif hasattr(asyncio.Task, "all_tasks"):
        globals()["all_tasks"] = asyncio.Task.all_tasks

    if hasattr(asyncio.Task, "get_name"):
        # `get_name` is only available in Python ≥ 3.8
        globals()["_task_get_name"] = lambda task: task.get_name()

    if THREAD_LINK is None:
        THREAD_LINK = _threading._ThreadLink()

    init_stack_v2 = config.stack.v2_enabled and stack_v2.is_available

    @partial(wrap, sys.modules["asyncio.events"].BaseDefaultEventLoopPolicy.set_event_loop)
    def _(f, args, kwargs):
        loop = get_argument_value(args, kwargs, 1, "loop")
        try:
            if init_stack_v2:
                stack_v2.track_asyncio_loop(typing.cast(int, ddtrace_threading.current_thread().ident), loop)
            return f(*args, **kwargs)
        finally:
            THREAD_LINK.clear_threads(set(sys._current_frames().keys()))
            if loop is not None:
                THREAD_LINK.link_object(loop)

    if init_stack_v2:

        @partial(wrap, sys.modules["asyncio"].tasks._GatheringFuture.__init__)
        def _(f, args, kwargs):
            try:
                return f(*args, **kwargs)
            finally:
                children = get_argument_value(args, kwargs, 1, "children")
                # Pass an invalid positional index for 'loop'
                loop = get_argument_value(args, kwargs, -1, "loop")
                # Link the parent gathering task to the gathered children
                parent = globals()["current_task"](loop)
                for child in children:
                    stack_v2.link_tasks(parent, child)

        if sys.hexversion >= 0x030C0000:
            scheduled_tasks = asyncio.tasks._scheduled_tasks.data
            eager_tasks = asyncio.tasks._eager_tasks
        else:
            scheduled_tasks = asyncio.tasks._all_tasks.data
            eager_tasks = None

        stack_v2.init_asyncio(asyncio.tasks._current_tasks, scheduled_tasks, eager_tasks)  # type: ignore[attr-defined]


def get_event_loop_for_thread(thread_id):
    global THREAD_LINK

    return THREAD_LINK.get_object(thread_id) if THREAD_LINK is not None else None
