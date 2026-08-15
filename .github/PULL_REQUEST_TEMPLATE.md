<!-- Keep this short. The commit messages carry the detail; this is the summary
     a reviewer reads first. -->

## What this changes

<!-- One or two sentences. What a group, an operator or a developer would
     notice. -->

## Why

<!-- The problem it solves. Link the issue if there is one: "Closes #123". -->

## Compatibility

<!-- v1 compatibility is not negotiable — a command that changes its name, its
     permissions or its reply is a regression. Tick what applies. -->

- [ ] No command changed its trigger, wording, permissions or feature switch
- [ ] Behaviour did change, and the reason is recorded in `docs/contracts/`
- [ ] Not applicable (docs, infrastructure, tooling)

## Checks

```
python scripts/cb.py check    # lint, types, tests, benchmarks, spec consistency
```

- [ ] `cb.py check` passes locally
- [ ] New behaviour has a scenario in `qa/features/` (see [Development](https://cookiebot-team.github.io/cookiebot-telegram-bot/docs/development))
- [ ] Docs updated if a command, setting or reply changed
