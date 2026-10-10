# Style profiles — taste as data

The `style/` directory formalizes video taste as machine-checkable rules.
Every rule was learned from a real incident; every incident is recorded with
its evidence. The loop:

```
incident → rule (rationale + evidence + machine check) → vcompose lint → fix
```

`vcompose lint comp.json [--profile vandal-raw]` renders the composition and
checks it against the active profile. Errors exit non-zero (block the render);
warnings print and let it through. Run lint before every final render —
`render` does not lint for you, deliberately: lint is the review pass, render
is the print.

## Profiles

- `default.yaml` — universal rules every profile inherits. Not taste: a blank
  viewport reads as broken video in any style.
- `vandal-raw.yaml` — Nate's profile, the reference implementation other
  users fork. Inherits `default`, adds his taste (annotation stays clean,
  content is always graded, glitch fires on hits).

A profile is YAML:

```yaml
name: vandal-raw
inherits: default          # merge parent rules; same id overrides
rules:
  - id: no-dead-viewport
    severity: error        # error | warn
    rationale: "..."       # human paragraph: why this exists
    incident:              # the evidence that created it
      file: <path>         # incident record (what/where/when)
      detail: "..."
    check:                 # machine params
      type: dead_viewport
      dark_below: 18
      ...
```

Rule check types: `dead_viewport`, `accidental_freeze`, `near_white`
(frame analysis, needs a render); `annotation_effects`, `content_grade`,
`glitch_hits` (static, JSON only — no render needed).

## The intentional flag

The linter cannot infer intent. The founding incident's Etsy half-load
("nothing." beat, B4 ~14–16s) was deliberate emptiness — so compositions
mark deliberate beats explicitly:

```json
{"id": "dead-end-card", "type": "image", "intentional": true, ...}
```

`intentional: true` on a layer exempts its `[in, out]` range from the frame
rules. Declared `holds[]` are intent made data and are always exempt from
the freeze rule. When in doubt, mark it — an unmarked violation is a bug
report, a marked one is a decision.

## Forking a profile

1. Copy `vandal-raw.yaml` → `<your-name>.yaml`.
2. Set `inherits: default` (keep the universal gates) or `inherits: null`.
3. Edit rules: change severities, tune check params, add your own with
   `incident.detail` pointing at what taught you.
4. `vcompose lint comp.json --profile <your-name>`.

Profiles are taste, not personal data — they ship in the public repo.
Incident files may reference private project paths; keep those in the
private content-pipeline, not here.
