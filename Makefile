# slot-machine: develop on the Mac, run on the Pi.
PI      ?= yarikp@yarikos.local
SSH_KEY ?= $(HOME)/.ssh/pi
SSH     := ssh -i $(SSH_KEY) -o IdentitiesOnly=yes -o LogLevel=ERROR
PYTHON  ?= python3
VENV    := .venv
APP     := dist/slot-machine
REMOTE  := .local/bin/slot-machine
LOG     := .local/state/slot-machine/watch.log

.PHONY: help dev test lint fmt build clean deploy cron-on cron-off smoke-pi status logs \
        edit-config find-chat-id test-telegram set-cookie discover

help:
	@echo "On the Mac:"
	@echo "  make dev            create .venv with pytest and ruff"
	@echo "  make test           run the test suite"
	@echo "  make lint           ruff check"
	@echo "  make build          build the single-file zipapp $(APP)"
	@echo "On the Pi ($(PI)):"
	@echo "  make deploy         build and install the app (cron untouched)"
	@echo "  make smoke-pi       run the installed app against fake servers"
	@echo "  make edit-config    open the settings file in nano"
	@echo "  make find-chat-id   / make test-telegram"
	@echo "  make set-cookie     paste a fresh _yatri_session (hidden prompt)"
	@echo "  make discover       list your schedule and location ids"
	@echo "  make cron-on        hourly checks on   (make cron-off: off)"
	@echo "  make status         / make logs"

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
	$(PYTHON) deploy/build.py $(APP)

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
	@$(SSH) $(PI) '$(REMOTE) --status'

logs:
	@$(SSH) $(PI) 'tail -n 50 $(LOG) 2>/dev/null || echo "no log yet (cron writes it)"'

edit-config:
	@$(SSH) -t $(PI) 'nano ~/.config/slot-machine/slot-machine.env'

find-chat-id:
	@$(SSH) $(PI) '$(REMOTE) --find-chat-id'

test-telegram:
	@$(SSH) $(PI) '$(REMOTE) --test-telegram'

set-cookie:
	@$(SSH) -t $(PI) '$(REMOTE) --set-cookie'

discover:
	@$(SSH) $(PI) '$(REMOTE) --discover'
