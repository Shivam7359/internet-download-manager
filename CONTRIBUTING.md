# Contributing to IDM

## Ways to Contribute
- Report bugs
- Suggest features
- Fix bugs (check Issues labeled `good first issue`)
- Improve documentation
- Add tests

## Development Setup
1. Fork the repository
2. Clone your fork
3. Install dependencies
4. Run tests
5. Make changes
6. Submit pull request

## Branch Naming
- bug/description
- feature/description
- docs/description
- perf/description

## Commit Message Format
type(scope): short description

Types: feat, fix, docs, perf, refactor, test, chore

Examples:
feat(extension): add orange badge for redirect buttons
fix(downloader): resolve dl.php filename correctly
perf(storage): batch speed telemetry writes

## Pull Request Rules
- One feature/fix per PR
- Include tests for bug fixes
- Update CHANGELOG.md
- Update README if needed

## Code Style
- Python: black formatter, snake_case
- JavaScript: camelCase, 2 space indent
- Comments required for non-obvious logic