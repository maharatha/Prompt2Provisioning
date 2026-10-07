# Prompt-to-Provisioning Planner

This prototype will turn a plain-language infrastructure request into a reviewable deployment plan. It is being built in small phases. This phase provides a FastAPI application with a health endpoint and a pytest setup.

**Safety:** this project does not access a real cloud, does not deploy infrastructure, and uses synthetic data only.

## Repository structure

```
├── AGENTS.md
├── app/
│   ├── __init__.py
│   └── main.py
├── ui/
│   └── .gitkeep
├── tests/
│   ├── __init__.py
│   └── test_health.py
├── requirements.txt
├── requirements-dev.txt
├── pytest.ini
├── .gitignore
├── .env.example
└── README.md
```

## Prerequisite

Python 3.12

## Local commands

Create a virtual environment:

```
python -m venv .venv
```

Windows PowerShell:

```
.\.venv\Scripts\Activate.ps1
```

macOS / Linux:

```
source .venv/bin/activate
```

Install dependencies:

```
python -m pip install -r requirements-dev.txt
```

Run the API:

```
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Then open `http://127.0.0.1:8000/health`.

Run tests:

```
python -m pytest
```

Implementation is intentionally incremental. Later phases add planning, policy, pricing, approval, the UI, and dry-run artifact generation.
