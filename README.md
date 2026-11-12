# RepoPilot

RepoPilot is a local web application for understanding source repositories.

## Development

Use Python 3.12 or later. From the project directory, create a virtual environment.

```sh
python -m venv .venv
```

Activate it with `.venv\Scripts\Activate.ps1` in PowerShell or
`source .venv/bin/activate` in a POSIX shell. Install the development dependencies
and run the tests.

```sh
python -m pip install -e ".[dev]"
python -m pytest
```

Run the development server and visit `http://127.0.0.1:8000/health`.

```sh
python -m uvicorn repopilot.app:create_app --factory
```
