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
        self.use_web_api = bool(use_web_api)
        self.slack_token = slack_token
        self.api_base = api_base
        self._state_class_name = "SlackStatusPush"
        self._state_object_name = self._build_state_object_name()
        self._state_object_id = None
        self.throttle_interval_secs = max(float(throttle_interval_secs), 0.0)
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

        yield self._ensure_state_object_id()

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
                builder_name = (build.get("builder") or {}).get("name")
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
            builder_name = (build.get("builder") or {}).get("name", "unknown")
            attachments.append(
                {
                    "title": "Build #{buildid}".format(buildid=build.get("buildid", "?")),
                    "title_link": build.get("url", ""),
                    "fallback": "Build #{buildid}".format(buildid=build.get("buildid", "?")),
                    "text": "Builder: *{builder}*\\nStatus: *{status}*".format(
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
            self._runtime[state_key] = {
                "known_steps": set(),
                "finished_steps": set(),
                "current_step": None,
                "start_time": build.get("started_at") or build.get("start_time") or time.time(),
                "builderid": (build.get("builder") or {}).get("builderid"),
            }
        steps = build.get("steps") or []
        runtime_state = self._runtime[state_key]
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

        yield self._send_step_progress_update(buildid)

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
    def _estimate_eta_seconds(self, build, runtime_state, elapsed):
        builderid = runtime_state.get("builderid")
        buildid = build.get("buildid")
        if builderid is not None:
            try:
                history = yield self.master.db.builds.getBuilds(
                    builderid=builderid,
                    limit=self.eta_history_limit,
                )
                durations = []
                perfect_match_durations = []
                best_match_durations = []
                best_match_score = -1
                current_props = self._extract_eta_properties(build)
                for hist_build in history:
                    hist_buildid = hist_build.get("buildid")
                    if hist_buildid == buildid:
                        continue
                    start_time = hist_build.get("start_time")
                    complete_time = hist_build.get("complete_time")
                    if start_time is None or complete_time is None:
                        continue
                    duration = float(complete_time) - float(start_time)
                    durations.append(duration)

                    if not current_props:
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
                    return max(int(median_duration - elapsed), 0)
            except Exception as exc:
                logger.warn("Unable to compute historical ETA for build {buildid}: {error}", buildid=buildid, error=exc)

        known_steps = max(len(runtime_state.get("known_steps", [])), 1)
        finished_steps = len(runtime_state.get("finished_steps", []))
        if finished_steps > 0:
            projected_total = (elapsed / float(finished_steps)) * known_steps
            return max(int(projected_total - elapsed), 0)
        return None

    @defer.inlineCallbacks
    def _build_progress_text(self, build, runtime_state):
        known_steps = max(len(runtime_state.get("known_steps", [])), 1)
        finished_steps = len(runtime_state.get("finished_steps", []))
        progress_ratio = min(float(finished_steps) / float(known_steps), 1.0)
        done_blocks = int(progress_ratio * self.progress_bar_width)
        pending_blocks = self.progress_bar_width - done_blocks
        progress_bar = "[{done}{pending}] {pct:3.0f}%".format(
            done="#" * done_blocks,
            pending="-" * pending_blocks,
            pct=progress_ratio * 100.0,
        )

        now = time.time()
        elapsed_seconds = max(now - float(runtime_state.get("start_time", now)), 0.0)
        eta_seconds = yield self._estimate_eta_seconds(build, runtime_state, elapsed_seconds)
        current_step = runtime_state.get("current_step") or "waiting"

        lines = [
            "{emoji} Build in progress".format(emoji=STATUS_EMOJIS["running"]),
            "Step: *{current}* ({done}/{total})".format(
                current=current_step,
                done=finished_steps,
                total=known_steps,
            ),
            "Progress: {bar}".format(bar=progress_bar),
            "Elapsed: {elapsed}".format(elapsed=self._format_duration(elapsed_seconds)),
        ]
        if eta_seconds is not None:
            lines.append("ETA: {eta}".format(eta=self._format_duration(eta_seconds)))
        if build.get("url"):
            lines.append("Details: {url}".format(url=build["url"]))
        return "\\n".join(lines)

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
    def _send_step_progress_update(self, buildid):
        if not self.use_web_api:
            return
        state_key = self._state_key(buildid)
        message_ref = yield self._get_message_ref(buildid)
        if message_ref is None:
            return
        now = time.time()
        last_update = self._last_step_update.get(state_key)
        if last_update is not None and (now - last_update) < self.throttle_interval_secs:
            return

        build = yield self.master.data.get(("builds", buildid))
        runtime_state = self._runtime.get(state_key)
        if build is None or runtime_state is None:
            return
        self._initialize_runtime(build)
        runtime_state = self._runtime.get(state_key)
        text = yield self._build_progress_text(build, runtime_state)

        update_payload = {
            "channel": message_ref["channel"],
            "ts": message_ref["ts"],
            "text": text,
        }
        if self.attachments:
            update_payload["attachments"] = yield self.getAttachments(build)

        api_response = yield self._call_slack_api("chat.update", update_payload)
        if api_response is not None:
            self._last_step_update[state_key] = now

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
