from dataclasses import dataclass
from itertools import count
from pathlib import Path
import sys

# from threading import current_thread
from types import FrameType
from types import FunctionType
import typing as t
import uuid

import ddtrace

# from ddtrace import config
from ddtrace._trace.processor import SpanProcessor

# from ddtrace.debugging._debugger import Debugger
from ddtrace.debugging._probe.model import DEFAULT_CAPTURE_LIMITS
from ddtrace.debugging._probe.model import LiteralTemplateSegment
from ddtrace.debugging._probe.model import LogFunctionProbe
from ddtrace.debugging._probe.model import LogLineProbe
from ddtrace.debugging._probe.model import ProbeEvaluateTimingForMethod
from ddtrace.debugging._signal.collector import SignalContext

# from ddtrace.debugging._signal.snapshot import Snapshot
from ddtrace.ext import EXIT_SPAN_TYPES
from ddtrace.internal import compat
from ddtrace.internal import core
from ddtrace.internal.packages import is_user_code
from ddtrace.internal.safety import _isinstance
from ddtrace.internal.wrapping.context import WrappingContext
from ddtrace.settings.code_origin import config as co_config
from ddtrace.span import Span


def frame_stack(frame: FrameType) -> t.Iterator[FrameType]:
    _frame: t.Optional[FrameType] = frame
    while _frame is not None:
        yield _frame
        _frame = _frame.f_back


def wrap_entrypoint(f: t.Callable) -> None:
    if not _isinstance(f, FunctionType):
        return

    _f = t.cast(FunctionType, f)
    if not EntrySpanWrappingContext.is_wrapped(_f):
        EntrySpanWrappingContext(_f).wrap()


@dataclass
class EntrySpanProbe(LogFunctionProbe):
    __span_class__ = "entry"

    @classmethod
    def build(cls, name: str, module: str, function: str) -> "EntrySpanProbe":
        message = f"{cls.__span_class__} span info for {name}, in {module}, in function {function}"

        return cls(
            probe_id=str(uuid.uuid4()),
            version=0,
            tags={},
            module=module,
            func_qname=function,
            evaluate_at=ProbeEvaluateTimingForMethod.ENTER,
            template=message,
            segments=[LiteralTemplateSegment(message)],
            take_snapshot=True,
            limits=DEFAULT_CAPTURE_LIMITS,
            condition=None,
            condition_error_rate=0.0,
            rate=float("inf"),
        )


@dataclass
class ExitSpanProbe(LogLineProbe):
    __span_class__ = "exit"

    @classmethod
    def build(cls, name: str, filename: str, line: int) -> "ExitSpanProbe":
        message = f"{cls.__span_class__} span info for {name}, in {filename}, at {line}"

        return cls(
            probe_id=str(uuid.uuid4()),
            version=0,
            tags={},
            source_file=filename,
            line=line,
            template=message,
            segments=[LiteralTemplateSegment(message)],
            take_snapshot=True,
            limits=DEFAULT_CAPTURE_LIMITS,
            condition=None,
            condition_error_rate=0.0,
            rate=float("inf"),
        )

    @classmethod
    def from_frame(cls, frame: FrameType) -> "ExitSpanProbe":
        code = frame.f_code
        return t.cast(
            ExitSpanProbe,
            cls.build(
                name=code.co_qualname if sys.version_info >= (3, 11) else code.co_name,  # type: ignore[attr-defined]
                filename=str(Path(code.co_filename).resolve()),
                line=frame.f_lineno,
            ),
        )


@dataclass
class EntrySpanLocation:
    name: str
    line: int
    file: str
    module: str
    probe: EntrySpanProbe


class EntrySpanWrappingContext(WrappingContext):
    def __init__(self, f):
        super().__init__(f)

        filename = str(Path(f.__code__.co_filename).resolve())
        name = f.__qualname__
        module = f.__module__
        self.location = EntrySpanLocation(
            name=name,
            line=f.__code__.co_firstlineno,
            file=filename,
            module=module,
            probe=t.cast(EntrySpanProbe, EntrySpanProbe.build(name=name, module=module, function=name)),
        )

    def __enter__(self):
        super().__enter__()

        root = ddtrace.tracer.current_root_span()
        span = ddtrace.tracer.current_span()
        location = self.location
        if root is None or span is None or root.get_tag("_dd.entry_location.file") is not None:
            return self

        # Add tags to the local root
        for s in (root, span):
            s.set_tag_str("_dd.code_origin.type", "entry")

            s.set_tag_str("_dd.code_origin.frames.0.file", location.file)
            s.set_tag_str("_dd.code_origin.frames.0.line", str(location.line))
            s.set_tag_str("_dd.code_origin.frames.0.type", location.module)
            s.set_tag_str("_dd.code_origin.frames.0.method", location.name)

        # TODO[gab]: This will be enabled as part of the live debugger/distributed debugging
        # if ld_config.enabled:
        #     # Create a snapshot
        #     snapshot = Snapshot(
        #         probe=location.probe,
        #         frame=self.__frame__,
        #         thread=current_thread(),
        #         trace_context=root,
        #     )

        #     # Capture on entry
        #     context = Debugger.get_collector().attach(snapshot)

        #     # Correlate the snapshot with the span
        #     root.set_tag_str("_dd.code_origin.frames.0.snapshot_id", snapshot.uuid)
        #     span.set_tag_str("_dd.code_origin.frames.0.snapshot_id", snapshot.uuid)

        #     self.set("context", context)
        #     self.set("start_time", compat.monotonic_ns())

        return self

    def _close_context(self, retval=None, exc_info=(None, None, None)):
        try:
            context: SignalContext = self.get("context")
        except KeyError:
            # No snapshot was created
            return

        context.exit(retval, exc_info, compat.monotonic_ns() - self.get("start_time"))

    def __return__(self, retval):
        self._close_context(retval=retval)
        return super().__return__(retval)

    def __exit__(self, exc_type, exc_value, traceback):
        self._close_context(exc_info=(exc_type, exc_value, traceback))
        super().__exit__(exc_type, exc_value, traceback)


@dataclass
class SpanCodeOriginProcessor(SpanProcessor):
    _instance: t.Optional["SpanCodeOriginProcessor"] = None

    def on_span_start(self, span: Span) -> None:
        if span.span_type not in EXIT_SPAN_TYPES:
            return

        span.set_tag_str("_dd.code_origin.type", "exit")

        # Add call stack information to the exit span. Report only the part of
        # the stack that belongs to user code.
        seq = count(0)
        for frame in frame_stack(sys._getframe(1)):
            code = frame.f_code
            filename = code.co_filename

            if is_user_code(Path(filename)):
                n = next(seq)
                if n >= co_config.max_user_frames:
                    break

                span.set_tag_str(f"_dd.code_origin.frames.{n}.file", filename)
                span.set_tag_str(f"_dd.code_origin.frames.{n}.line", str(code.co_firstlineno))
                # DEV: Without a function object we cannot infer the function
                # and any potential class name.

                # TODO[gab]: This will be enabled as part of the live debugger/distributed debugging
                # if ld_config.enabled:
                #     # Create a snapshot
                #     snapshot = Snapshot(
                #         probe=ExitSpanProbe.from_frame(frame),
                #         frame=frame,
                #         thread=current_thread(),
                #         trace_context=span,
                #     )

                #     # Capture on entry
                #     snapshot.line()

                #     # Collect
                #     Debugger.get_collector().push(snapshot)

                #     # Correlate the snapshot with the span
                #     span.set_tag_str(f"_dd.code_origin.frames.{n}.snapshot_id", snapshot.uuid)

    def on_span_finish(self, span: Span) -> None:
        pass

    @classmethod
    def enable(cls):
        if cls._instance is not None:
            return

        core.on("service_entrypoint.patch", wrap_entrypoint)

        instance = cls._instance = cls()
        instance.register()

    @classmethod
    def disable(cls):
        if cls._instance is None:
            return

        cls._instance.unregister()
        cls._instance = None

        core.reset_listeners("service_entrypoint.patch", wrap_entrypoint)
