# Contributing

Thanks for taking a look. fleetnotes is a small personal project: I read issues and pull requests when I can, with no promised response time.

## Before you start

- For anything bigger than a fix, open an issue first and describe the change, so we can agree on it before you spend time on it.
- fleetnotes stays dependency-free: standard library only, Python 3.11 or newer, macOS and Linux.
- Privacy comes first. The public files must never carry text the user did not approve, and a change that relaxes a filter needs a test that shows why it is safe.

## Running the tests

```sh
python3 -m unittest discover -s tests -t . -v
```

The suite writes only to temporary folders and never reads your real config. The overlay tests drive a headless Chrome or Chromium and are skipped when none is found; set `CHROME_BIN` to point at one. On Linux CI, `FLEETNOTES_CHROME_ARGS=--no-sandbox` passes extra flags.

## Style

- Code, comments, messages and docs are in English.
- Keep functions small and the behaviour deterministic. No network access except the ssh read of a configured remote machine.
- Messages and logs name rules and counts, never the text that was blocked, task ids or paths.
- Test fixtures use made-up names only: no real projects, hosts, paths, people or tokens.

## Sign-off

Contributions are accepted under the [Developer Certificate of Origin](https://developercertificate.org/). Sign off each commit with `git commit -s`, which adds a `Signed-off-by:` line with your name and email. You can use your GitHub `noreply` address.

## Pull requests

- One topic per pull request, with tests.
- CI must pass. It runs the suite on Ubuntu and macOS with Python 3.11 to 3.14.
- Do not include generated files, real notes, rules files or logs.
