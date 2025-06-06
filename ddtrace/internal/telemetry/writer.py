# -*- coding: utf-8 -*-
import http.client as httplib  # noqa: E402
import itertools
from logging import getLogger
import os
import sys
import time
from typing import TYPE_CHECKING  # noqa:F401
from typing import Any  # noqa:F401
from typing import Dict  # noqa:F401
from typing import List  # noqa:F401
from typing import Optional  # noqa:F401
from typing import Set  # noqa:F401
from typing import Tuple  # noqa:F401
from typing import Union  # noqa:F401
import urllib.parse as parse

from ...internal import atexit
from ...internal import forksafe
from ..agent import get_connection
from ..agent import get_trace_url
from ..compat import get_connection_response
from ..encoding import JSONEncoderV2
from ..periodic import PeriodicService
from ..runtime import container
from ..runtime import get_runtime_id
from ..service import ServiceStatus
from ..utils.formats import asbool
from ..utils.time import StopWatch
from ..utils.version import _pep440_to_semver
from . import modules
from .constants import TELEMETRY_APM_PRODUCT
from .constants import TELEMETRY_LOG_LEVEL  # noqa:F401
from .constants import TELEMETRY_TYPE_DISTRIBUTION
from .constants import TELEMETRY_TYPE_GENERATE_METRICS
from .constants import TELEMETRY_TYPE_LOGS
from .data import get_application
from .data import get_host_info
from .data import get_python_config_vars
from .data import update_imported_dependencies
from .metrics import CountMetric
from .metrics import DistributionMetric
from .metrics import GaugeMetric
from .metrics import MetricTagType  # noqa:F401
from .metrics import RateMetric
from .metrics_namespaces import MetricNamespace
from .metrics_namespaces import NamespaceMetricType  # noqa:F401


log = getLogger(__name__)


class _TelemetryConfig:
    API_KEY = os.environ.get("DD_API_KEY", None)
    SITE = os.environ.get("DD_SITE", "datadoghq.com")
    ENV = os.environ.get("DD_ENV", "")
    SERVICE = os.environ.get("DD_SERVICE", "unnamed-python-service")
    VERSION = os.environ.get("DD_VERSION", "")
    AGENTLESS_MODE = asbool(os.environ.get("DD_CIVISIBILITY_AGENTLESS_ENABLED", False))
    HEARTBEAT_INTERVAL = float(os.environ.get("DD_TELEMETRY_HEARTBEAT_INTERVAL", "60"))
    TELEMETRY_ENABLED = asbool(os.environ.get("DD_INSTRUMENTATION_TELEMETRY_ENABLED", "true").lower())
    DEPENDENCY_COLLECTION = asbool(os.environ.get("DD_TELEMETRY_DEPENDENCY_COLLECTION_ENABLED", "true"))
    INSTALL_ID = os.environ.get("DD_INSTRUMENTATION_INSTALL_ID", None)
    INSTALL_TYPE = os.environ.get("DD_INSTRUMENTATION_INSTALL_TYPE", None)
    INSTALL_TIME = os.environ.get("DD_INSTRUMENTATION_INSTALL_TIME", None)
    FORCE_START = asbool(os.environ.get("_DD_INSTRUMENTATION_TELEMETRY_TESTS_FORCE_APP_STARTED", "false"))


class LogData(dict):
    def __hash__(self):
        return hash((self["message"], self["level"], self.get("tags"), self.get("stack_trace")))

    def __eq__(self, other):
        return (
            self["message"] == other["message"]
            and self["level"] == other["level"]
            and self.get("tags") == other.get("tags")
            and self.get("stack_trace") == other.get("stack_trace")
        )


class _TelemetryClient:
    AGENT_ENDPOINT = "telemetry/proxy/api/v2/apmtelemetry"
    AGENTLESS_ENDPOINT_V2 = "api/v2/apmtelemetry"

    def __init__(self, agentless):
        # type: (bool) -> None
        self._telemetry_url = self.get_host(_TelemetryConfig.SITE, agentless)
        self._endpoint = self.get_endpoint(agentless)
        self._encoder = JSONEncoderV2()
        self._agentless = agentless

        self._headers = {
            "Content-Type": "application/json",
            "DD-Client-Library-Language": "python",
            "DD-Client-Library-Version": _pep440_to_semver(),
        }

        if agentless and _TelemetryConfig.API_KEY:
            self._headers["dd-api-key"] = _TelemetryConfig.API_KEY

    @property
    def url(self):
        return parse.urljoin(self._telemetry_url, self._endpoint)

    def send_event(self, request: Dict) -> Optional[httplib.HTTPResponse]:
        """Sends a telemetry request to the trace agent"""
        resp = None
        conn = None
        try:
            rb_json, _ = self._encoder.encode(request)
            headers = self.get_headers(request)
            with StopWatch() as sw:
                conn = get_connection(self._telemetry_url)
                conn.request("POST", self._endpoint, rb_json, headers)
                resp = get_connection_response(conn)
            if resp.status < 300:
                log.debug("sent %d in %.5fs to %s. response: %s", len(rb_json), sw.elapsed(), self.url, resp.status)
            else:
                log.debug("failed to send telemetry to %s. response: %s", self.url, resp.status)
        except Exception:
            log.debug("failed to send telemetry to %s.", self.url, exc_info=True)
        finally:
            if conn is not None:
                conn.close()
        return resp

    def get_headers(self, request):
        # type: (Dict) -> Dict
        """Get all telemetry api v2 request headers"""
        headers = self._headers.copy()
        headers["DD-Telemetry-Debug-Enabled"] = request["debug"]
        headers["DD-Telemetry-Request-Type"] = request["request_type"]
        headers["DD-Telemetry-API-Version"] = request["api_version"]
        container.update_headers_with_container_info(headers, container.get_container_info())
        return headers

    def get_endpoint(self, agentless: bool) -> str:
        return self.AGENTLESS_ENDPOINT_V2 if agentless else self.AGENT_ENDPOINT

    def get_host(self, site: str, agentless: bool) -> str:
        if not agentless:
            return get_trace_url()
        elif site == "datad0g.com":
            return "https://all-http-intake.logs.datad0g.com"
        elif site == "datadoghq.eu":
            return "https://instrumentation-telemetry-intake.datadoghq.eu"
        return f"https://instrumentation-telemetry-intake.{site}/"


class TelemetryWriter(PeriodicService):
    """
    Submits Instrumentation Telemetry events to the datadog agent.
    Supports v2 of the instrumentation telemetry api
    """

    # Counter representing the number of events sent by the writer. Here we are relying on the atomicity
    # of `itertools.count()` which is a CPython implementation detail. The sequence field in telemetry
    # payloads is only used in tests and is not required to process Telemetry events.
    _sequence = itertools.count(1)
    _ORIGINAL_EXCEPTHOOK = staticmethod(sys.excepthook)

    def __init__(self, is_periodic=True, agentless=None):
        # type: (bool, Optional[bool]) -> None
        super(TelemetryWriter, self).__init__(interval=min(_TelemetryConfig.HEARTBEAT_INTERVAL, 10))
        # Decouples the aggregation and sending of the telemetry events
        # TelemetryWriter events will only be sent when _periodic_count == _periodic_threshold.
        # By default this will occur at 10 second intervals.
        self._periodic_threshold = int(_TelemetryConfig.HEARTBEAT_INTERVAL // self.interval) - 1
        self._periodic_count = 0
        self._is_periodic = is_periodic
        self._integrations_queue = dict()  # type: Dict[str, Dict]
        # Currently telemetry only supports reporting a single error.
        # If we'd like to report multiple errors in the future
        # we could hack it in by xor-ing error codes and concatenating strings
        self._error = (0, "")  # type: Tuple[int, str]
        self._namespace = MetricNamespace()
        self._logs = set()  # type: Set[Dict[str, Any]]
        self._forked = False  # type: bool
        self._events_queue = []  # type: List[Dict]
        self._configuration_queue = {}  # type: Dict[str, Dict]
        self._lock = forksafe.Lock()  # type: forksafe.ResetObject
        self._imported_dependencies: Dict[str, str] = dict()
        self._product_enablement = {product.value: False for product in TELEMETRY_APM_PRODUCT}
        self._send_product_change_updates = False

        self.started = False

        # Debug flag that enables payload debug mode.
        self._debug = os.environ.get("DD_TELEMETRY_DEBUG", "false").lower() in ("true", "1")

        self._enabled = _TelemetryConfig.TELEMETRY_ENABLED

        if agentless is None:
            agentless = _TelemetryConfig.AGENTLESS_MODE or _TelemetryConfig.API_KEY not in (None, "")

        if agentless and not _TelemetryConfig.API_KEY:
            log.debug("Disabling telemetry: no Datadog API key found in agentless mode")
            self._enabled = False
        self._client = _TelemetryClient(agentless)

        if self._enabled:
            # Avoids sending app-started and app-closed events in forked processes
            forksafe.register(self._fork_writer)
            # shutdown the telemetry writer when the application exits
            atexit.register(self.app_shutdown)
            # Captures unhandled exceptions during application start up
            self.install_excepthook()
            # In order to support 3.12, we start the writer upon initialization.
            # See https://github.com/python/cpython/pull/104826.
            # Telemetry events will only be sent after the `app-started` is queued.
            # This will occur when the agent writer starts.
            self.enable()
            # Force app started for unit tests
            if _TelemetryConfig.FORCE_START:
                self._app_started()

    def enable(self):
        # type: () -> bool
        """
        Enable the instrumentation telemetry collection service. If the service has already been
        activated before, this method does nothing. Use ``disable`` to turn off the telemetry collection service.
        """
        if not self._enabled:
            return False

        if self.status == ServiceStatus.RUNNING:
            return True

        if self._is_periodic:
            self.start()
            return True

        self.status = ServiceStatus.RUNNING
        if _TelemetryConfig.DEPENDENCY_COLLECTION:
            modules.install_import_hook()
        return True

    def disable(self):
        # type: () -> None
        """
        Disable the telemetry collection service and drop the existing integrations and events
        Once disabled, telemetry collection can not be re-enabled.
        """
        self._enabled = False
        modules.uninstall_import_hook()
        self.reset_queues()
        if self._is_running():
            self.stop()
        else:
            self.status = ServiceStatus.STOPPED

    def enable_agentless_client(self, enabled=True):
        # type: (bool) -> None

        if self._client._agentless == enabled:
            return

        self._client = _TelemetryClient(enabled)

    def _is_running(self):
        # type: () -> bool
        """Returns True when the telemetry writer worker thread is running"""
        return self._is_periodic and self._worker is not None and self.status is ServiceStatus.RUNNING

    def add_event(self, payload, payload_type):
        # type: (Union[Dict[str, Any], List[Any]], str) -> None
        """
        Adds a Telemetry event to the TelemetryWriter event buffer

        :param Dict payload: stores a formatted telemetry event
        :param str payload_type: The payload_type denotes the type of telemetry request.
            Payload types accepted by telemetry/proxy v2: app-started, app-closing, app-integrations-change
        """
        if self.enable():
            event = {
                "tracer_time": int(time.time()),
                "runtime_id": get_runtime_id(),
                "api_version": "v2",
                "seq_id": next(self._sequence),
                "debug": self._debug,
                "application": get_application(
                    _TelemetryConfig.SERVICE, _TelemetryConfig.VERSION, _TelemetryConfig.ENV
                ),
                "host": get_host_info(),
                "payload": payload,
                "request_type": payload_type,
            }
            self._events_queue.append(event)

    def add_integration(self, integration_name, patched, auto_patched=None, error_msg=None, version=""):
        # type: (str, bool, Optional[bool], Optional[str], Optional[str]) -> None
        """
        Creates and queues the names and settings of a patched module

        :param str integration_name: name of patched module
        :param bool auto_enabled: True if module is enabled in _monkey.PATCH_MODULES
        """
        # Integrations can be patched before the telemetry writer is enabled.
        with self._lock:
            if integration_name not in self._integrations_queue:
                self._integrations_queue[integration_name] = {"name": integration_name}

            self._integrations_queue[integration_name]["version"] = version
            self._integrations_queue[integration_name]["enabled"] = patched

            if auto_patched is not None:
                self._integrations_queue[integration_name]["auto_enabled"] = auto_patched

            if error_msg is not None:
                self._integrations_queue[integration_name]["compatible"] = error_msg == ""
                self._integrations_queue[integration_name]["error"] = error_msg

    def add_error(self, code, msg, filename, line_number):
        # type: (int, str, Optional[str], Optional[int]) -> None
        """Add an error to be submitted with an event.
        Note that this overwrites any previously set errors.
        """
        if filename and line_number is not None:
            msg = "%s:%s: %s" % (filename, line_number, msg)
        self._error = (code, msg)

    def _app_started(self, register_app_shutdown=True):
        # type: (bool) -> None
        """Sent when TelemetryWriter is enabled or forks"""
        if self._forked or self.started:
            # app-started events should only be sent by the main process
            return
        #  List of configurations to be collected

        self.started = True

        products = {
            product: {"version": _pep440_to_semver(), "enabled": status}
            for product, status in self._product_enablement.items()
        }

        # SOABI should help us identify which wheels people are getting from PyPI
        self.add_configurations(get_python_config_vars())  # type: ignore

        payload = {
            "configuration": self._flush_configuration_queue(),
            "error": {
                "code": self._error[0],
                "message": self._error[1],
            },
            "products": products,
        }  # type: Dict[str, Union[Dict[str, Any], List[Any]]]
        # Add time to value telemetry metrics for single step instrumentation
        if _TelemetryConfig.INSTALL_ID or _TelemetryConfig.INSTALL_TYPE or _TelemetryConfig.INSTALL_TIME:
            payload["install_signature"] = {
                "install_id": _TelemetryConfig.INSTALL_ID,
                "install_type": _TelemetryConfig.INSTALL_TYPE,
                "install_time": _TelemetryConfig.INSTALL_TIME,
            }

        # Reset the error after it has been reported.
        self._error = (0, "")
        self.add_event(payload, "app-started")

    def _app_heartbeat_event(self):
        # type: () -> None
        if self._forked:
            # TODO: Enable app-heartbeat on forks
            #   Since we only send app-started events in the main process
            #   any forked processes won't be able to access the list of
            #   dependencies for this app, and therefore app-heartbeat won't
            #   add much value today.
            return

        self.add_event({}, "app-heartbeat")

    def _app_closing_event(self):
        # type: () -> None
        """Adds a Telemetry event which notifies the agent that an application instance has terminated"""
        if self._forked:
            # app-closing event should only be sent by the main process
            return
        payload = {}  # type: Dict
        self.add_event(payload, "app-closing")

    def _app_integrations_changed_event(self, integrations):
        # type: (List[Dict]) -> None
        """Adds a Telemetry event which sends a list of configured integrations to the agent"""
        payload = {
            "integrations": integrations,
        }
        self.add_event(payload, "app-integrations-change")

    def _flush_integrations_queue(self):
        # type: () -> List[Dict]
        """Flushes and returns a list of all queued integrations"""
        with self._lock:
            integrations = list(self._integrations_queue.values())
            self._integrations_queue = dict()
        return integrations

    def _flush_new_imported_dependencies(self) -> Set[str]:
        with self._lock:
            new_deps = modules.get_newly_imported_modules()
        return new_deps

    def _flush_configuration_queue(self):
        # type: () -> List[Dict]
        """Flushes and returns a list of all queued configurations"""
        with self._lock:
            configurations = list(self._configuration_queue.values())
            self._configuration_queue = {}
        return configurations

    def _app_client_configuration_changed_event(self, configurations):
        # type: (List[Dict]) -> None
        """Adds a Telemetry event which sends list of modified configurations to the agent"""
        payload = {
            "configuration": configurations,
        }
        self.add_event(payload, "app-client-configuration-change")

    def _app_dependencies_loaded_event(self, newly_imported_deps: List[str]):
        """Adds events to report imports done since the last periodic run"""

        if not _TelemetryConfig.DEPENDENCY_COLLECTION or not self._enabled:
            return

        with self._lock:
            packages = update_imported_dependencies(self._imported_dependencies, newly_imported_deps)

        if packages:
            payload = {"dependencies": packages}
            self.add_event(payload, "app-dependencies-loaded")

    def _app_product_change(self):
        # type: () -> None
        """Adds a Telemetry event which reports the enablement of an APM product"""

        if not self._send_product_change_updates:
            return

        payload = {
            "products": {
                product: {"version": _pep440_to_semver(), "enabled": status}
                for product, status in self._product_enablement.items()
            }
        }
        self.add_event(payload, "app-product-change")
        self._send_product_change_updates = False

    def product_activated(self, product, enabled):
        # type: (TELEMETRY_APM_PRODUCT, bool) -> None
        """Updates the product enablement dict"""

        if self._product_enablement[product.value] == enabled:
            return

        self._product_enablement[product.value] = enabled

        # If the app hasn't started, the product status will be included in the app_started event's payload
        if self.started:
            self._send_product_change_updates = True

    def remove_configuration(self, configuration_name):
        with self._lock:
            del self._configuration_queue[configuration_name]

    def add_configuration(self, configuration_name, configuration_value, origin="unknown"):
        # type: (str, Any, str) -> None
        """Creates and queues the name, origin, value of a configuration"""
        if isinstance(configuration_value, dict):
            configuration_value = ",".join(":".join((k, str(v))) for k, v in configuration_value.items())
        elif isinstance(configuration_value, (list, tuple)):
            configuration_value = ",".join(str(v) for v in configuration_value)
        elif not isinstance(configuration_value, (bool, str, int, float, type(None))):
            # convert unsupported types to strings
            configuration_value = str(configuration_value)

        with self._lock:
            self._configuration_queue[configuration_name] = {
                "name": configuration_name,
                "origin": origin,
                "value": configuration_value,
            }

    def add_configurations(self, configuration_list):
        # type: (List[Tuple[str, Union[bool, float, str], str]]) -> None
        """Creates and queues a list of configurations"""
        with self._lock:
            for name, value, _origin in configuration_list:
                self._configuration_queue[name] = {
                    "name": name,
                    "origin": _origin,
                    "value": value,
                }

    def add_log(self, level, message, stack_trace="", tags=None):
        # type: (TELEMETRY_LOG_LEVEL, str, str, Optional[Dict]) -> None
        """
        Queues log. This event is meant to send library logs to Datadog’s backend through the Telemetry intake.
        This will make support cycles easier and ensure we know about potentially silent issues in libraries.
        """
        if tags is None:
            tags = {}

        if self.enable():
            data = LogData(
                {
                    "message": message,
                    "level": level.value,
                    "tracer_time": int(time.time()),
                }
            )
            if tags:
                data["tags"] = ",".join(["%s:%s" % (k, str(v).lower()) for k, v in tags.items()])
            if stack_trace:
                data["stack_trace"] = stack_trace
            self._logs.add(data)

    def add_gauge_metric(self, namespace, name, value, tags=None):
        # type: (str,str, float, MetricTagType) -> None
        """
        Queues gauge metric
        """
        if self.status == ServiceStatus.RUNNING or self.enable():
            self._namespace.add_metric(
                GaugeMetric,
                namespace,
                name,
                value,
                tags,
                self.interval,
            )

    def add_rate_metric(self, namespace, name, value=1.0, tags=None):
        # type: (str,str, float, MetricTagType) -> None
        """
        Queues rate metric
        """
        if self.status == ServiceStatus.RUNNING or self.enable():
            self._namespace.add_metric(
                RateMetric,
                namespace,
                name,
                value,
                tags,
                self.interval,
            )

    def add_count_metric(self, namespace, name, value=1.0, tags=None):
        # type: (str,str, float, MetricTagType) -> None
        """
        Queues count metric
        """
        if self.status == ServiceStatus.RUNNING or self.enable():
            self._namespace.add_metric(
                CountMetric,
                namespace,
                name,
                value,
                tags,
            )

    def add_distribution_metric(self, namespace, name, value=1.0, tags=None):
        # type: (str,str, float, MetricTagType) -> None
        """
        Queues distributions metric
        """
        if self.status == ServiceStatus.RUNNING or self.enable():
            self._namespace.add_metric(
                DistributionMetric,
                namespace,
                name,
                value,
                tags,
            )

    def _flush_log_metrics(self):
        # type () -> Set[Metric]
        with self._lock:
            log_metrics = self._logs
            self._logs = set()
        return log_metrics

    def _generate_metrics_event(self, namespace_metrics):
        # type: (NamespaceMetricType) -> None
        for payload_type, namespaces in namespace_metrics.items():
            for namespace, metrics in namespaces.items():
                if metrics:
                    payload = {
                        "namespace": namespace,
                        "series": [m.to_dict() for m in metrics.values()],
                    }
                    log.debug("%s request payload, namespace %s", payload_type, namespace)
                    if payload_type == TELEMETRY_TYPE_DISTRIBUTION:
                        self.add_event(payload, TELEMETRY_TYPE_DISTRIBUTION)
                    elif payload_type == TELEMETRY_TYPE_GENERATE_METRICS:
                        self.add_event(payload, TELEMETRY_TYPE_GENERATE_METRICS)

    def _generate_logs_event(self, logs):
        # type: (Set[Dict[str, str]]) -> None
        log.debug("%s request payload", TELEMETRY_TYPE_LOGS)
        self.add_event({"logs": list(logs)}, TELEMETRY_TYPE_LOGS)

    def periodic(self, force_flush=False, shutting_down=False):
        # ensure app_started is called at least once in case traces weren't flushed
        self._app_started()
        self._app_product_change()

        namespace_metrics = self._namespace.flush()
        if namespace_metrics:
            self._generate_metrics_event(namespace_metrics)

        logs_metrics = self._flush_log_metrics()
        if logs_metrics:
            self._generate_logs_event(logs_metrics)

        # Telemetry metrics and logs should be aggregated into payloads every time periodic is called.
        # This ensures metrics and logs are submitted in 10 second time buckets.
        if self._is_periodic and force_flush is False:
            if self._periodic_count < self._periodic_threshold:
                self._periodic_count += 1
                return
            self._periodic_count = 0

        integrations = self._flush_integrations_queue()
        if integrations:
            self._app_integrations_changed_event(integrations)

        configurations = self._flush_configuration_queue()
        if configurations:
            self._app_client_configuration_changed_event(configurations)

        if _TelemetryConfig.DEPENDENCY_COLLECTION:
            newly_imported_deps = self._flush_new_imported_dependencies()
            if newly_imported_deps:
                self._app_dependencies_loaded_event(newly_imported_deps)

        if shutting_down:
            self._app_closing_event()

        # Send a heartbeat event to the agent, this is required to keep RC connections alive
        self._app_heartbeat_event()

        telemetry_events = self._flush_events_queue()
        for telemetry_event in telemetry_events:
            self._client.send_event(telemetry_event)

    def app_shutdown(self):
        if self.started:
            self.periodic(force_flush=True, shutting_down=True)
        self.disable()

    def reset_queues(self):
        # type: () -> None
        self._events_queue = []
        self._integrations_queue = dict()
        self._namespace.flush()
        self._logs = set()
        self._imported_dependencies = {}
        self._configuration_queue = {}

    def _flush_events_queue(self):
        # type: () -> List[Dict]
        """Flushes and returns a list of all telemtery event"""
        with self._lock:
            events = self._events_queue
            self._events_queue = []
        return events

    def _fork_writer(self):
        # type: () -> None
        self._forked = True
        # Avoid sending duplicate events.
        # Queued events should be sent in the main process.
        self.reset_queues()
        if self.status == ServiceStatus.STOPPED:
            return

        if self._is_running():
            self.stop(join=False)

        # Enable writer service in child process to avoid interpreter shutdown
        # error in Python 3.12
        self.enable()

    def _restart_sequence(self):
        self._sequence = itertools.count(1)

    def _stop_service(self, join=True, *args, **kwargs):
        # type: (...) -> None
        super(TelemetryWriter, self)._stop_service(*args, **kwargs)
        if join:
            self.join(timeout=2)

    def _telemetry_excepthook(self, tp, value, root_traceback):
        if root_traceback is not None:
            # Get the frame which raised the exception
            traceback = root_traceback
            while traceback.tb_next:
                traceback = traceback.tb_next

            lineno = traceback.tb_frame.f_code.co_firstlineno
            filename = traceback.tb_frame.f_code.co_filename
            self.add_error(1, str(value), filename, lineno)

            dir_parts = filename.split(os.path.sep)
            # Check if exception was raised in the  `ddtrace.contrib` package
            if "ddtrace" in dir_parts and "contrib" in dir_parts:
                ddtrace_index = dir_parts.index("ddtrace")
                contrib_index = dir_parts.index("contrib")
                # Check if the filename has the following format:
                # `../ddtrace/contrib/integration_name/..(subpath and/or file)...`
                if ddtrace_index + 1 == contrib_index and len(dir_parts) - 2 > contrib_index:
                    integration_name = dir_parts[contrib_index + 1]
                    if "internal" in dir_parts:
                        # Check if the filename has the format:
                        # `../ddtrace/contrib/internal/integration_name/..(subpath and/or file)...`
                        internal_index = dir_parts.index("internal")
                        integration_name = dir_parts[internal_index + 1]
                    self.add_count_metric(
                        "tracers",
                        "integration_errors",
                        1,
                        (("integration_name", integration_name), ("error_type", tp.__name__)),
                    )
                    error_msg = "{}:{} {}".format(filename, lineno, str(value))
                    self.add_integration(integration_name, True, error_msg=error_msg)

            if self._enabled and not self.started:
                self._app_started(False)

            self.app_shutdown()

        return TelemetryWriter._ORIGINAL_EXCEPTHOOK(tp, value, root_traceback)

    def install_excepthook(self):
        """Install a hook that intercepts unhandled exception and send metrics about them."""
        sys.excepthook = self._telemetry_excepthook

    def uninstall_excepthook(self):
        """Uninstall the global tracer except hook."""
        sys.excepthook = TelemetryWriter._ORIGINAL_EXCEPTHOOK
