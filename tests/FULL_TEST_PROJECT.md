# Full Test Project

This project includes a complete automated test set for core, UI, server, extension edge cases, torrent behavior, and concurrency stress.

## One-command run

From project root:

```powershell
./scripts/run_full_test_project.ps1
```

Verbose run:

```powershell
./scripts/run_full_test_project.ps1 -VerboseOutput
```

## Report output

The runner writes a JUnit XML report to:

- `test-reports/junit.xml`

## What is covered

- Downloader/chunking behavior
- Network retry/error handling
- Scheduler behavior
- Server endpoints/debug APIs
- UI components/dialogs
- Media extraction and quality selection flow
- Torrent manager scenarios
- Extension edge-case static checks
- Concurrency stress queue behavior

## Direct pytest alternative

```powershell
.venv/Scripts/python.exe -m pytest tests -q
```
