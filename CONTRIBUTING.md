# Getting involved

Hoki tracks Auckland's trains live and lets riders rate and report them. Help is welcome, and you don't need to write code.

## Ways to help
- **Ride and report.** Use the live site and report smells, crowding, damage, rowdy behaviour, or praise. Reports are what make the map useful.
- **Report bugs.** Open a [bug report](../../issues/new?template=bug_report.md). Include the unit (e.g. AM 701), your device and a screenshot if you can.
- **Suggest features.** Open a [feature request](../../issues/new?template=feature_request.md).
- **Fix or build something.** Pick an open issue, comment that you're on it, then send a pull request.

## Terms used
- **Unit** is a 3-car set, written `AM 701`.
- **Cars** are `AMP`, `AMT` and `AMA`. AMT is the middle car.
- A 6-car train is two coupled units.

## Code
- Python server in `src/`, single-page front end in `web/index.html`.
- Run the tests with `python src/test_feedback.py` before opening a PR.
- Keep PRs small and describe what a rider will see change.
- Never commit secrets, tokens or real rider data.
