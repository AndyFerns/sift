## What does this change?

<!-- A short description of the change and why it is needed. Link the issue if there is one. -->

## How was it tested?

<!-- Commands you ran and what you checked by hand. -->

## Checklist

- [ ] `pytest` passes
- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `mypy` passes
- [ ] If `web/` changed: `npm run typecheck` and `npm test` pass in `web/`
- [ ] New or changed behaviour has tests
- [ ] Scanning and cleanup logic lives in `sift.core`, not in an interface
- [ ] Tests that delete anything only touch pytest temporary directories
- [ ] README / CHANGELOG updated if behaviour changed

## Safety impact

<!--
Does this touch detection, path validation or deletion? If so, explain what could go wrong
and how the change avoids it. Write "none" if it does not apply.
-->
