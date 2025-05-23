import json
from typing import Dict
from typing import Optional
from typing import Union

import ddtrace
from ddtrace import Span
from ddtrace import config
from ddtrace.ext import SpanTypes
from ddtrace.internal.logger import get_logger
from ddtrace.llmobs._constants import GEMINI_APM_SPAN_NAME
from ddtrace.llmobs._constants import LANGCHAIN_APM_SPAN_NAME
from ddtrace.llmobs._constants import ML_APP
from ddtrace.llmobs._constants import OPENAI_APM_SPAN_NAME
from ddtrace.llmobs._constants import PARENT_ID_KEY
from ddtrace.llmobs._constants import PROPAGATED_PARENT_ID_KEY
from ddtrace.llmobs._constants import SESSION_ID


log = get_logger(__name__)


def validate_prompt(prompt: dict) -> Dict[str, Union[str, dict]]:
    validated_prompt = {}  # type: Dict[str, Union[str, dict]]
    if not isinstance(prompt, dict):
        raise TypeError("Prompt must be a dictionary")
    variables = prompt.get("variables")
    template = prompt.get("template")
    version = prompt.get("version")
    prompt_id = prompt.get("id")
    if variables is not None:
        if not isinstance(variables, dict):
            raise TypeError("Prompt variables must be a dictionary.")
        if not any(isinstance(k, str) or isinstance(v, str) for k, v in variables.items()):
            raise TypeError("Prompt variable keys and values must be strings.")
        validated_prompt["variables"] = variables
    if template is not None:
        if not isinstance(template, str):
            raise TypeError("Prompt template must be a string")
        validated_prompt["template"] = template
    if version is not None:
        if not isinstance(version, str):
            raise TypeError("Prompt version must be a string.")
        validated_prompt["version"] = version
    if prompt_id is not None:
        if not isinstance(prompt_id, str):
            raise TypeError("Prompt id must be a string.")
        validated_prompt["id"] = prompt_id
    return validated_prompt


class AnnotationContext:
    def __init__(self, _register_annotator, _deregister_annotator):
        self._register_annotator = _register_annotator
        self._deregister_annotator = _deregister_annotator

    def __enter__(self):
        self._register_annotator()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._deregister_annotator()

    async def __aenter__(self):
        self._register_annotator()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._deregister_annotator()


def _get_attr(o: object, attr: str, default: object):
    # Convenience method to get an attribute from an object or dict
    if isinstance(o, dict):
        return o.get(attr, default)
    return getattr(o, attr, default)


def _get_nearest_llmobs_ancestor(span: Span) -> Optional[Span]:
    """Return the nearest LLMObs-type ancestor span of a given span."""
    parent = span._parent
    while parent:
        if parent.span_type == SpanTypes.LLM:
            return parent
        parent = parent._parent
    return None


def _get_llmobs_parent_id(span: Span) -> Optional[str]:
    """Return the span ID of the nearest LLMObs-type span in the span's ancestor tree.
    In priority order: manually set parent ID tag, nearest LLMObs ancestor, local root's propagated parent ID tag.
    """
    if span.get_tag(PARENT_ID_KEY):
        return span.get_tag(PARENT_ID_KEY)
    nearest_llmobs_ancestor = _get_nearest_llmobs_ancestor(span)
    if nearest_llmobs_ancestor:
        return str(nearest_llmobs_ancestor.span_id)
    return span.get_tag(PROPAGATED_PARENT_ID_KEY)


def _get_span_name(span: Span) -> str:
    if span.name in (LANGCHAIN_APM_SPAN_NAME, GEMINI_APM_SPAN_NAME) and span.resource != "":
        return span.resource
    elif span.name == OPENAI_APM_SPAN_NAME and span.resource != "":
        client_name = span.get_tag("openai.request.client") or "OpenAI"
        return "{}.{}".format(client_name, span.resource)
    return span.name


def _get_ml_app(span: Span) -> str:
    """
    Return the ML app name for a given span, by checking the span's nearest LLMObs span ancestor.
    Default to the global config LLMObs ML app name otherwise.
    """
    ml_app = span.get_tag(ML_APP)
    if ml_app:
        return ml_app
    nearest_llmobs_ancestor = _get_nearest_llmobs_ancestor(span)
    if nearest_llmobs_ancestor:
        ml_app = nearest_llmobs_ancestor.get_tag(ML_APP)
    return ml_app or config._llmobs_ml_app or "unknown-ml-app"


def _get_session_id(span: Span) -> Optional[str]:
    """
    Return the session ID for a given span, by checking the span's nearest LLMObs span ancestor.
    Default to the span's trace ID.
    """
    session_id = span.get_tag(SESSION_ID)
    if session_id:
        return session_id
    nearest_llmobs_ancestor = _get_nearest_llmobs_ancestor(span)
    if nearest_llmobs_ancestor:
        session_id = nearest_llmobs_ancestor.get_tag(SESSION_ID)
    return session_id


def _inject_llmobs_parent_id(span_context):
    """Inject the LLMObs parent ID into the span context for reconnecting distributed LLMObs traces."""
    span = ddtrace.tracer.current_span()
    if span is None:
        log.warning("No active span to inject LLMObs parent ID info.")
        return
    if span.context is not span_context:
        log.warning("The current active span and span_context do not match. Not injecting LLMObs parent ID.")
        return

    if span.span_type == SpanTypes.LLM:
        llmobs_parent_id = str(span.span_id)
    else:
        llmobs_parent_id = _get_llmobs_parent_id(span)
    span_context._meta[PROPAGATED_PARENT_ID_KEY] = llmobs_parent_id or "undefined"


def _unserializable_default_repr(obj):
    default_repr = "[Unserializable object: {}]".format(repr(obj))
    log.warning("I/O object is not JSON serializable. Defaulting to placeholder value instead.")
    return default_repr


def safe_json(obj):
    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(obj, skipkeys=True, default=_unserializable_default_repr)
    except Exception:
        log.error("Failed to serialize object to JSON.", exc_info=True)
