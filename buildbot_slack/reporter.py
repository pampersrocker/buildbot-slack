# Based on the gitlab reporter from buildbot
from __future__ import absolute_import
from __future__ import print_function

import json
import hashlib
import statistics
import time

from buildbot.process.properties import Properties
from buildbot.process.results import statusToString
from buildbot.reporters import utils
from buildbot.reporters.base import ReporterBase
from buildbot.util import httpclientservice
from twisted.internet import defer
from twisted.internet import task
from twisted.logger import Logger
from buildbot.reporters.generators.build import BuildStartEndStatusGenerator
from buildbot.reporters.generators.buildrequest import BuildRequestGenerator
from buildbot.reporters.message import MessageFormatterRenderable

logger = Logger()

STATUS_EMOJIS = {
    "running": ":hourglass_flowing_sand:",
    "success": ":white_check_mark:",
    "warnings": ":meow_wow:",
    "failure": ":x:",
    "skipped": ":hand:",
    "exception": ":skull:",
    "retry": ":face_palm:",
    "cancelled": ":hand:",
}
STATUS_COLORS = {
    "running": "#2f96d4",
    "success": "#36a64f",
    "warnings": "#fc8c03",
    "failure": "#fc0303",
    "skipped": "#fc8c03",
    "exception": "#fc0303",
    "retry": "#fc8c03",
    "cancelled": "#fc8c03",
}

ETA_PROPERTY_EXCLUDE = {
    "buildid",
    "buildnumber",
    "build_number",
    "buildername",
    "builder_name",
    "workername",
    "worker_name",
    "got_revision",
    "revision",
    "timestamp",
    "reason",
}


class SlackStatusPush(ReporterBase):
    name = "SlackStatusPush"
    neededDetails = dict(wantProperties=True)

    def checkConfig(
        self,
        generators=None,
        endpoint=None,
        channel=None,
        host_url=None,
        username=None,
        slack_token=None,
        use_web_api=False,
        **kwargs,
    ):
        if endpoint is not None and not isinstance(endpoint, str):
            logger.warn(
                "[SlackStatusPush] endpoint should be a string, got '{typename}' instead",
                typename=type(endpoint).__name__,
            )
        elif isinstance(endpoint, str) and not endpoint.startswith("http"):
            logger.warn(
                '[SlackStatusPush] endpoint should start with "http...", endpoint: {endpoint}',
                endpoint=endpoint,
            )
        if use_web_api and (not slack_token or not isinstance(slack_token, str)):
            logger.warn("[SlackStatusPush] slack_token must be provided in bot mode")
        if not use_web_api and endpoint is None:
            logger.warn(
                "[SlackStatusPush] endpoint must be provided when use_web_api is disabled"
            )
        if channel and not isinstance(channel, str):
            logger.warn(
                "[SlackStatusPush] channel must be a string, got '{typename}' instead",
                typename=type(channel).__name__,
            )
        if username and not isinstance(username, str):
            logger.warn(
                "[SlackStatusPush] username must be a string, got '{typename}' instead",
                typename=type(username).__name__,
            )
        if host_url and not isinstance(host_url, str):  # deprecated
            logger.warn(
                "[SlackStatusPush] host_url must be a string, got '{typename}' instead",
                typename=type(host_url).__name__,
            )
        elif host_url:
            logger.warn(
                "[SlackStatusPush] argument host_url is deprecated and will be removed in the next release: specify the full url as endpoint"
            )

    @defer.inlineCallbacks
    def reconfigService(
        self,
        endpoint=None,
        channel=None,
        username=None,
        attachments=True,
        slack_token=None,
        use_web_api=False,
        api_base="https://slack.com/api",
        throttle_interval_secs=4,
        progress_refresh_secs=15,
        eta_history_limit=10,
        progress_bar_width=12,
        verbose=False,
        eta_match_properties=None,
        eta_exclude_properties=None,
        generators=None,
        debug=None,
        verify=None,
        codebases=None,
        builder=None,
        with_responsible_user=True,
        with_branch=True,
        with_builder=True,
        with_repository=True,
        extra_properties=None,
        failure_thread=True,
        failure_thread_upload_logs=True,
        **kwargs,
    ):
        self.debug = debug
        self.verify = verify

        if generators is None:
            generators = self._create_default_generators()

        yield super().reconfigService(generators=generators, **kwargs)

        self.endpoint = endpoint
        self.channel = channel
        self.username = username
        self.attachments = attachments
        self.codebases = codebases
        self.builder = builder
        self.with_responsible_user = with_responsible_user
        self.with_branch = with_branch
        self.with_builder = with_builder
        self.with_repository = with_repository
        self.extra_properties = extra_properties
        self.failure_thread = bool(failure_thread)
        self.failure_thread_upload_logs = bool(failure_thread_upload_logs)
        self.use_web_api = bool(use_web_api)
        self.slack_token = slack_token
        self.api_base = api_base
        self._state_class_name = "SlackStatusPush"
        self._state_object_name = self._build_state_object_name()
        self._state_object_id = None
        self.throttle_interval_secs = max(float(throttle_interval_secs), 0.0)
        self.progress_refresh_secs = max(float(progress_refresh_secs), 2.0)
        self.eta_history_limit = max(int(eta_history_limit), 1)
        self.progress_bar_width = max(int(progress_bar_width), 5)
        self.eta_match_properties = self._normalize_property_list(eta_match_properties)
        self.eta_exclude_properties = set(ETA_PROPERTY_EXCLUDE)
        self.eta_exclude_properties.update(self._normalize_property_list(eta_exclude_properties) or set())
        self.verbose = verbose
        self.project_ids = {}
        self._api_http = None
        self._http = None
        self._message_refs = {}
        self._runtime = {}
        self._last_step_update = {}
        self._channel_name_cache = {}
        self._step_event_consumers = {}
        self._progress_update_tasks = {}

        if self.endpoint:
            self._http = yield httpclientservice.HTTPClientService.getService(
                self.master,
                self.endpoint,
                debug=self.debug,
                verify=self.verify,
            )

        if self.use_web_api:
            self._api_http = yield httpclientservice.HTTPClientService.getService(
                self.master,
                self.api_base,
                debug=self.debug,
                verify=self.verify,
            )

        yield self._reconfigure_step_event_consumers()

        yield self._ensure_state_object_id()

    @defer.inlineCallbacks
    def _reconfigure_step_event_consumers(self):
        wanted_keys = set()
        if self.use_web_api:
            wanted_keys.update(
                {
                    ("builds", None, "steps", None, "started"),
                    ("builds", None, "steps", None, "finished"),
                }
            )

        for key in list(self._step_event_consumers.keys()):
            if key not in wanted_keys:
                yield self._step_event_consumers[key].stopConsuming()
                del self._step_event_consumers[key]

        for key in sorted(wanted_keys):
            if key not in self._step_event_consumers:
                self._step_event_consumers[key] = yield self.master.mq.startConsuming(
                    self._got_step_event,
                    key,
                )

    @defer.inlineCallbacks
    def _got_step_event(self, key, msg):
        event = key[-1]
        if event == "started":
            yield self.stepStarted(key, msg)
        elif event == "finished":
            yield self.stepFinished(key, msg)

    @defer.inlineCallbacks
    def stopService(self):
        for buildid in list(getattr(self, "_progress_update_tasks", {}).keys()):
            self._stop_periodic_progress_updates(buildid)
        for key in list(getattr(self, "_step_event_consumers", {}).keys()):
            yield self._step_event_consumers[key].stopConsuming()
            del self._step_event_consumers[key]
        yield super().stopService()

    def _create_default_generators(self):
        start_formatter = MessageFormatterRenderable('Build started.')
        end_formatter = MessageFormatterRenderable('Build done.')
        pending_formatter = MessageFormatterRenderable('Build pending.')

        return [
            # BuildRequestGenerator(formatter=pending_formatter),
            BuildStartEndStatusGenerator(start_formatter=start_formatter,
                                         end_formatter=end_formatter)
        ]

    @defer.inlineCallbacks
    def getAttachments(self, build):
        buildset = build.get("buildset") or {}
        sourcestamps = buildset.get("sourcestamps") or []
        attachments = []
        build_status = self._get_status_key(build)
        state_key = self._state_key(build.get("buildid"))
        runtime_state = self._runtime.get(state_key)
        builder_name = yield self._get_builder_name(build, runtime_state)

        for sourcestamp in sourcestamps:
            if self.codebases != None and sourcestamp.get("codebase") not in self.codebases:
                continue
            sha = sourcestamp.get("revision")

            title = "Build #{buildid}".format(buildid=build["buildid"])
            project = sourcestamp.get("project")
            if project:
                title += " for {project} {sha}".format(project=project, sha=sha)
            sub_build = bool(buildset.get("parent_buildid"))
            if sub_build:
                title += " {relationship}: #{parent_build_id}".format(
                    relationship=buildset.get("parent_relationship"),
                    parent_build_id=buildset.get("parent_buildid"),
                )

            fields = []
            if not sub_build:
                branch_name = sourcestamp.get("branch")
                if branch_name and self.with_branch:
                    fields.append({"title": "Branch", "value": branch_name, "short": True})
                repositories = sourcestamp.get("repository")
                if repositories and self.with_repository:
                    fields.append({"title": "Repository", "value": repositories, "short": True})
                responsible_users = yield utils.getResponsibleUsersForBuild(self.master, build["buildid"])
                if responsible_users and self.with_responsible_user:
                    fields.append(
                        {
                            "title": "Committers",
                            "value": ", ".join(responsible_users),
                            "short": True,
                        }
                    )
                if self.with_builder:
                    fields.append({"title": "Builder", "value": builder_name, "short": True})
                if self.extra_properties != None:
                    props = Properties.fromDict(build.get('properties', {}))
                    for extra_property in self.extra_properties:
                        if extra_property in props:
                            fields.append({"title": extra_property, "value": props[extra_property], "short": True})
            attachments.append(
                {
                    "title": title,
                    "title_link": build.get("url", ""),
                    "fallback": "{}: <{}>".format(title, build.get("url", "")),
                    "text": "Status: *{status}*".format(status=build_status),
                    "color": STATUS_COLORS.get(build_status, ""),
                    "mrkdwn_in": ["text", "title", "fallback"],
                    "fields": fields,
                }
            )
        if not attachments:
            attachments.append(
                {
                    "title": "Build #{buildid}".format(buildid=build.get("buildid", "?")),
                    "title_link": build.get("url", ""),
                    "fallback": "Build #{buildid}".format(buildid=build.get("buildid", "?")),
                    "text": "Builder: *{builder}*\nStatus: *{status}*".format(
                        builder=builder_name,
                        status=build_status,
                    ),
                    "color": STATUS_COLORS.get(build_status, ""),
                    "mrkdwn_in": ["text", "title", "fallback"],
                    "fields": [],
                }
            )
        return attachments

    @defer.inlineCallbacks
    def getBuildDetailsAndSendMessage(self, report):
        build = report["builds"][0]
        text = yield self.getMessage(report)
        postData = {}
        if self.attachments:
            attachments = yield self.getAttachments(build)
            if attachments:
                postData["attachments"] = attachments
        else:
            text += "\n here: " + build["url"]
        postData["text"] = text

        if self.channel:
            postData["channel"] = self.channel

        if self.username:
            postData["username"] = self.username

        extra_params = yield self.getExtraParams(build)
        postData.update(extra_params)
        return postData

    def getMessage(self, report):
        build = report["builds"][0]
        emoji = STATUS_EMOJIS.get(self._get_status_key(build), ":hourglass_flowing_sand:")
        return f"{emoji} {report['body']}"

    # returns a Deferred that returns None
    def buildStarted(self, key, build):
        self._initialize_runtime(build)
        return self.send(build, key[2])

    # returns a Deferred that returns None
    def buildFinished(self, key, build):
        self._initialize_runtime(build)
        return self.send(build, key[2])

    # returns a Deferred that returns None
    def stepStarted(self, key, step):
        return self._handle_step_event(step, finished=False)

    # returns a Deferred that returns None
    def stepFinished(self, key, step):
        return self._handle_step_event(step, finished=True)

    def getExtraParams(self, build):
        return {}

    def _get_status_key(self, build):
        if build.get("results") is None:
            return "running"
        return statusToString(build["results"])

    def _state_key(self, buildid):
        return str(buildid)

    def _build_state_object_name(self):
        raw = json.dumps(
            {
                "endpoint": self.endpoint,
                "channel": self.channel,
                "api_base": self.api_base,
                "use_web_api": self.use_web_api,
            },
            sort_keys=True,
        )
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
        return "slack_status_push_{digest}".format(digest=digest)

    @defer.inlineCallbacks
    def _ensure_state_object_id(self):
        if self._state_object_id is not None:
            return self._state_object_id
        if not getattr(self.master, "db", None) or not getattr(self.master.db, "state", None):
            logger.warn("Buildbot DB state connector is not available; using in-memory message refs only")
            return None
        try:
            self._state_object_id = yield self.master.db.state.getObjectId(
                self._state_object_name,
                self._state_class_name,
            )
        except Exception as exc:
            logger.error("Unable to initialize state object for SlackStatusPush: {error}", error=exc)
            self._state_object_id = None
        return self._state_object_id

    def _state_value_name(self, buildid):
        return "build_{buildid}".format(buildid=buildid)

    @defer.inlineCallbacks
    def _set_message_ref(self, buildid, channel, ts):
        state_key = self._state_key(buildid)
        state_value = {"channel": channel, "ts": ts}
        self._message_refs[state_key] = state_value
        objectid = yield self._ensure_state_object_id()
        if objectid is None:
            return
        try:
            yield self.master.db.state.setState(
                objectid,
                self._state_value_name(buildid),
                state_value,
            )
        except Exception as exc:
            logger.error("Unable to persist Slack message reference for build {buildid}: {error}", buildid=buildid, error=exc)

    @defer.inlineCallbacks
    def _get_message_ref(self, buildid):
        state_key = self._state_key(buildid)
        cached = self._message_refs.get(state_key)
        if cached is not None:
            return cached
        objectid = yield self._ensure_state_object_id()
        if objectid is None:
            return None
        try:
            state_value = yield self.master.db.state.getState(
                objectid,
                self._state_value_name(buildid),
                None,
            )
        except KeyError:
            return None
        except Exception as exc:
            logger.error("Unable to read Slack message reference for build {buildid}: {error}", buildid=buildid, error=exc)
            return None
        if not isinstance(state_value, dict):
            return None
        if "channel" not in state_value or "ts" not in state_value:
            return None
        self._message_refs[state_key] = state_value
        return state_value

    @defer.inlineCallbacks
    def _clear_build_state(self, buildid):
        state_key = self._state_key(buildid)
        self._stop_periodic_progress_updates(buildid)
        if state_key in self._message_refs:
            del self._message_refs[state_key]
        objectid = yield self._ensure_state_object_id()
        if objectid is not None:
            try:
                yield self.master.db.state.setState(
                    objectid,
                    self._state_value_name(buildid),
                    None,
                )
            except Exception as exc:
                logger.error("Unable to clear Slack message reference for build {buildid}: {error}", buildid=buildid, error=exc)
        if state_key in self._runtime:
            del self._runtime[state_key]
        if state_key in self._last_step_update:
            del self._last_step_update[state_key]

    def _initialize_runtime(self, build):
        buildid = build.get("buildid")
        if buildid is None:
            return
        state_key = self._state_key(buildid)
        if state_key not in self._runtime:
            raw_start_time = build.get("started_at") or build.get("start_time") or time.time()
            start_time = self._coerce_timestamp(raw_start_time, default=time.time())
            builder_info = build.get("builder") or {}
            self._runtime[state_key] = {
                "known_steps": set(),
                "finished_steps": set(),
                "current_step": None,
                "start_time": start_time,
                "builderid": builder_info.get("builderid") or build.get("builderid"),
                "builder_name": builder_info.get("name"),
                "estimated_total_steps": None,
                "planned_total_steps": None,
            }
        steps = build.get("steps") or []
        runtime_state = self._runtime[state_key]
        self._refresh_runtime_from_steps(runtime_state, steps)

    def _refresh_runtime_from_steps(self, runtime_state, steps):
        for step in steps:
            step_name = step.get("name")
            if not step_name:
                continue
            runtime_state["known_steps"].add(step_name)
            if step.get("complete") or step.get("complete_at") or step.get("complete_time") is not None:
                runtime_state["finished_steps"].add(step_name)

    @defer.inlineCallbacks
    def _handle_step_event(self, step, finished=False):
        if not self.use_web_api:
            return
        buildid = step.get("buildid")
        if buildid is None:
            return

        state_key = self._state_key(buildid)
        if state_key not in self._runtime:
            build = yield self.master.data.get(("builds", buildid))
            self._initialize_runtime(build)

        runtime_state = self._runtime.get(state_key)
        if runtime_state is None:
            return

        step_name = step.get("name") or "unknown-step"
        runtime_state["known_steps"].add(step_name)

        if finished:
            runtime_state["finished_steps"].add(step_name)
            if runtime_state.get("current_step") == step_name:
                runtime_state["current_step"] = None
        else:
            runtime_state["current_step"] = step_name

        self._ensure_periodic_progress_updates(buildid)

        yield self._send_step_progress_update(buildid)

    def _stop_periodic_progress_updates(self, buildid):
        task_key = self._state_key(buildid)
        progress_task = self._progress_update_tasks.get(task_key)
        if progress_task is None:
            return
        if progress_task.running:
            progress_task.stop()
        del self._progress_update_tasks[task_key]

    def _on_periodic_progress_error(self, failure, buildid):
        # LoopingCall stops with CancelledError when explicitly stopped.
        if not failure.check(defer.CancelledError):
            logger.warn(
                "Periodic progress update failed for build {buildid}: {error}",
                buildid=buildid,
                error=failure,
            )
        self._stop_periodic_progress_updates(buildid)

    def _ensure_periodic_progress_updates(self, buildid):
        if not self.use_web_api:
            return
        task_key = self._state_key(buildid)
        existing = self._progress_update_tasks.get(task_key)
        if existing is not None and existing.running:
            return

        progress_task = task.LoopingCall(self._periodic_progress_tick, buildid)
        reactor = getattr(self.master, "reactor", None)
        if reactor is not None:
            progress_task.clock = reactor
        deferred_loop = progress_task.start(self.progress_refresh_secs, now=False)
        deferred_loop.addErrback(self._on_periodic_progress_error, buildid)
        self._progress_update_tasks[task_key] = progress_task

    @defer.inlineCallbacks
    def _periodic_progress_tick(self, buildid):
        if not self.use_web_api:
            return
        build = yield self.master.data.get(("builds", buildid))
        if build is None or self._get_status_key(build) != "running":
            self._stop_periodic_progress_updates(buildid)
            return
        yield self._send_step_progress_update(buildid, force=True)

    def _format_duration(self, seconds):
        if seconds is None:
            return "unknown"
        total_seconds = int(max(seconds, 0))
        mins, secs = divmod(total_seconds, 60)
        hours, mins = divmod(mins, 60)
        if hours > 0:
            return "{h}h {m}m {s}s".format(h=hours, m=mins, s=secs)
        if mins > 0:
            return "{m}m {s}s".format(m=mins, s=secs)
        return "{s}s".format(s=secs)

    def _normalize_property_list(self, values):
        if values is None:
            return None
        if isinstance(values, str):
            values = [values]
        normalized = set()
        for value in values:
            if value is None:
                continue
            normalized.add(str(value))
        return normalized

    def _normalize_property_value(self, value):
        if isinstance(value, (list, tuple)):
            if value:
                return self._normalize_property_value(value[0])
            return None
        if isinstance(value, dict):
            if "value" in value:
                return self._normalize_property_value(value.get("value"))
            return None
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return None

    @staticmethod
    def _db_model_to_dict(model):
        """Convert a Buildbot DB model object (dataclass) to a plain dict."""
        if isinstance(model, dict):
            return model
        import dataclasses
        if dataclasses.is_dataclass(model) and not isinstance(model, type):
            return dataclasses.asdict(model)
        return vars(model)

    def _coerce_timestamp(self, value, default=None):
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return default
        timestamp_fn = getattr(value, "timestamp", None)
        if callable(timestamp_fn):
            try:
                ts_value = timestamp_fn()
                if isinstance(ts_value, (int, float)):
                    return float(ts_value)
                if isinstance(ts_value, str):
                    return float(ts_value)
            except Exception:
                return default
        return default

    def _coerce_duration_seconds(self, value, default=None):
        if value is None:
            return default
        if isinstance(value, (int, float)):
            duration = float(value)
            return duration if duration >= 0 else default
        if isinstance(value, str):
            try:
                duration = float(value)
                return duration if duration >= 0 else default
            except ValueError:
                return default
        total_seconds_fn = getattr(value, "total_seconds", None)
        if callable(total_seconds_fn):
            try:
                duration = float(total_seconds_fn())
                return duration if duration >= 0 else default
            except Exception:
                return default
        return default

    def _extract_build_duration_seconds(self, build):
        duration_keys = (
            "duration",
            "duration_s",
            "build_duration",
            "build_duration_s",
            "elapsed",
            "elapsed_s",
            "runtime",
            "run_time",
            "total_duration",
        )
        for key in duration_keys:
            duration = self._coerce_duration_seconds(build.get(key))
            if duration is not None:
                return duration

        start_time = self._coerce_timestamp(build.get("start_time") or build.get("started_at"))
        complete_time = self._coerce_timestamp(build.get("complete_time") or build.get("complete_at"))
        if start_time is None or complete_time is None:
            return None
        return max(complete_time - start_time, 0.0)

    def _extract_eta_properties(self, build):
        raw_props = build.get("properties") or {}
        if not isinstance(raw_props, dict):
            return {}
        extracted = {}
        for key, raw_value in raw_props.items():
            if self.eta_match_properties is not None and key not in self.eta_match_properties:
                continue
            if key in self.eta_exclude_properties:
                continue
            normalized = self._normalize_property_value(raw_value)
            if normalized is None:
                continue
            extracted[key] = normalized
        return extracted

    def _score_property_match(self, current_props, candidate_props):
        if not current_props:
            return (0, 0)
        comparable_keys = [key for key in current_props.keys() if key in candidate_props]
        if not comparable_keys:
            return (0, 0)
        score = 0
        for key in comparable_keys:
            if current_props[key] == candidate_props[key]:
                score += 1
        return (score, len(comparable_keys))

    @defer.inlineCallbacks
    def _get_builder_name(self, build, runtime_state=None):
        builder_info = build.get("builder") or {}
        builder_name = builder_info.get("name")
        if builder_name:
            if runtime_state is not None:
                runtime_state["builder_name"] = builder_name
            return builder_name

        if runtime_state is not None and runtime_state.get("builder_name"):
            return runtime_state["builder_name"]

        builderid = (
            builder_info.get("builderid")
            or build.get("builderid")
            or (runtime_state or {}).get("builderid")
        )
        if builderid is None:
            return "unknown"

        try:
            builder_data = yield self.master.data.get(("builders", builderid))
            resolved_name = (builder_data or {}).get("name")
            if resolved_name:
                if runtime_state is not None:
                    runtime_state["builder_name"] = resolved_name
                    runtime_state["builderid"] = builderid
                return resolved_name
        except Exception as exc:
            logger.warn("Unable to resolve builder name for builderid {builderid}: {error}", builderid=builderid, error=exc)

        return "unknown"

    @defer.inlineCallbacks
    def _estimate_total_steps(self, build, runtime_state):
        known_steps = max(len(runtime_state.get("known_steps", [])), 1)
        factory_total = yield self._estimate_factory_total_steps(build, runtime_state)

        # Factory count is the most authoritative — use it directly when available.
        if isinstance(factory_total, int) and factory_total > 0:
            # Still respect more steps seen live (e.g. dynamic steps added at runtime).
            return max(factory_total, known_steps)

        cached_total = runtime_state.get("estimated_total_steps")
        if isinstance(cached_total, int) and cached_total > 0:
            return max(cached_total, known_steps)

        builderid = runtime_state.get("builderid") or build.get("builderid")
        if builderid is None:
            return known_steps

        try:
            history = (yield self.master.db.builds.getBuilds(
                builderid=builderid,
            ) or [])[:self.eta_history_limit]

            step_counts = []
            perfect_match_counts = []
            best_match_counts = []
            best_match_score = -1
            current_props = self._extract_eta_properties(build)

            for _hist_model in history:
                hist_build = self._db_model_to_dict(_hist_model)
                hist_buildid = hist_build.get("buildid") or hist_build.get("id")
                if hist_buildid == build.get("buildid"):
                    continue

                # Only count steps from successful completed builds.
                hist_results = hist_build.get("results")
                if hist_results != 0:
                    continue

                if not hist_buildid:
                    continue
                hist_full_build = yield self.master.data.get(("builds", hist_buildid))
                if not hist_full_build:
                    continue
                hist_steps = yield self.master.data.get(("builds", hist_buildid, "steps"))
                if not hist_steps:
                    hist_steps = hist_full_build.get("steps") or []
                step_count = len(hist_steps)
                if step_count <= 0:
                    continue
                step_counts.append(step_count)

                if not current_props:
                    continue

                candidate_props = self._extract_eta_properties(hist_full_build)
                score, comparable_count = self._score_property_match(current_props, candidate_props)
                if comparable_count == 0:
                    continue
                if score == comparable_count:
                    perfect_match_counts.append(step_count)
                    continue
                if score <= 0:
                    continue
                if score > best_match_score:
                    best_match_score = score
                    best_match_counts = [step_count]
                elif score == best_match_score:
                    best_match_counts.append(step_count)

            candidate_counts = None
            if perfect_match_counts:
                candidate_counts = perfect_match_counts
            elif best_match_counts:
                candidate_counts = best_match_counts
            elif step_counts:
                candidate_counts = step_counts

            if candidate_counts:
                estimated_total = max(int(round(statistics.median(candidate_counts))), 1)
                runtime_state["estimated_total_steps"] = estimated_total
                return max(estimated_total, known_steps)
        except Exception as exc:
            logger.warn(
                "Unable to estimate total step count for build {buildid}: {error}",
                buildid=build.get("buildid"),
                error=exc,
            )

        return known_steps

    @defer.inlineCallbacks
    def _estimate_factory_total_steps(self, build, runtime_state):
        cached = runtime_state.get("planned_total_steps")
        if isinstance(cached, int) and cached > 0:
            return cached

        builder_name = yield self._get_builder_name(build, runtime_state)
        if not builder_name or builder_name == "unknown":
            return None

        botmaster = getattr(self.master, "botmaster", None)
        builders = getattr(botmaster, "builders", None)
        builder_obj = None
        if isinstance(builders, dict):
            builder_obj = builders.get(builder_name)

        if builder_obj is None:
            return None

        builder_config = getattr(builder_obj, "config", None)
        factory = getattr(builder_config, "factory", None)
        factory_steps = getattr(factory, "steps", None)
        if not isinstance(factory_steps, list):
            return None

        planned_total = max(len(factory_steps), 1)
        runtime_state["planned_total_steps"] = planned_total
        return planned_total

    def _is_channel_id(self, channel):
        if not isinstance(channel, str) or not channel:
            return False
        return channel[0] in ("C", "G", "D")

    @defer.inlineCallbacks
    def _resolve_channel_id(self, channel):
        if channel is None:
            return None
        if self._is_channel_id(channel):
            return channel

        channel_name = channel.lstrip("#").strip()
        if not channel_name:
            return channel

        cached = self._channel_name_cache.get(channel_name)
        if cached:
            return cached

        cursor = None
        while True:
            payload = {
                "exclude_archived": True,
                "limit": 200,
                "types": "public_channel,private_channel",
            }
            if cursor:
                payload["cursor"] = cursor

            response = yield self._call_slack_api("conversations.list", payload)
            if response is None:
                return channel

            for conversation in response.get("channels", []):
                name = conversation.get("name")
                chan_id = conversation.get("id")
                if not name or not chan_id:
                    continue
                self._channel_name_cache[name] = chan_id
                if name == channel_name:
                    return chan_id

            cursor = ((response.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not cursor:
                break

        logger.error(
            "Unable to resolve Slack channel '{channel}' to an ID; using provided value",
            channel=channel,
        )
        return channel

    @defer.inlineCallbacks
    def _estimate_eta_seconds(self, build, runtime_state, elapsed):
        total_steps = yield self._estimate_total_steps(build, runtime_state)
        finished_steps = len(runtime_state.get("finished_steps", []))
        builderid = runtime_state.get("builderid")
        buildid = build.get("buildid")
        if builderid is not None:
            try:
                history = (yield self.master.db.builds.getBuilds(
                    builderid=builderid,
                ) or [])[:self.eta_history_limit]
                durations = []
                perfect_match_durations = []
                best_match_durations = []
                best_match_score = -1
                current_props = self._extract_eta_properties(build)
                for _hist_model in history:
                    hist_build = self._db_model_to_dict(_hist_model)
                    hist_buildid = hist_build.get("buildid") or hist_build.get("id")
                    if hist_buildid == buildid:
                        continue
                    # Only use successful completed builds for ETA.
                    hist_results = hist_build.get("results")
                    if hist_results != 0:
                        continue
                    duration = self._extract_build_duration_seconds(hist_build)
                    if duration is None or duration <= 0:
                        continue
                    durations.append(duration)

                    if not current_props:
                        continue
                    if not hist_buildid:
                        continue
                    hist_full_build = yield self.master.data.get(("builds", hist_buildid))
                    if not hist_full_build:
                        continue
                    candidate_props = self._extract_eta_properties(hist_full_build)
                    score, comparable_count = self._score_property_match(current_props, candidate_props)
                    if comparable_count == 0:
                        continue
                    if score == comparable_count:
                        perfect_match_durations.append(duration)
                        continue
                    if score <= 0:
                        continue
                    if score > best_match_score:
                        best_match_score = score
                        best_match_durations = [duration]
                    elif score == best_match_score:
                        best_match_durations.append(duration)

                candidate_durations = None
                if perfect_match_durations:
                    candidate_durations = perfect_match_durations
                elif best_match_durations:
                    candidate_durations = best_match_durations
                elif durations:
                    candidate_durations = durations

                if candidate_durations:
                    median_duration = statistics.median(candidate_durations)

                    if finished_steps > 0 and total_steps > finished_steps:
                        # Step-based projection: scale remaining time by fraction of steps left.
                        fraction_done = float(finished_steps) / float(total_steps)
                        if fraction_done > 0:
                            projected_total = elapsed / fraction_done
                            step_eta = max(int(projected_total - elapsed), 0)
                        else:
                            step_eta = None

                        if elapsed < median_duration:
                            # Still within historical range: blend both models.
                            duration_eta = int(median_duration - elapsed)
                            if step_eta is not None:
                                return max(duration_eta, step_eta)
                            return duration_eta
                        else:
                            # Build is running longer than median — trust step projection.
                            if step_eta is not None:
                                return step_eta
                            # Fallback: extrapolate median proportionally.
                            remaining_fraction = max(1.0 - float(finished_steps) / float(total_steps), 0.0)
                            return max(int(median_duration * remaining_fraction), 0)

                    # No step information: use duration minus elapsed, but extrapolate
                    # proportionally if we have overrun the median.
                    if elapsed < median_duration:
                        return int(median_duration - elapsed)
                    # Overrun: show a small proportional estimate, not 0.
                    overrun_factor = elapsed / median_duration
                    return max(int(median_duration * (overrun_factor - 1.0) * 0.5), 10)
            except Exception as exc:
                logger.warn("Unable to compute historical ETA for build {buildid}: {error}", buildid=buildid, error=exc)

        if finished_steps > 0:
            projected_total = (elapsed / float(finished_steps)) * float(total_steps)
            return max(int(projected_total - elapsed), 0)
        return None

    @defer.inlineCallbacks
    def _build_progress_text(self, build, runtime_state):
        total_steps = yield self._estimate_total_steps(build, runtime_state)
        finished_steps = len(runtime_state.get("finished_steps", []))
        step_ratio = min(float(finished_steps) / float(total_steps), 1.0)

        now = time.time()
        start_time = self._coerce_timestamp(runtime_state.get("start_time", now), default=now)
        if start_time is None:
            start_time = now
        elapsed_seconds = max(now - start_time, 0.0)
        eta_seconds = yield self._estimate_eta_seconds(build, runtime_state, elapsed_seconds)

        if eta_seconds is not None and elapsed_seconds > 0:
            time_ratio = min(elapsed_seconds / (elapsed_seconds + float(eta_seconds) + 0.001), 1.0)
            progress_ratio = min(step_ratio, time_ratio)
        else:
            progress_ratio = step_ratio

        done_blocks = int(progress_ratio * self.progress_bar_width)
        pending_blocks = self.progress_bar_width - done_blocks
        progress_bar = "[{done}{pending}] {pct:3.0f}%".format(
            done="#" * done_blocks,
            pending="-" * pending_blocks,
            pct=progress_ratio * 100.0,
        )

        current_step = runtime_state.get("current_step") or "waiting"
        builder_name = yield self._get_builder_name(build, runtime_state)
        build_label = "#{buildid}".format(buildid=build.get("buildid", "?"))

        lines = [
            "{emoji} Build in progress".format(emoji=STATUS_EMOJIS["running"]),
            "Build: {build} on *{builder}*".format(build=build_label, builder=builder_name),
            "Step: *{current}* ({done}/{total})".format(
                current=current_step,
                done=finished_steps,
                total=total_steps,
            ),
            "Progress: {bar}".format(bar=progress_bar),
            "Elapsed: {elapsed}".format(elapsed=self._format_duration(elapsed_seconds)),
        ]
        if eta_seconds is not None:
            lines.append("ETA: {eta}".format(eta=self._format_duration(eta_seconds)))
        if build.get("url"):
            lines.append("Details: {url}".format(url=build["url"]))
        return "\n".join(lines)

    def _should_skip_build(self, build):
        if self.builder != None and (build.get("builder") or {}).get("name") not in self.builder:
            return True
        if self.codebases is None:
            return False
        sourcestamps = ((build.get("buildset") or {}).get("sourcestamps") or [])
        for sourcestamp in sourcestamps:
            if sourcestamp.get("codebase") in self.codebases:
                return False
        return True

    @defer.inlineCallbacks
    def _post_webhook_message(self, postData):
        if self._http is None:
            logger.error("Webhook endpoint is not configured")
            return
        response = yield self._http.post("", json=postData)
        if response.code != 200:
            content = yield response.content()
            logger.error(
                "{code}: unable to upload status: {content}",
                code=response.code,
                content=content,
            )

    @defer.inlineCallbacks
    def _call_slack_api(self, path, payload):
        if self._api_http is None or not self.slack_token:
            logger.error("Slack Web API is not configured correctly")
            return None
        api_path = path if path.startswith("/") else "/{path}".format(path=path)
        headers = {
            "Authorization": "Bearer {token}".format(token=self.slack_token),
            "Content-Type": "application/json; charset=utf-8",
        }
        response = yield self._api_http.post(api_path, json=payload, headers=headers)
        content = yield response.content()
        if response.code != 200:
            logger.error(
                "{code}: Slack API request failed at {path}: {content}",
                code=response.code,
                path=api_path,
                content=content,
            )
            return None
        try:
            data = json.loads(content.decode("utf-8")) if isinstance(content, bytes) else json.loads(content)
        except Exception as exc:
            logger.error("Could not decode Slack API response for {path}: {error}", path=api_path, error=exc)
            return None
        if not data.get("ok"):
            logger.error("Slack API {path} returned an error: {error}", path=api_path, error=data.get("error"))
            return None
        return data

    @defer.inlineCallbacks
    def _send_step_progress_update(self, buildid, force=False):
        if not self.use_web_api:
            return
        state_key = self._state_key(buildid)
        message_ref = yield self._get_message_ref(buildid)
        if message_ref is None:
            return
        now = time.time()
        last_update = self._last_step_update.get(state_key)
        if not force and last_update is not None and (now - last_update) < self.throttle_interval_secs:
            return

        build = yield self.master.data.get(("builds", buildid))
        runtime_state = self._runtime.get(state_key)
        if build is None or runtime_state is None:
            return
        self._initialize_runtime(build)

        steps = yield self.master.data.get(("builds", buildid, "steps"))
        if steps:
            self._refresh_runtime_from_steps(runtime_state, steps)

        runtime_state = self._runtime.get(state_key)
        text = yield self._build_progress_text(build, runtime_state)

        update_payload = {
            "channel": message_ref["channel"],
            "ts": message_ref["ts"],
            "text": text,
        }

        api_response = yield self._call_slack_api("chat.update", update_payload)
        if api_response is not None:
            self._last_step_update[state_key] = now

    @defer.inlineCallbacks
    def _post_failure_thread_message(self, build, message_ref):
        """Post a threaded reply with details of any failed steps, uploading logs as files."""
        buildid = build.get("buildid")
        try:
            steps = yield self.master.data.get(("builds", buildid, "steps"))
        except Exception as exc:
            logger.warn("Unable to fetch steps for failure thread (build {buildid}): {error}", buildid=buildid, error=exc)
            return

        if not steps:
            return

        # Failure=2, Exception=4 — skip SUCCESS(0), WARNINGS(1), SKIPPED(3), RETRY(5), CANCELLED(6)
        failed_steps = [s for s in steps if s.get("results") in (2, 4)]
        if not failed_steps:
            return

        lines = [":x: *Failed step details*"]

        for step in failed_steps:
            step_name = step.get("name") or "unknown"
            state_string = step.get("state_string") or ""
            lines.append("")
            lines.append("*Step: {name}*".format(name=step_name))
            if state_string:
                lines.append("State: {state}".format(state=state_string))

            step_urls = step.get("urls") or []
            if step_urls:
                url_parts = []
                for u in step_urls:
                    label = u.get("name") or "link"
                    href = u.get("url") or ""
                    if href:
                        url_parts.append("<{href}|{label}>".format(href=href, label=label))
                if url_parts:
                    lines.append("Links: " + "  ".join(url_parts))

        # Post the summary text first so the file(s) attach underneath it.
        summary_text = "\n".join(lines)
        summary_payload = {
            "channel": message_ref["channel"],
            "thread_ts": message_ref["ts"],
            "text": summary_text,
        }
        try:
            yield self._call_slack_api("chat.postMessage", summary_payload)
        except Exception as exc:
            logger.warn("Unable to post failure thread for build {buildid}: {error}", buildid=buildid, error=exc)
            return

        # Upload a log file for each failed step.
        if not self.failure_thread_upload_logs:
            return
        for step in failed_steps:
            step_name = step.get("name") or "unknown"
            stepid = step.get("stepid")
            if stepid is None:
                continue
            try:
                logs = yield self.master.data.get(("steps", stepid, "logs"))
            except Exception:
                continue
            for log in (logs or []):
                log_name = (log.get("name") or "").lower()
                if log_name not in ("stdio", "stderr", "output"):
                    continue
                logid = log.get("logid")
                if not logid:
                    continue
                try:
                    contents = yield self.master.data.get(("logs", logid, "contents"))
                except Exception:
                    continue
                raw = (contents or {}).get("content", "") if isinstance(contents, dict) else ""
                if not raw:
                    continue
                # Strip Buildbot per-line type prefix (o/e/h/i/s).
                clean_lines = []
                for log_line in raw.splitlines():
                    if log_line and log_line[0] in ("o", "e", "h", "i", "s"):
                        clean_lines.append(log_line[1:])
                    else:
                        clean_lines.append(log_line)
                log_content = "\n".join(clean_lines)
                safe_name = step_name.replace("/", "_").replace(" ", "_")
                filename = "build{buildid}_{step}_{log}.txt".format(
                    buildid=buildid, step=safe_name, log=log_name
                )
                yield self._upload_log_file_to_slack(
                    filename=filename,
                    content=log_content,
                    channel=message_ref["channel"],
                    thread_ts=message_ref["ts"],
                )
                break  # one log per step is enough

    @defer.inlineCallbacks
    def _upload_log_file_to_slack(self, filename, content, channel, thread_ts):
        """Upload a text file to a Slack thread using the v2 upload API."""
        try:
            import treq as _treq
        except ImportError:
            logger.warn("treq is not available; cannot upload log file to Slack")
            return

        content_bytes = content.encode("utf-8") if isinstance(content, str) else content

        # Step 1: obtain a pre-signed upload URL.
        url_response = yield self._call_slack_api("files.getUploadURLExternal", {
            "filename": filename,
            "length": len(content_bytes),
        })
        if url_response is None:
            return
        upload_url = url_response.get("upload_url")
        file_id = url_response.get("file_id")
        if not upload_url or not file_id:
            logger.warn("Slack did not return upload_url/file_id for {filename}", filename=filename)
            return

        # Step 2: PUT the file content to the pre-signed URL.
        try:
            upload_resp = yield _treq.put(
                upload_url,
                data=content_bytes,
                headers={b"Content-Type": b"text/plain; charset=utf-8"},
            )
            if upload_resp.code not in (200, 204):
                body = yield _treq.content(upload_resp)
                logger.warn(
                    "Log file upload failed ({code}): {body}",
                    code=upload_resp.code,
                    body=body,
                )
                return
        except Exception as exc:
            logger.warn("Log file upload PUT failed: {error}", error=exc)
            return

        # Step 3: finalise the upload and share it into the thread.
        yield self._call_slack_api("files.completeUploadExternal", {
            "files": [{"id": file_id}],
            "channel_id": channel,
            "thread_ts": thread_ts,
        })

    @defer.inlineCallbacks
    def sendMessage(self, reports):
        # We only use the first report, even if multiple are passed
        report = reports[0]
        # We also only report on the first build, even if multiple are present
        build = report["builds"][0]
        if self._should_skip_build(build):
            return
        self._initialize_runtime(build)
        postData = yield self.getBuildDetailsAndSendMessage(report)
        if not postData:
            return
        buildid = build.get("buildid")
        status_key = self._get_status_key(build)

        try:
            if self.use_web_api:
                resolved_channel = yield self._resolve_channel_id(postData.get("channel") or self.channel)
                if resolved_channel:
                    postData["channel"] = resolved_channel
                message_ref = yield self._get_message_ref(buildid)
                if message_ref is None:
                    logger.info("posting to Slack Web API chat.postMessage")
                    api_response = yield self._call_slack_api("chat.postMessage", postData)
                    if api_response is not None:
                        channel = api_response.get("channel") or postData.get("channel")
                        ts = api_response.get("ts")
                        if channel and ts:
                            yield self._set_message_ref(buildid, channel, ts)
                else:
                    update_payload = {
                        "channel": message_ref["channel"],
                        "ts": message_ref["ts"],
                        "text": postData.get("text", ""),
                    }
                    if "attachments" in postData:
                        update_payload["attachments"] = postData["attachments"]
                    logger.info("posting to Slack Web API chat.update")
                    yield self._call_slack_api("chat.update", update_payload)

                if status_key == "running":
                    self._ensure_periodic_progress_updates(buildid)
                else:
                    self._stop_periodic_progress_updates(buildid)
                    if self.failure_thread and build.get("results") in (2, 4):  # FAILURE or EXCEPTION
                        final_ref = yield self._get_message_ref(buildid)
                        if final_ref is not None:
                            yield self._post_failure_thread_message(build, final_ref)
            else:
                logger.info("posting to {url}", url=self.endpoint)
                yield self._post_webhook_message(postData)
        except Exception as e:
            sourcestamp = ((build.get("buildset") or {}).get("sourcestamps") or [{}])[0]
            logger.error(
                "Failed to send status for {repo} at {sha}: {error}",
                repo=sourcestamp.get("repository", "unknown"),
                sha=sourcestamp.get("revision"),
                error=e,
            )

        if status_key != "running":
            yield self._clear_build_state(buildid)
