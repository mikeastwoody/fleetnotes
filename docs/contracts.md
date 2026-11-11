# File contracts

The overlay reads two JSON files from the output folder every 5 seconds. Both are public by design: anything in them may appear on stream. Writers must use an atomic replace (write a temp file, then rename), as fleetnotes does.

A reader must ignore a file whose `schema` it does not know. The overlay fails closed: an unreadable `notes.json` hides everything, and an unreadable, foreign or stale `fleet.json` hides the fleet notes.

## `notes.json` — `fleetnotes-notes.v1`

The captain's notes, written by `fleetnotes add`, `pin`, `rm`, `clear`, `off` and `on`.

```json
{
  "schema": "fleetnotes-notes.v1",
  "updated": "2026-11-04T12:39:50Z",
  "off": false,
  "notes": [
    {"key": "a1b2c3", "text": "Today: notes that update themselves", "pinned": true, "expires": null},
    {"key": "d4e5f6", "text": "Chat: what is a crewmate?", "pinned": false, "expires": "2026-11-04T13:09:50Z"}
  ]
}
```

| Field | Meaning |
|---|---|
| `off` | `true` hides the whole board, fleet included (`fleetnotes off`). |
| `notes[].key` | Opaque id of the note. |
| `notes[].text` | One line, at most 90 wide (an emoji or wide character counts 2). Already checked against the blocklist. |
| `notes[].pinned` | The overlay shows the newest pinned note and the newest temporary one. |
| `notes[].expires` | UTC time when a temporary note stops showing; `null` for no expiry. |

## `fleet.json` — `fleetnotes-feed.v1`

The public projection of a Firstmate fleet, written by `fleetnotes fleet watch`. It is built from Firstmate's read-only view (`bin/fm-fleet-snapshot.sh --json`, schema `fm-fleet-snapshot.v1`, which belongs to Firstmate) and holds only these keys; `fleetnotes.fleet.validate_feed` rejects anything else.

```json
{
  "schema": "fleetnotes-feed.v1",
  "generated": "2026-11-04T12:39:50Z",
  "summary": {"running": 4, "validating": 1, "needs_captain": 1, "problem": 0, "paused": 1, "done_recent": 0},
  "notes": [
    {"key": "3f9c0a1b7e", "project": "Driftwood", "who": "Secondmate of Driftwood",
     "label": "Slack integration", "label_from": "title", "kind": "ship", "state": "validating",
     "step": "ci", "started": "2026-11-04T12:22:00Z", "changed": "2026-11-04T12:38:00Z"}
  ]
}
```

| Field | Meaning |
|---|---|
| `generated` | When the fleet was read. The overlay hides the fleet notes once this is more than 180 s old. |
| `summary` | Anonymous counts of every ship and scout task, including those of projects that are not allowed. |
| `notes` | At most 10, ordered by state (`needs_captain`, `problem`, `done`, `validating`, `running`, `paused`), newest change first. |
| `key` | HMAC-SHA256 of the task id with a local salt, first 10 hex characters. Stable, opaque. |
| `project` | The allowlisted public name (up to 16 characters). |
| `who` | `project`, `"Secondmate of <project>"` or `"<machine>: <project>"`; always ends with `project`. The words `Secondmate of ` are a fixed English form, not display text: a writer must not translate them, and a reader may show them in its own language (see below). |
| `label`, `label_from` | What the task does: the approved label (`"label"`), the cleaned backlog title (`"title"`), or `null`/`null` when nothing public is left. Up to 60 characters. |
| `kind` | `ship` or `scout`. |
| `state` | `running`, `validating`, `needs_captain`, `problem`, `paused` or `done` (shown for 10 minutes). |
| `step` | Optional: `review`, `test`, `lint`, `docs`, `push`, `pr`, `ci` or `rebase`. |
| `position`, `total` | Optional non-negative integers (a queue position while validating). |
| `started`, `changed` | UTC times of the crewmate's spawn and of its last status event, or `null`. |

Task ids, backlog text, status messages, paths, pull requests, secondmate ids and ssh hosts are read only to decide what to publish; they never appear in this file.

### Languages

The feed is the same whatever language the overlay shows: its vocabulary (`state`, `step`, `kind`) is closed and the overlay turns it into phrases. With `?lang=es` the overlay shows a `who` that is exactly `"Secondmate of " + project` as `"El secondmate de " + project`. Any other `who`, such as `"<machine>: <project>"` or a project whose public name itself starts with those words, is shown as it is. Since 0.2.0 depends on no new key, a 0.1.0 overlay or `validate_feed` still reads a 0.2.0 feed, and the other way round.
