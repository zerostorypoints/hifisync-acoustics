# hifisync-acoustics

A room acoustics measurement engine: it analyses measurements of a listening room
and produces results that other software, such as Home Assistant integrations,
can consume.

The design is described in the `zerostorypoints/domosenso` repository at
`docs/superpowers/specs/2026-11-22-room-acoustics-design.md`.

## Development

```
uv sync
uv run pytest -q
```

Requires Python 3.13 or newer.

## Licence

Apache-2.0. See `LICENSE` and `NOTICE`.
