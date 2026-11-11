# Changelog

## 0.2.0 — unreleased

- The overlay speaks Spanish with `?lang=es`: the state phrases, the steps, the header ("7 crewmates · 1 espera"), the ages ("ahora", "hace 5 min"), the captain's note, "un crewmate en marcha" and "El secondmate de <project>". Without the parameter, or with any other value, the overlay is the same as in 0.1.0.
- `fleetnotes-feed.v1` is unchanged. The overlay translates the feed's fixed `"Secondmate of <project>"` form itself, so 0.1.0 readers and writers keep working; [docs/contracts.md](docs/contracts.md) now says the form is fixed.

## 0.1.0 — unreleased

First public version.

- Captain's notes: `add`, `pin`, `list`, `rm`, `clear`, `off`, `on`, with a built-in blocklist and your own blocked phrases.
- `fleetnotes serve`: the overlay and its two JSON files on 127.0.0.1.
- Optional Firstmate feed: `fleet allow|deny|label|unlabel|plan|watch`, closed by default, with secondmate homes and remote machines over ssh.
- File contracts `fleetnotes-notes.v1` and `fleetnotes-feed.v1`.
