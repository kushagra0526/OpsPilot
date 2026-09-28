# OpsPilot developer commands. On Windows use `mingw32-make <target>` (or install GNU make).

ifeq ($(OS),Windows_NT)
	VENV_PY := .venv/Scripts/python.exe
	SYS_PY := py -3.13
else
	VENV_PY := .venv/bin/python
	SYS_PY := python3
endif

.PHONY: setup seed run cli test eval lint site site-check

setup:
	$(SYS_PY) -m venv .venv
	$(VENV_PY) -m pip install --upgrade pip
	$(VENV_PY) -m pip install -r requirements.txt
	$(VENV_PY) -m pip install -e . --no-deps

seed:
	$(VENV_PY) scripts/seed.py

run:
	$(VENV_PY) -m streamlit run app/streamlit_app.py

cli:
	$(VENV_PY) cli.py chat --session session1

test:
	$(VENV_PY) -m pytest

eval:
	$(VENV_PY) evals/run_evals.py

lint:
	$(VENV_PY) -m ruff format .
	$(VENV_PY) -m ruff check . --fix


site:
	cd site && npm install && npm run dev

site-check:
	cd site && npm install && npm run check
