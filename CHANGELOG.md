version 0.3.0
-------------

- add Slack Bot API mode using `chat.postMessage` and `chat.update`
- support one message per build with continuous step-based updates
- add per-build progress bar, elapsed time, and ETA estimation
- persist Slack message references in Buildbot DB state
- keep incoming webhook mode for backward compatibility

version 0.2.4
-------------

- [issue #8] fix checkConfig and make it more permissive

version 0.2.3
-------------

- [issue #4 and #6] fix setup requirements

version 0.2.1
-------------

- [issue #1] allow notification even if there is no source stamp (handle alwaysUseLatest)

version 0.2.0
-------------

- use attachments
- use Slack colors
- hide attachment fields for trigerred builders

version 0.1.0
-------------

Initial version with basic notifications using only text from Slack webhook 
