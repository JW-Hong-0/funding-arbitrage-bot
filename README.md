# Funding Arbitrage Bot

Modular funding arbitrage bot for GRVT, Lighter, and Variational.

## Setup

1) Create a virtual environment

```
python -m venv .venv
```

2) Install dependencies

```
.venv/bin/pip install -r requirements.txt
```

3) Install SDKs (local paths recommended)

```
.venv/bin/pip install -e /path/to/grvt-pysdk --no-build-isolation
.venv/bin/pip install -e /path/to/lighter-python --no-build-isolation
```

4) Add env file

Create `private/Funding_Arbitrage.env` with the required keys.

## Run

```
.venv/bin/python -m src.main_modular
```

## Logs

Logs are created in `logs/session_YYYYMMDD_HHMMSS/`.
