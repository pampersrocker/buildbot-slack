[![PyPI version](https://badge.fury.io/py/buildbot-slack.svg)](https://badge.fury.io/py/buildbot-slack)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

Buildbot plugin to publish status on Slack
==========================================

This Buildbot plugin sends messages to Slack for build lifecycle events.

It supports two transport modes:

- Incoming Webhook mode: posts new messages only.
- Slack Bot API mode: posts one message per build and edits it continuously as steps progress.

This plugin is based on many other reporter plugins made for Slack

Contributions are welcome!

## Install

### via pip

```
pip install buildbot-slack
```

## Setup

### Which mode should I use?

- If you want one live-updating message per build, use Bot API mode.
- If you only need start/finish posts, Webhook mode is enough.

Incoming webhooks cannot edit previously sent messages, so they cannot implement a single continuously updated build message.

Create a new incoming webhook in your slack account. (see https://api.slack.com/tutorials/slack-apps-hello-world)

Then in your master.cfg, add the following:

```
from buildbot.plugins import reporters
c['services'].append(reporters.SlackStatusPush(
    endpoint=<YOUR_WEBHOOK_ENDPOINT>,
))
```

### Bot API mode (single message + progress updates)

Create a Slack app with a bot token and grant at least these scopes:

- `chat:write`
- `chat:write.public` (only if posting to channels the bot is not yet a member of)

Install the app into your workspace, invite the bot to the target channel, then configure:

```
from buildbot.plugins import reporters

c['services'].append(reporters.SlackStatusPush(
    use_web_api=True,
    slack_token=<YOUR_XOXB_TOKEN>,
    channel="#builds",
    throttle_interval_secs=4,
    eta_history_limit=10,
    progress_bar_width=12,
))
```

In this mode:

- Build start creates a message.
- Step events update the same message with progress and ETA.
- Build finish updates the same message to final status.

### Options

Common options:

```
  use_web_api = False
  slack_token = None
  channel = None
  username = None
  attachments = True
  throttle_interval_secs = 4
  eta_history_limit = 10
  eta_match_properties = None
  eta_exclude_properties = None
  progress_bar_width = 12
```

Message references for bot-mode updates are persisted in Buildbot DB state (`master.db.state`) by default.

ETA matching options:

- `eta_match_properties`: optional allow-list of property names to use for matching historical builds.
- `eta_exclude_properties`: optional deny-list of property names to ignore during matching.

When `eta_match_properties` is not set, all available properties are considered except default excluded volatile keys and keys in `eta_exclude_properties`.

Webhook mode options:

```
  endpoint = <YOUR_WEBHOOK_ENDPOINT>
```

Bot mode options:

```
  api_base = "https://slack.com/api"
```

### Backward compatibility

Existing webhook configurations continue to work unchanged.
To enable single-message build reporting, switch to Bot API mode.

Have fun!
