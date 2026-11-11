# Security policy

## Reporting a problem

Please report security problems privately, through GitHub's **Report a vulnerability** button on the repository's Security tab. Do not open a public issue for them.

This is a personal project: there is no response-time commitment, but private reports are read first.

## What counts

fleetnotes exists to keep private text off a live stream, so these are security problems:

- Text that reaches `notes.json` or `fleet.json` without passing the rules: a task id, a title or label of a project that is not allowed, a blocked word, a path, an email address or a link.
- `fleetnotes serve` answering anything other than the overlay and its two JSON files, or listening beyond the configured address.
- Script or HTML injection into the overlay.
- Command injection through the config, the rules file or a remote machine's settings, including the ssh command line.
- The CLI or the watcher's log printing task ids, titles, labels or blocked text (`fleet plan --ids` prints ids on purpose).

Not in scope: what you choose to allow or label yourself, your streaming software, Firstmate itself, and the fonts the overlay loads from Google Fonts.

## Supported versions

Only the latest release gets fixes.
