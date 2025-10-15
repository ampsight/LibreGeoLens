# Repository Guidelines

## Project Structure & Module Organization

`libre_geo_lens/` hosts the plugin package:
- entrypoint `libre_geo_lens.py` wires the QGIS hooks,
- UI logic lives in `dock.py` and `custom_qt.py`
- Database in `db.py`
- Settings in `settings.py`

Icons and Qt resources sit under `libre_geo_lens/resources/`.
Repo-level `resources/media/` stores demo assets used by the README.

## Build, Test, and Development Commands
Do not handle this yourself since there's not automated testing. Testing must be done manually and visually by the user.

Do not try running any python commands. It won't work.

## Coding Style & Naming Conventions
Use 4-space indentation and snake_case for functions and module-level names; keep Qt widget classes in PascalCase(see `dock.py`).
When making code changes, keep in mind that the plugin is meant to work cross-platform (Windows, MacOS, Unix).

## Git Guidelines
Do not make any git commit, push or PRs. The user will do so manually after testing your changes.

## Security & Configuration Tips
Never commit API keys or AWS credentials, document required environment variables
(`OPENAI_API_KEY`, `GROQ_API_KEY`, `AWS_ACCESS_KEY_ID`, etc.) instead. Scrub private bucket names from logs or captures
shared in issues, and remind testers to restart QGIS after updating environment variables.
