# slot-machine: develop on the Mac, run on the Pi.
PI      ?= yarikp@yarikos.local
SSH_KEY ?= $(HOME)/.ssh/pi
SSH     := ssh -i $(SSH_KEY) -o IdentitiesOnly=yes
PYTHON  ?= python3
VENV    := .venv
APP     := dist/slot-machine
REMOTE  := .local/bin/slot-machine
LOG     := .local/state/slot-machine/watch.log

.PHONY: help dev test lint fmt build clean deploy cron-on cron-off smoke-pi status logs

help:
	@echo "make dev       create .venv with pytest and ruff"
	@echo "make test      run the test suite"
	@echo "make lint      ruff check"
	@echo "make build     build the single-file zipapp $(APP)"
	@echo "make deploy    build and copy it to $(PI) (cron untouched)"
	@echo "make cron-on   install the hourly cron entry on the Pi"
	@echo "make cron-off  remove it"
	@echo "make smoke-pi  run the deployed app on the Pi against fake servers"
	@echo "make status    slot-machine --status on the Pi"
	@echo "make logs      tail the Pi's log"

$(VENV)/bin/pytest: requirements-dev.txt
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -q --upgrade pip
	$(VENV)/bin/pip install -q -r requirements-dev.txt
	@touch $@

dev: $(VENV)/bin/pytest

test: dev
	$(VENV)/bin/pytest

lint: dev
	$(VENV)/bin/ruff check src tests

fmt: dev
	$(VENV)/bin/ruff check --fix src tests
	$(VENV)/bin/ruff format src tests

build:
	rm -rf build/zipapp && mkdir -p build/zipapp dist
	cp -R src/slot_machine build/zipapp/
	find build/zipapp -name __pycache__ -type d -prune -exec rm -rf {} +
	$(PYTHON) -m zipapp build/zipapp -c -p '/usr/bin/env python3' -m 'slot_machine.cli:main' -o $(APP)
	@echo "built $(APP) ($$(wc -c < $(APP) | tr -d ' ') bytes)"

clean:
	rm -rf build dist .pytest_cache .ruff_cache

deploy: build
	PI=$(PI) SSH_KEY=$(SSH_KEY) deploy/deploy.sh

cron-on:
	PI=$(PI) SSH_KEY=$(SSH_KEY) deploy/deploy.sh --cron-on

cron-off:
	PI=$(PI) SSH_KEY=$(SSH_KEY) deploy/deploy.sh --cron-off

smoke-pi:
	PI=$(PI) SSH_KEY=$(SSH_KEY) deploy/smoke-pi.sh

status:
	$(SSH) $(PI) '$(REMOTE) --status'

logs:
	$(SSH) $(PI) 'tail -n 50 $(LOG)'
