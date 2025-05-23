from copy import deepcopy
import json
import os
import re
import sys
from typing import Any  # noqa:F401
from typing import Callable  # noqa:F401
from typing import Dict  # noqa:F401
from typing import List  # noqa:F401
from typing import Optional  # noqa:F401
from typing import Tuple  # noqa:F401
from typing import Union  # noqa:F401

from ddtrace.internal._file_queue import File_Queue
from ddtrace.internal.serverless import in_azure_function
from ddtrace.internal.serverless import in_gcp_function
from ddtrace.internal.telemetry import telemetry_writer
from ddtrace.internal.utils.cache import cachedmethod
from ddtrace.internal.utils.deprecations import DDTraceDeprecationWarning
from ddtrace.vendor.debtcollector import deprecate

from ..internal import gitmetadata
from ..internal.constants import _PROPAGATION_STYLE_DEFAULT
from ..internal.constants import DEFAULT_BUFFER_SIZE
from ..internal.constants import DEFAULT_MAX_PAYLOAD_SIZE
from ..internal.constants import DEFAULT_PROCESSING_INTERVAL
from ..internal.constants import DEFAULT_REUSE_CONNECTIONS
from ..internal.constants import DEFAULT_SAMPLING_RATE_LIMIT
from ..internal.constants import DEFAULT_TIMEOUT
from ..internal.constants import PROPAGATION_STYLE_ALL
from ..internal.constants import PROPAGATION_STYLE_B3_SINGLE
from ..internal.logger import get_logger
from ..internal.schema import DEFAULT_SPAN_SERVICE_NAME
from ..internal.serverless import in_aws_lambda
from ..internal.utils.formats import asbool
from ..internal.utils.formats import parse_tags_str
from ..pin import Pin
from ._core import get_config as _get_config
from ._otel_remapper import otel_remapping as _otel_remapping
from .http import HttpConfig
from .integration import IntegrationConfig


if sys.version_info >= (3, 8):
    from typing import Literal  # noqa:F401
else:
    from typing_extensions import Literal


log = get_logger(__name__)


DD_TRACE_OBFUSCATION_QUERY_STRING_REGEXP_DEFAULT = (
    r"(?ix)"
    r"(?:"  # JSON-ish leading quote
    r'(?:"|%22)?'
    r")"
    r"(?:"  # common keys"
    r"(?:old[-_]?|new[-_]?)?p(?:ass)?w(?:or)?d(?:1|2)?"  # pw, password variants
    r"|pass(?:[-_]?phrase)?"  # pass, passphrase variants
    r"|secret"
    r"|(?:"  # key, key_id variants
    r"api[-_]?"
    r"|private[-_]?"
    r"|public[-_]?"
    r"|access[-_]?"
    r"|secret[-_]?"
    r"|app(?:lica"
    r"tion)?[-_]?"
    r")key(?:[-_]?id)?"
    r"|token"
    r"|consumer[-_]?(?:id|key|secret)"
    r"|sign(?:ed|ature)?"
    r"|auth(?:entication|orization)?"
    r")"
    r"(?:"
    # '=' query string separator, plus value til next '&' separator
    r"(?:\s|%20)*(?:=|%3D)[^&]+"
    # JSON-ish '": "somevalue"', key being handled with case above, without the opening '"'
    r'|(?:"|%22)'  # closing '"' at end of key
    r"(?:\s|%20)*(?::|%3A)(?:\s|%20)*"  # ':' key-value separator, with surrounding spaces
    r'(?:"|%22)'  # opening '"' at start of value
    r'(?:%2[^2]|%[^2]|[^"%])+'  # value
    r'(?:"|%22)'  # closing '"' at end of value
    r")"
    r"|(?:"  # other common secret values
    r" bearer(?:\s|%20)+[a-z0-9._\-]+"
    r"|token(?::|%3A)[a-z0-9]{13}"
    r"|gh[opsu]_[0-9a-zA-Z]{36}"
    r"|ey[I-L](?:[\w=-]|%3D)+\.ey[I-L](?:[\w=-]|%3D)+(?:\.(?:[\w.+/=-]|%3D|%2F|%2B)+)?"
    r"|-{5}BEGIN(?:[a-z\s]|%20)+PRIVATE(?:\s|%20)KEY-{5}[^\-]+-{5}END"
    r"(?:[a-z\s]|%20)+PRIVATE(?:\s|%20)KEY(?:-{5})?(?:\n|%0A)?"
    r"|(?:ssh-(?:rsa|dss)|ecdsa-[a-z0-9]+-[a-z0-9]+)(?:\s|%20|%09)+(?:[a-z0-9/.+]"
    r"|%2F|%5C|%2B){100,}(?:=|%3D)*(?:(?:\s|%20|%09)+[a-z0-9._-]+)?"
    r")"
)


def _parse_propagation_styles(styles_str):
    # type: (str) -> Optional[List[str]]
    """Helper to parse http propagation extract/inject styles via env variables.

    The expected format is::

        <style>[,<style>...]


    The allowed values are:

    - "datadog"
    - "b3multi"
    - "b3" (formerly 'b3 single header')
    - "b3 single header (deprecated for 'b3')"
    - "tracecontext"
    - "baggage"
    - "none"


    The default value is ``"datadog,tracecontext,baggage"``.


    Examples::

        # Extract and inject b3 headers:
        DD_TRACE_PROPAGATION_STYLE="b3multi"

        # Disable header propagation:
        DD_TRACE_PROPAGATION_STYLE="none"

        # Extract trace context from "x-datadog-*" or "x-b3-*" headers from upstream headers
        DD_TRACE_PROPAGATION_STYLE_EXTRACT="datadog,b3multi"

        # Inject the "b3: *" header into downstream requests headers
        DD_TRACE_PROPAGATION_STYLE_INJECT="b3"
    """
    styles = []
    for style in styles_str.split(","):
        style = style.strip().lower()
        if style == "b3 single header":
            deprecate(
                'Using DD_TRACE_PROPAGATION_STYLE="b3 single header" is deprecated',
                message="Please use 'DD_TRACE_PROPAGATION_STYLE=\"b3\"' instead",
                removal_version="3.0.0",
                category=DDTraceDeprecationWarning,
            )
            style = PROPAGATION_STYLE_B3_SINGLE
        if not style:
            continue
        if style not in PROPAGATION_STYLE_ALL:
            log.warning("Unknown DD_TRACE_PROPAGATION_STYLE: {!r}, allowed values are %r", style, PROPAGATION_STYLE_ALL)
            continue
        styles.append(style)
    return styles


# Borrowed from: https://stackoverflow.com/questions/20656135/python-deep-merge-dictionary-data#20666342
def _deepmerge(source, destination):
    """
    Merge the first provided ``dict`` into the second.

    :param dict source: The ``dict`` to merge into ``destination``
    :param dict destination: The ``dict`` that should get updated
    :rtype: dict
    :returns: ``destination`` modified
    """
    for key, value in source.items():
        if isinstance(value, dict):
            # get node or create one
            node = destination.setdefault(key, {})
            _deepmerge(value, node)
        else:
            destination[key] = value

    return destination


def get_error_ranges(error_range_str):
    # type: (str) -> List[Tuple[int, int]]
    error_ranges = []
    error_range_str = error_range_str.strip()
    error_ranges_str = error_range_str.split(",")
    for error_range in error_ranges_str:
        values = error_range.split("-")
        try:
            # Note: mypy does not like variable type changing
            values = [int(v) for v in values]  # type: ignore[misc]
        except ValueError:
            log.exception("Error status codes was not a number %s", values)
            continue
        error_range = (min(values), max(values))  # type: ignore[assignment]
        error_ranges.append(error_range)
    return error_ranges  # type: ignore[return-value]


_ConfigSource = Literal["default", "env_var", "code", "remote_config"]
_JSONType = Union[None, int, float, str, bool, List["_JSONType"], Dict[str, "_JSONType"]]


class _ConfigItem:
    """Configuration item that tracks the value of a setting, and where it came from."""

    def __init__(self, default, envs):
        # type: (Union[_JSONType, Callable[[], _JSONType]], List[Tuple[str, Callable[[str], Any]]]) -> None
        # _ConfigItem._name is only used in __repr__ and instrumentation telemetry
        self._name = envs[0][0]
        self._env_value: _JSONType = None
        self._code_value: _JSONType = None
        self._rc_value: _JSONType = None
        if callable(default):
            self._default_value = default()
        else:
            self._default_value = default
        self._envs = envs
        for env_var, parser in envs:
            if env_var in os.environ:
                self._env_value = parser(os.environ[env_var])
                break
        telemetry_writer.add_configuration(self._name, self.value(), self.source())

    def set_value_source(self, value: Any, source: _ConfigSource) -> None:
        if source == "code":
            self._code_value = value
        elif source == "remote_config":
            self._rc_value = value
        else:
            log.warning("Invalid source: %s", source)

    def set_code(self, value: _JSONType) -> None:
        self._code_value = value

    def unset_rc(self) -> None:
        self._rc_value = None

    def value(self) -> _JSONType:
        if self._rc_value is not None:
            return self._rc_value
        if self._code_value is not None:
            return self._code_value
        if self._env_value is not None:
            return self._env_value
        return self._default_value

    def source(self) -> _ConfigSource:
        if self._rc_value is not None:
            return "remote_config"
        if self._code_value is not None:
            return "code"
        if self._env_value is not None:
            return "env_var"
        return "default"

    def __repr__(self):
        return "<{} name={} default={} env_value={} user_value={} remote_config_value={}>".format(
            self.__class__.__name__,
            self._name,
            self._default_value,
            self._env_value,
            self._code_value,
            self._rc_value,
        )


def _parse_global_tags(s):
    # cleanup DD_TAGS, because values will be inserted back in the optimal way (via _dd.git.* tags)
    return gitmetadata.clean_tags(parse_tags_str(s))


def _default_config() -> Dict[str, _ConfigItem]:
    return {
        "_trace_sample_rate": _ConfigItem(
            default=1.0,
            envs=[("DD_TRACE_SAMPLE_RATE", float)],
        ),
        "_trace_sampling_rules": _ConfigItem(
            default=lambda: "",
            envs=[("DD_TRACE_SAMPLING_RULES", str)],
        ),
        "_logs_injection": _ConfigItem(
            default=False,
            envs=[("DD_LOGS_INJECTION", asbool)],
        ),
        "_trace_http_header_tags": _ConfigItem(
            default=lambda: {},
            envs=[("DD_TRACE_HEADER_TAGS", parse_tags_str)],
        ),
        "tags": _ConfigItem(
            default=lambda: {},
            envs=[("DD_TAGS", _parse_global_tags)],
        ),
        "_tracing_enabled": _ConfigItem(
            default=True,
            envs=[("DD_TRACE_ENABLED", asbool)],
        ),
        "_profiling_enabled": _ConfigItem(
            default=False,
            envs=[("DD_PROFILING_ENABLED", asbool)],
        ),
        "_asm_enabled": _ConfigItem(
            default=False,
            envs=[("DD_APPSEC_ENABLED", asbool)],
        ),
        "_sca_enabled": _ConfigItem(
            default=None,
            envs=[("DD_APPSEC_SCA_ENABLED", asbool)],
        ),
        "_dsm_enabled": _ConfigItem(
            default=False,
            envs=[("DD_DATA_STREAMS_ENABLED", asbool)],
        ),
    }


class Config(object):
    """Configuration object that exposes an API to set and retrieve
    global settings for each integration. All integrations must use
    this instance to register their defaults, so that they're public
    available and can be updated by users.
    """

    # Maps deprecated configuration attributes to their corresponding environment variable and
    # internalized attribute name
    _DEPRECATED_ATTRS = {
        "http_tag_query_string": ("_http_tag_query_string", "DD_TRACE_HTTP_CLIENT_TAG_QUERY_STRING"),
        "trace_http_header_tags": ("_trace_http_header_tags", "DD_TRACE_HEADER_TAGS"),
        "report_hostname": ("_report_hostname", "DD_TRACE_REPORT_HOSTNAME"),
        "health_metrics_enabled": ("_health_metrics_enabled", "DD_TRACE_HEALTH_METRICS_ENABLED"),
        "analytics_enabled": ("_analytics_enabled", "DD_TRACE_ANALYTICS_ENABLED"),
        "client_ip_header": ("_client_ip_header", "DD_TRACE_CLIENT_IP_HEADER"),
        "retrieve_client_ip": ("_retrieve_client_ip", "DD_TRACE_CLIENT_IP_ENABLED"),
        "propagation_http_baggage_enabled": (
            "_propagation_http_baggage_enabled",
            "DD_TRACE_PROPAGATION_HTTP_BAGGAGE_ENABLED",
        ),
        "global_query_string_obfuscation_disabled": (
            "_global_query_string_obfuscation_disabled",
            'DD_TRACE_OBFUSCATION_QUERY_STRING_REGEXP=""',
        ),
        "trace_methods": ("_trace_methods", "DD_TRACE_METHODS"),
        "ci_visibility_log_level": ("_ci_visibility_log_level", "DD_CIVISIBILITY_LOG_LEVEL"),
        "test_session_name": ("_test_session_name", "DD_TEST_SESSION_NAME"),
        "logs_injection": ("_logs_injection", "DD_LOGS_INJECTION"),
    }

    class _HTTPServerConfig(object):
        _error_statuses = _get_config("DD_TRACE_HTTP_SERVER_ERROR_STATUSES", "500-599")  # type: str
        _error_ranges = get_error_ranges(_error_statuses)  # type: List[Tuple[int, int]]

        @property
        def error_statuses(self):
            # type: () -> str
            return self._error_statuses

        @error_statuses.setter
        def error_statuses(self, value):
            # type: (str) -> None
            self._error_statuses = value
            self._error_ranges = get_error_ranges(value)
            # Mypy can't catch cached method's invalidate()
            self.is_error_code.invalidate()  # type: ignore[attr-defined]

        @property
        def error_ranges(self):
            # type: () -> List[Tuple[int, int]]
            return self._error_ranges

        @cachedmethod()
        def is_error_code(self, status_code):
            # type: (int) -> bool
            """Returns a boolean representing whether or not a status code is an error code.
            Error status codes by default are 500-599.
            You may also enable custom error codes::

                from ddtrace import config
                config.http_server.error_statuses = '401-404,419'

            Ranges and singular error codes are permitted and can be separated using commas.
            """
            for error_range in self.error_ranges:
                if error_range[0] <= status_code <= error_range[1]:
                    return True
            return False

    def __init__(self):
        # Must map Otel configurations to Datadog configurations before creating the config object.
        _otel_remapping()
        # Must come before _integration_configs due to __setattr__
        self._config = _default_config()

        sample_rate = os.getenv("DD_TRACE_SAMPLE_RATE")
        if sample_rate is not None:
            deprecate(
                "DD_TRACE_SAMPLE_RATE is deprecated",
                message="Please use DD_TRACE_SAMPLING_RULES instead.",
                removal_version="3.0.0",
            )

        # Use a dict as underlying storing mechanism for integration configs
        self._integration_configs = {}

        self._debug_mode = _get_config("DD_TRACE_DEBUG", False, asbool)
        self._startup_logs_enabled = _get_config("DD_TRACE_STARTUP_LOGS", False, asbool)

        rate_limit = os.getenv("DD_TRACE_RATE_LIMIT")
        if rate_limit is not None and self._trace_sampling_rules in ("", "[]"):
            # This warning will be logged when DD_TRACE_SAMPLE_RATE is set. This is intentional.
            # Even though DD_TRACE_SAMPLE_RATE is treated as a global trace sampling rule, this configuration
            # is deprecated. We should always encourage users to set DD_TRACE_SAMPLING_RULES instead.
            log.warning(
                "DD_TRACE_RATE_LIMIT is set to %s and DD_TRACE_SAMPLING_RULES is not set. "
                "Tracer rate limitting is only applied to spans that match tracer sampling rules. "
                "All other spans will be rate limited by the Datadog Agent via DD_APM_MAX_TPS.",
                rate_limit,
            )
        self._trace_rate_limit = _get_config("DD_TRACE_RATE_LIMIT", DEFAULT_SAMPLING_RATE_LIMIT, int)
        self._partial_flush_enabled = _get_config("DD_TRACE_PARTIAL_FLUSH_ENABLED", True, asbool)
        self._partial_flush_min_spans = _get_config("DD_TRACE_PARTIAL_FLUSH_MIN_SPANS", 300, int)

        self.http = HttpConfig(header_tags=self._trace_http_header_tags)
        self._remote_config_enabled = _get_config("DD_REMOTE_CONFIGURATION_ENABLED", True, asbool)
        self._remote_config_poll_interval = _get_config(
            ["DD_REMOTE_CONFIG_POLL_INTERVAL_SECONDS", "DD_REMOTECONFIG_POLL_SECONDS"], 5.0, float
        )
        self._trace_api = _get_config("DD_TRACE_API_VERSION")
        if self._trace_api == "v0.3":
            deprecate(
                "DD_TRACE_API_VERSION=v0.3 is deprecated",
                message="Traces will be submitted to the v0.4/traces agent endpoint instead.",
                removal_version="3.0.0",
                category=DDTraceDeprecationWarning,
            )
            self._trace_api = "v0.4"
        self._trace_writer_buffer_size = _get_config("DD_TRACE_WRITER_BUFFER_SIZE_BYTES", DEFAULT_BUFFER_SIZE, int)
        self._trace_writer_payload_size = _get_config(
            "DD_TRACE_WRITER_MAX_PAYLOAD_SIZE_BYTES", DEFAULT_MAX_PAYLOAD_SIZE, int
        )
        self._trace_writer_interval_seconds = _get_config(
            "DD_TRACE_WRITER_INTERVAL_SECONDS", DEFAULT_PROCESSING_INTERVAL, float
        )
        self._trace_writer_connection_reuse = _get_config(
            "DD_TRACE_WRITER_REUSE_CONNECTIONS", DEFAULT_REUSE_CONNECTIONS, asbool
        )
        self._trace_writer_log_err_payload = _get_config("_DD_TRACE_WRITER_LOG_ERROR_PAYLOADS", False, asbool)

        self._trace_agent_hostname = _get_config(["DD_AGENT_HOST", "DD_TRACE_AGENT_HOSTNAME"])
        self._trace_agent_port = _get_config(["DD_AGENT_PORT", "DD_TRACE_AGENT_PORT"])
        self._trace_agent_url = _get_config("DD_TRACE_AGENT_URL")

        self._stats_agent_hostname = _get_config(["DD_AGENT_HOST", "DD_DOGSTATSD_HOST"])
        self._stats_agent_port = _get_config("DD_DOGSTATSD_PORT")
        self._stats_agent_url = _get_config("DD_DOGSTATSD_URL")
        self._agent_timeout_seconds = _get_config("DD_TRACE_AGENT_TIMEOUT_SECONDS", DEFAULT_TIMEOUT, float)

        self._span_traceback_max_size = _get_config("DD_TRACE_SPAN_TRACEBACK_MAX_SIZE", 30, int)

        # Master switch for turning on and off trace search by default
        # this weird invocation of getenv is meant to read the DD_ANALYTICS_ENABLED
        # legacy environment variable. It should be removed in the future
        self._analytics_enabled = _get_config(["DD_TRACE_ANALYTICS_ENABLED", "DD_ANALYTICS_ENABLED"], False, asbool)
        if self._analytics_enabled:
            deprecate(
                "Datadog App Analytics is deprecated and will be removed in a future version. "
                "App Analytics can be enabled via DD_TRACE_ANALYTICS_ENABLED and DD_ANALYTICS_ENABLED "
                "environment variables and ddtrace.config.analytics_enabled configuration. "
                "These configurations will also be removed.",
                category=DDTraceDeprecationWarning,
            )
        self._client_ip_header = _get_config("DD_TRACE_CLIENT_IP_HEADER")
        self._retrieve_client_ip = _get_config("DD_TRACE_CLIENT_IP_ENABLED", False, asbool)

        self._propagation_http_baggage_enabled = _get_config("DD_TRACE_PROPAGATION_HTTP_BAGGAGE_ENABLED", False, asbool)

        self.env = _get_config("DD_ENV", self.tags.get("env"))
        self.service = _get_config("DD_SERVICE", self.tags.get("service", DEFAULT_SPAN_SERVICE_NAME))

        if self.service is None and in_gcp_function():
            self.service = _get_config(["K_SERVICE", "FUNCTION_NAME"], DEFAULT_SPAN_SERVICE_NAME)
        if self.service is None and in_azure_function():
            self.service = _get_config("WEBSITE_SITE_NAME", DEFAULT_SPAN_SERVICE_NAME)

        self._extra_services = set()
        self._extra_services_queue = None if in_aws_lambda() or not self._remote_config_enabled else File_Queue()
        self.version = _get_config("DD_VERSION", self.tags.get("version"))
        self.http_server = self._HTTPServerConfig()

        self._unparsed_service_mapping = _get_config("DD_SERVICE_MAPPING", "")
        self.service_mapping = parse_tags_str(self._unparsed_service_mapping)

        # The service tag corresponds to span.service and should not be
        # included in the global tags.
        if self.service and "service" in self.tags:
            del self.tags["service"]

        # The version tag should not be included on all spans.
        if self.version and "version" in self.tags:
            del self.tags["version"]

        self._report_hostname = _get_config("DD_TRACE_REPORT_HOSTNAME", False, asbool)

        self._health_metrics_enabled = _get_config("DD_TRACE_HEALTH_METRICS_ENABLED", False, asbool)

        self._telemetry_enabled = _get_config("DD_INSTRUMENTATION_TELEMETRY_ENABLED", True, asbool)
        self._telemetry_heartbeat_interval = _get_config("DD_TELEMETRY_HEARTBEAT_INTERVAL", 60, float)
        self._telemetry_dependency_collection = _get_config("DD_TELEMETRY_DEPENDENCY_COLLECTION_ENABLED", True, asbool)

        self._runtime_metrics_enabled = _get_config("DD_RUNTIME_METRICS_ENABLED", False, asbool)

        self._128_bit_trace_id_enabled = _get_config("DD_TRACE_128_BIT_TRACEID_GENERATION_ENABLED", True, asbool)

        self._128_bit_trace_id_logging_enabled = _get_config("DD_TRACE_128_BIT_TRACEID_LOGGING_ENABLED", False, asbool)
        if self._128_bit_trace_id_logging_enabled:
            deprecate(
                "Using DD_TRACE_128_BIT_TRACEID_LOGGING_ENABLED is deprecated.",
                message="Log injection format is now configured automatically.",
                removal_version="3.0.0",
                category=DDTraceDeprecationWarning,
            )

        self._sampling_rules = _get_config("DD_SPAN_SAMPLING_RULES")
        self._sampling_rules_file = _get_config("DD_SPAN_SAMPLING_RULES_FILE")

        # Propagation styles
        # DD_TRACE_PROPAGATION_STYLE_EXTRACT and DD_TRACE_PROPAGATION_STYLE_INJECT
        #  take precedence over DD_TRACE_PROPAGATION_STYLE
        self._propagation_style_extract = _parse_propagation_styles(
            _get_config(
                ["DD_TRACE_PROPAGATION_STYLE_EXTRACT", "DD_TRACE_PROPAGATION_STYLE"], _PROPAGATION_STYLE_DEFAULT
            )
        )
        self._propagation_style_inject = _parse_propagation_styles(
            _get_config(["DD_TRACE_PROPAGATION_STYLE_INJECT", "DD_TRACE_PROPAGATION_STYLE"], _PROPAGATION_STYLE_DEFAULT)
        )

        self._propagation_extract_first = _get_config("DD_TRACE_PROPAGATION_EXTRACT_FIRST", False, asbool)

        # Datadog tracer tags propagation
        x_datadog_tags_max_length = _get_config("DD_TRACE_X_DATADOG_TAGS_MAX_LENGTH", 512, int)
        if x_datadog_tags_max_length < 0:
            log.warning(
                (
                    "Invalid value %r provided for DD_TRACE_X_DATADOG_TAGS_MAX_LENGTH, "
                    "only non-negative values allowed"
                ),
                x_datadog_tags_max_length,
            )
            x_datadog_tags_max_length = 0
        self._x_datadog_tags_max_length = x_datadog_tags_max_length
        self._x_datadog_tags_enabled = x_datadog_tags_max_length > 0

        # Raise certain errors only if in testing raise mode to prevent crashing in production with non-critical errors
        self._raise = _get_config("DD_TESTING_RAISE", False, asbool)

        trace_compute_stats_default = in_gcp_function() or in_azure_function()
        self._trace_compute_stats = _get_config(
            ["DD_TRACE_COMPUTE_STATS", "DD_TRACE_STATS_COMPUTATION_ENABLED"], trace_compute_stats_default, asbool
        )
        self._data_streams_enabled = _get_config("DD_DATA_STREAMS_ENABLED", False, asbool)

        legacy_client_tag_enabled = _get_config("DD_HTTP_CLIENT_TAG_QUERY_STRING")
        if legacy_client_tag_enabled is None:
            self._http_client_tag_query_string = _get_config("DD_TRACE_HTTP_CLIENT_TAG_QUERY_STRING", "true")
        else:
            deprecate(
                "DD_HTTP_CLIENT_TAG_QUERY_STRING is deprecated",
                message="Please use DD_TRACE_HTTP_CLIENT_TAG_QUERY_STRING instead.",
                removal_version="3.0.0",
                category=DDTraceDeprecationWarning,
            )
            self._http_client_tag_query_string = legacy_client_tag_enabled.lower()

        dd_trace_obfuscation_query_string_regexp = _get_config(
            "DD_TRACE_OBFUSCATION_QUERY_STRING_REGEXP", DD_TRACE_OBFUSCATION_QUERY_STRING_REGEXP_DEFAULT
        )
        self._global_query_string_obfuscation_disabled = dd_trace_obfuscation_query_string_regexp == ""
        self._obfuscation_query_string_pattern = None
        self._http_tag_query_string = True  # Default behaviour of query string tagging in http.url
        try:
            self._obfuscation_query_string_pattern = re.compile(
                dd_trace_obfuscation_query_string_regexp.encode("ascii")
            )
        except Exception:
            log.warning("Invalid obfuscation pattern, disabling query string tracing", exc_info=True)
            self._http_tag_query_string = False  # Disable query string tagging if malformed obfuscation pattern

        self._ci_visibility_agentless_enabled = _get_config("DD_CIVISIBILITY_AGENTLESS_ENABLED", False, asbool)
        self._ci_visibility_agentless_url = _get_config("DD_CIVISIBILITY_AGENTLESS_URL", "")
        self._ci_visibility_intelligent_testrunner_enabled = _get_config("DD_CIVISIBILITY_ITR_ENABLED", True, asbool)
        self._ci_visibility_log_level = _get_config("DD_CIVISIBILITY_LOG_LEVEL", "info")
        self._test_session_name = _get_config("DD_TEST_SESSION_NAME")
        self._test_visibility_early_flake_detection_enabled = _get_config(
            "DD_CIVISIBILITY_EARLY_FLAKE_DETECTION_ENABLED", True, asbool
        )
        self._otel_enabled = _get_config("DD_TRACE_OTEL_ENABLED", False, asbool)
        if self._otel_enabled:
            # Replaces the default otel api runtime context with DDRuntimeContext
            # https://github.com/open-telemetry/opentelemetry-python/blob/v1.16.0/opentelemetry-api/src/opentelemetry/context/__init__.py#L53
            os.environ["OTEL_PYTHON_CONTEXT"] = "ddcontextvars_context"
        self._subscriptions = []  # type: List[Tuple[List[str], Callable[[Config, List[str]], None]]]
        self._span_aggregator_rlock = _get_config("DD_TRACE_SPAN_AGGREGATOR_RLOCK", True, asbool)
        if self._span_aggregator_rlock is False:
            deprecate(
                "DD_TRACE_SPAN_AGGREGATOR_RLOCK is deprecated",
                message="Soon the ddtrace library will only support using threading.Rlock to "
                "aggregate and encode span data. If you need to disable the re-entrant lock and "
                "revert to using threading.Lock, please contact Datadog support.",
                removal_version="3.0.0",
            )

        self._trace_methods = _get_config("DD_TRACE_METHODS")

        self._telemetry_install_id = _get_config("DD_INSTRUMENTATION_INSTALL_ID")
        self._telemetry_install_type = _get_config("DD_INSTRUMENTATION_INSTALL_TYPE")
        self._telemetry_install_time = _get_config("DD_INSTRUMENTATION_INSTALL_TYPE")

        self._dd_api_key = _get_config("DD_API_KEY")
        self._dd_site = _get_config("DD_SITE", "datadoghq.com")

        self._llmobs_enabled = _get_config("DD_LLMOBS_ENABLED", False, asbool)
        self._llmobs_sample_rate = _get_config("DD_LLMOBS_SAMPLE_RATE", 1.0, float)
        self._llmobs_ml_app = _get_config("DD_LLMOBS_ML_APP")
        self._llmobs_agentless_enabled = _get_config("DD_LLMOBS_AGENTLESS_ENABLED", False, asbool)

        self._inject_force = _get_config("DD_INJECT_FORCE", False, asbool)
        self._lib_was_injected = False
        self._inject_was_attempted = _get_config("_DD_INJECT_WAS_ATTEMPTED", False, asbool)

    def __getattr__(self, name) -> Any:
        if name in self._DEPRECATED_ATTRS:
            new_name, env_var = self._DEPRECATED_ATTRS[name]
            deprecate(
                f"ddtrace.config.{name} is deprecated",
                message=f"Use the environment variable {env_var} instead. "
                "This variable must be set before importing ddtrace.",
                removal_version="3.0.0",
                category=DDTraceDeprecationWarning,
            )
            if name == new_name:
                raise RuntimeError(
                    f"Circular mapping detected: deprecated attribute {name} "
                    f"in {self._DEPRECATED_ATTRS} maps to a deprecated attribute {new_name}"
                )
            return getattr(self, new_name)

        if name in self._config:
            return self._config[name].value()

        if name not in self._integration_configs:
            self._integration_configs[name] = IntegrationConfig(self, name)

        return self._integration_configs[name]

    def _add_extra_service(self, service_name: str) -> None:
        if self._extra_services_queue is None:
            return
        if service_name != self.service:
            self._extra_services_queue.put(service_name)

    def _get_extra_services(self):
        # type: () -> set[str]
        if self._extra_services_queue is None:
            return set()
        self._extra_services.update(self._extra_services_queue.get_all())
        while len(self._extra_services) > 64:
            self._extra_services.pop()
        return self._extra_services

    def get_from(self, obj):
        """Retrieves the configuration for the given object.
        Any object that has an attached `Pin` must have a configuration
        and if a wrong object is given, an empty `dict` is returned
        for safety reasons.
        """
        pin = Pin.get_from(obj)
        if pin is None:
            log.debug("No configuration found for %s", obj)
            return {}

        return pin._config

    def _add(self, integration, settings, merge=True):
        """Internal API that registers an integration with given default
        settings.

        :param str integration: The integration name (i.e. `requests`)
        :param dict settings: A dictionary that contains integration settings;
            to preserve immutability of these values, the dictionary is copied
            since it contains integration defaults.
        :param bool merge: Whether to merge any existing settings with those provided,
            or if we should overwrite the settings with those provided;
            Note: when merging existing settings take precedence.
        """
        # DEV: Use `getattr()` to call our `__getattr__` helper
        existing = getattr(self, integration)
        settings = deepcopy(settings)

        if merge:
            # DEV: This may appear backwards keeping `existing` as the "source" and `settings` as
            #   the "destination", but we do not want to let `_add(..., merge=True)` overwrite any
            #   existing settings
            #
            # >>> config.requests['split_by_domain'] = True
            # >>> config._add('requests', dict(split_by_domain=False))
            # >>> config.requests['split_by_domain']
            # True
            self._integration_configs[integration] = IntegrationConfig(
                self, integration, _deepmerge(existing, settings)
            )
        else:
            self._integration_configs[integration] = IntegrationConfig(self, integration, settings)

    def trace_headers(self, whitelist):
        """
        Registers a set of headers to be traced at global level or integration level.
        :param whitelist: the case-insensitive list of traced headers
        :type whitelist: list of str or str
        :return: self
        :rtype: HttpConfig
        """
        self.http.trace_headers(whitelist)
        return self

    def header_is_traced(self, header_name):
        # type: (str) -> bool
        """
        Returns whether or not the current header should be traced.
        :param header_name: the header name
        :type header_name: str
        :rtype: bool
        """
        return self.http.header_is_traced(header_name)

    @cachedmethod()
    def _header_tag_name(self, header_name):
        # type: (str) -> Optional[str]
        return self.http._header_tag_name(header_name)

    def _get_service(self, default=None):
        """
        Returns the globally configured service or the default if none is configured.

        :param default: the default service to use if none is configured or
            found.
        :type default: str
        :rtype: str|None
        """
        # TODO: This method can be replaced with `config.service`.
        return self.service if self.service is not None else default

    def __repr__(self):
        cls = self.__class__
        integrations = ", ".join(self._integration_config.keys())
        return "{}.{}({})".format(cls.__module__, cls.__name__, integrations)

    def _subscribe(self, items, handler):
        # type: (List[str], Callable[[Config, List[str]], None]) -> None
        self._subscriptions.append((items, handler))

    def _notify_subscribers(self, changed_items):
        # type: (List[str]) -> None
        for sub_items, sub_handler in self._subscriptions:
            sub_updated_items = [i for i in changed_items if i in sub_items]
            if sub_updated_items:
                sub_handler(self, sub_updated_items)

    def __setattr__(self, key, value):
        # type: (str, Any) -> None
        if key == "_config":
            return super(self.__class__, self).__setattr__(key, value)
        elif key in self._config:
            self._set_config_items([(key, value, "code")])
            return None
        else:
            if key in self._DEPRECATED_ATTRS:
                # replace deprecated attribute name with the new name
                new_key, env_var = self._DEPRECATED_ATTRS[key]
                deprecate(
                    f"ddtrace.config.{key} is deprecated",
                    message=f"Use the environment variable {env_var} instead. "
                    "This variable must be set before importing ddtrace.",
                    removal_version="3.0.0",
                    category=DDTraceDeprecationWarning,
                )
                key = new_key
            return super(self.__class__, self).__setattr__(key, value)

    def _set_config_items(self, items):
        # type: (List[Tuple[str, Any, _ConfigSource]]) -> None
        item_names = []
        for key, value, origin in items:
            item_names.append(key)
            item = self._config[key]
            item.set_value_source(value, origin)
            if self._telemetry_enabled:
                telemetry_writer.add_configuration(item._name, item.value(), item.source())
        self._notify_subscribers(item_names)

    def _reset(self):
        # type: () -> None
        self._config = _default_config()

    def _get_source(self, item):
        # type: (str) -> str
        return self._config[item].source()

    def _remoteconfigPubSub(self):
        from ddtrace.internal.remoteconfig._connectors import PublisherSubscriberConnector
        from ddtrace.internal.remoteconfig._publishers import RemoteConfigPublisher
        from ddtrace.internal.remoteconfig._pubsub import PubSub
        from ddtrace.internal.remoteconfig._pubsub import RemoteConfigSubscriber

        class _GlobalConfigPubSub(PubSub):
            __publisher_class__ = RemoteConfigPublisher
            __subscriber_class__ = RemoteConfigSubscriber
            __shared_data__ = PublisherSubscriberConnector()

            def __init__(self, callback):
                self._publisher = self.__publisher_class__(self.__shared_data__, None)
                self._subscriber = self.__subscriber_class__(self.__shared_data__, callback, "GlobalConfig")

        return _GlobalConfigPubSub

    def _handle_remoteconfig(self, data, test_tracer=None):
        # type: (Any, Any) -> None
        if not isinstance(data, dict) or (isinstance(data, dict) and "config" not in data):
            log.warning("unexpected RC payload %r", data)
            return
        if len(data["config"]) == 0:
            log.warning("unexpected number of RC payloads %r", data)
            return

        # Check if 'lib_config' is a key in the dictionary since other items can be sent in the payload
        config = None
        for config_item in data["config"]:
            if isinstance(config_item, Dict):
                if "lib_config" in config_item:
                    config = config_item
                    break

        # If no data is submitted then the RC config has been deleted. Revert the settings.
        base_rc_config = {n: None for n in self._config}

        if config and "lib_config" in config:
            lib_config = config["lib_config"]
            if "tracing_sampling_rate" in lib_config:
                base_rc_config["_trace_sample_rate"] = lib_config["tracing_sampling_rate"]

            if "tracing_sampling_rules" in lib_config:
                trace_sampling_rules = lib_config["tracing_sampling_rules"]
                if trace_sampling_rules:
                    # returns None if no rules
                    trace_sampling_rules = self.convert_rc_trace_sampling_rules(trace_sampling_rules)
                    if trace_sampling_rules:
                        base_rc_config["_trace_sampling_rules"] = trace_sampling_rules

            if "log_injection_enabled" in lib_config:
                base_rc_config["_logs_injection"] = lib_config["log_injection_enabled"]

            if "tracing_tags" in lib_config:
                tags = lib_config["tracing_tags"]
                if tags:
                    tags = self._format_tags(lib_config["tracing_tags"])
                base_rc_config["tags"] = tags

            if "tracing_enabled" in lib_config and lib_config["tracing_enabled"] is not None:
                base_rc_config["_tracing_enabled"] = asbool(lib_config["tracing_enabled"])  # type: ignore[assignment]

            if "tracing_header_tags" in lib_config:
                tags = lib_config["tracing_header_tags"]
                if tags:
                    tags = self._format_tags(lib_config["tracing_header_tags"])
                base_rc_config["_trace_http_header_tags"] = tags
        self._set_config_items([(k, v, "remote_config") for k, v in base_rc_config.items()])
        # called unconditionally to handle the case where header tags have been unset
        self._handle_remoteconfig_header_tags(base_rc_config)

    def _handle_remoteconfig_header_tags(self, base_rc_config):
        """Implements precedence order between remoteconfig header tags from code, env, and RC"""
        header_tags_conf = self._config["_trace_http_header_tags"]
        env_headers = header_tags_conf._env_value or {}
        code_headers = header_tags_conf._code_value or {}
        non_rc_header_tags = {**code_headers, **env_headers}
        selected_header_tags = base_rc_config.get("_trace_http_header_tags") or non_rc_header_tags
        self.http = HttpConfig(header_tags=selected_header_tags)

    def _format_tags(self, tags: List[Union[str, Dict]]) -> Dict[str, str]:
        if not tags:
            return {}
        if isinstance(tags[0], Dict):
            pairs = [(item["header"], item["tag_name"]) for item in tags]  # type: ignore[index]
        else:
            pairs = [t.split(":") for t in tags]  # type: ignore[union-attr,misc]
        return {k: v for k, v in pairs}

    def enable_remote_configuration(self):
        # type: () -> None
        """Enable fetching configuration from Datadog."""
        from ddtrace.internal.flare.flare import Flare
        from ddtrace.internal.flare.handler import _handle_tracer_flare
        from ddtrace.internal.flare.handler import _tracerFlarePubSub
        from ddtrace.internal.remoteconfig.worker import remoteconfig_poller

        remoteconfig_pubsub = self._remoteconfigPubSub()(self._handle_remoteconfig)
        flare = Flare(trace_agent_url=self._trace_agent_url, api_key=self._dd_api_key, ddconfig=self.__dict__)
        tracerflare_pubsub = _tracerFlarePubSub()(_handle_tracer_flare, flare)
        remoteconfig_poller.register("APM_TRACING", remoteconfig_pubsub)
        remoteconfig_poller.register("AGENT_CONFIG", tracerflare_pubsub)
        remoteconfig_poller.register("AGENT_TASK", tracerflare_pubsub)

    def _remove_invalid_rules(self, rc_rules: List) -> List:
        """Remove invalid sampling rules from the given list"""
        # loop through list of dictionaries, if a dictionary doesn't have certain attributes, remove it
        for rule in rc_rules:
            if (
                ("service" not in rule and "name" not in rule and "resource" not in rule and "tags" not in rule)
                or "sample_rate" not in rule
                or "provenance" not in rule
            ):
                log.debug("Invalid sampling rule from remoteconfig found, rule will be removed: %s", rule)
                rc_rules.remove(rule)

        return rc_rules

    def _tags_to_dict(self, tags: List[Dict]):
        """
        Converts a list of tag dictionaries to a single dictionary.
        """
        if isinstance(tags, list):
            return {tag["key"]: tag["value_glob"] for tag in tags}
        return tags

    def convert_rc_trace_sampling_rules(self, rc_rules: List[Dict[str, Any]]) -> Optional[str]:
        """Example of an incoming rule:
        [
          {
            "service": "my-service",
            "name": "web.request",
            "resource": "*",
            "provenance": "customer",
            "sample_rate": 1.0,
            "tags": [
              {
                "key": "care_about",
                "value_glob": "yes"
              },
              {
                "key": "region",
                "value_glob": "us-*"
              }
            ]
          }
        ]

                Example of a converted rule:
                '[{"sample_rate":1.0,"service":"my-service","resource":"*","name":"web.request","tags":{"care_about":"yes","region":"us-*"},provenance":"customer"}]'
        """
        rc_rules = self._remove_invalid_rules(rc_rules)
        for rule in rc_rules:
            tags = rule.get("tags")
            if tags:
                rule["tags"] = self._tags_to_dict(tags)
        if rc_rules:
            return json.dumps(rc_rules)
        else:
            return None
