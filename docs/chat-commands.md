# In-Chat Commands

These commands work inside chat channels and interactive agent sessions:

| Command | Description |
|---------|-------------|
| `/new` | Stop current task and start a new conversation |
| `/compact` | Summarize the current conversation context while keeping saved chat history |
| `/stop` | Stop the current task |
| `/restart` | Restart the bot |
| `/status` | Show bot status |
| `/model` | Show the current model and available model presets |
| `/model <preset>` | Switch and persist the model preset for the current session |
| `/dream` | Run Dream memory consolidation now |
| `/dream-log` | Show the latest Dream memory change |
| `/dream-log <sha>` | Show a specific Dream memory change |
| `/dream-restore` | List recent Dream memory versions |
| `/dream-restore <sha>` | Restore memory to the state before a specific change |
| `/dream-prompt` | Show how Dream is being guided for memory |
| `/dream-prompt init` | Create an editable Dream memory guide at `prompts/dream.md` |
| `/skill` | List enabled skills and their descriptions |
| `/trigger` | Show local trigger usage |
| `/trigger <name>` | Create a named local trigger for the current chat/session |
| `/pairing` | List pending pairing requests |
| `/pairing approve <code>` | Approve a pairing code |
| `/pairing deny <code>` | Deny a pending pairing request |
| `/pairing revoke <user_id>` | Revoke a previously approved user on the current channel |
| `/pairing revoke <channel> <user_id>` | Revoke a previously approved user on a specific channel |
| `/git` | Commit the workspace, pull with rebase, then push |
| `/git <message>` | Same sync with an explicit commit message |
| `/read <filename>` | Print a Markdown file from the workspace |
| `/help` | Show available in-chat commands |

## Commands and file paths

The gateway treats a message as a command only when its first word matches a registered command name. Names are case-insensitive. Arguments must follow the command's supported form.

Other text goes to the agent unchanged. This includes `/tmp`, `/home/user/project`, and `/Users/alice/report.md`. Unknown names such as `/neaw` also go to the agent; the gateway no longer returns a spelling suggestion. Use `/help` or the command menu to find a command.

In the WebUI, Enter queues ordinary text while a response is running. Paths follow the same rule. Registered commands still go to the gateway immediately. If the command list is unavailable, slash-prefixed input goes to the gateway immediately, so commands such as `/stop` do not wait in the queue.

A path can have the same name as a command. For example, `/new` starts a new conversation. To discuss that directory, write `/new/` or `Look at /new`. Chat platforms can apply their own command rules before a message reaches the gateway.

## Workspace Git Sync

`/git` backs up or shares the workspace by committing it and syncing with the
remote:

```text
/git
/git Fix the onboarding notes
```

The steps always run in this order: stage everything the workspace's own ignore
rules allow, commit, `git pull --rebase`, then `git push`. Without an explicit
message the commit is titled `nanobot sync @ YYYY-MM-DD HH:MM:SS` in the
configured timezone. An explicit message is used verbatim, including spaces,
line breaks, and text that starts with `-`.

The command runs in the workspace that applies to the current chat: a chat
bound to its own project syncs that project, and every other chat syncs the
configured default workspace. Nothing outside that workspace is touched.

`/git` acknowledges immediately and keeps working in the background, so other
chats stay responsive while a slow remote answers. The result is reported back
in the same chat:

- `Git ✓ synced` — with the command transcript for each step. A clean tree with
  nothing to commit is a success, not an error.
- `Git ✗ failed (exit N)` — with the failing step, the transcript, and a
  readable explanation. The sequence stops at the first real failure, so a
  broken commit is never pushed.

Failures are explained in plain terms, including: the workspace is not a Git
repository, no remote or upstream is configured, the remote is unreachable,
credentials or push rights were rejected, and a rebase conflict. Each Git step
is time-limited and never waits for interactive input, so credential prompts or
unknown host keys fail fast instead of hanging. A rebase conflict leaves the
workspace mid-rebase, and the reply says how to continue or abort.

Credentials are never echoed: tokens and passwords inside remote URLs are
redacted from the output.

**Access and availability.** `/git` pushes workspace content to a remote, so it
follows the same authorization as other chat commands: only senders your channel
accepts — the allowlist, a `*` allow-all entry, or an approved pairing — can run
it. It is available in every channel that supports slash commands (including the
WebUI). Ignore rules are respected as-is: `/git` stages what the workspace's own
`.gitignore` allows and never weakens it to make a sync succeed.

## Reading Workspace Markdown

`/read` prints a Markdown file from the workspace:

```text
/read NANOBOT.md
/read docs/notes.md
```

Paths resolve against the current chat's workspace and support subdirectories.
Only `.md` files are served, and the file must stay inside that workspace even
when `tools.restrictToWorkspace` is disabled — absolute paths, `..` traversal,
and symlinks that point outside are refused. Large files are truncated with a
notice, and content that is not UTF-8 text is reported rather than dumped. The
command is read-only and always replies with a readable message; it never
changes a file or the workspace boundary. Like other commands, `/read` is
available only to senders your channel accepts, in every channel that supports
slash commands.

## Pairing

When someone sends a DM to the bot and isn't on the allowlist — whether it's a new user or an existing user on a new channel — nanobot automatically replies with a **pairing code** (like `ABCD-EFGH`) that expires in 10 minutes. To grant them access:

```text
/pairing approve ABCD-EFGH
```

To see who's waiting, use `/pairing`. To remove someone later, use `/pairing revoke <user_id>` — you can find user IDs in the `/pairing list` output.

See [Configuration: Pairing](./configuration.md#pairing) for the full setup guide.

## Model Presets

Use `/model` to inspect the current runtime model:

```text
/model
```

The response shows which model this conversation is using and the names of models you can switch to. `default` is also available and uses the instance's default model settings.

To switch presets for future turns:

```text
/model fast
/model deep
/model default
```

Switching changes the model used for future replies in this conversation and keeps that choice for later turns, including after a restart. It does not change other conversations or interrupt a reply already in progress. To add or configure model choices, see [Configuration: Model presets](./configuration.md#model-presets).

## Local triggers

Use `/trigger <name>` when a local script or another service should be able to send a message into the current chat/session later. A name is required; plain `/trigger` only shows the usage hint.

Create the trigger from the chat where future messages should arrive:

```text
/trigger PR review
```

nanobot replies with a trigger ID and a command shaped like:

```bash
nanobot trigger trg_8K4P2Q9X "Review PR #4502"
```

Replace `"Review PR #4502"` with the message you want nanobot to receive. The trigger is bound to the session where it was created, so the message goes back to that same chat. Keep `nanobot gateway` running so trigger messages can be delivered. The trigger message starts an automation turn recorded in that session with the message you passed to the CLI; it is not treated as a normal user message. If that session is already running a turn, the trigger waits until the session is idle instead of being injected into the active turn.

Trigger deliveries are stored in the workspace until their linked agent turn finishes successfully. If the gateway exits after claiming a delivery but before the turn completes, the next gateway start requeues that delivery. This is an at-least-once local queue: a delivery may run more than once if the process exits at the wrong time, so external scripts should make repeated trigger messages safe. If the delivery reaches the agent and the agent turn fails, the delivery is marked failed in Automations instead of retrying forever.

For longer or generated content, omit the message argument and pipe stdin:

```bash
printf '%s\n' "Review the latest failed CI job" | nanobot trigger trg_8K4P2Q9X
```

If an external webhook should wake nanobot up, run your own small webhook service and have it call the trigger command after it builds the final message:

```bash
nanobot trigger <trigger-id> "<message>"
```

If you run multiple nanobot instances, pass the same config or workspace selector used by the gateway:

```bash
nanobot trigger --config ./bot-a/config.json trg_8K4P2Q9X "Nightly report"
nanobot trigger --workspace ./bot-a/workspace trg_8K4P2Q9X "Nightly report"
```

Manage triggers from the WebUI Automations view. You can search, pause/resume, rename, delete, and copy the trigger command there. A session may have multiple triggers, just like it may have multiple scheduled automations.

See [Automations](./automations.md) for how local triggers fit with scheduled automations, heartbeat, and gateway delivery.

## Periodic Tasks

Periodic background checks are driven by `HEARTBEAT.md` in your workspace (`~/.nanobot/workspace/HEARTBEAT.md`). When `nanobot gateway` starts, it registers a protected heartbeat cron job by default. Every 30 minutes, that job checks the file; if it finds tasks under `## Active Tasks`, the agent executes them and delivers only results that pass the notification gate to your most recently active chat channel. If there are no active tasks, or the result is routine with nothing useful to report, the heartbeat is skipped silently.

Use heartbeat for recurring checks that should usually stay quiet. User-created cron jobs are different: they run as scheduled turns in the chat/session where they were created and normally deliver the result back to that channel.

**Setup:** edit `~/.nanobot/workspace/HEARTBEAT.md` (created automatically by `nanobot onboard`):

```markdown
## Active Tasks

- Check weather forecast and notify me only if storms are expected
- Scan inbox for urgent emails and notify me if any are found
```

The agent can also manage this file itself - ask it to "add a periodic background check" or "check this periodically but only notify me if something changes" and it will update `HEARTBEAT.md` for you. Completed tasks should be deleted from the file, not moved to another section.

You can change the interval or disable the built-in heartbeat in `~/.nanobot/config.json`:

```json
{
  "gateway": {
    "heartbeat": {
      "enabled": true,
      "intervalS": 1800
    }
  }
}
```

The heartbeat job is visible in `cron(action="list")` as `heartbeat`, but it is system-managed and cannot be removed with the `cron` tool. To stop it, set `gateway.heartbeat.enabled` to `false` and restart the gateway.

> **Note:** The gateway must be running (`nanobot gateway`) and you must have chatted with the bot at least once so it knows which channel to deliver to.
