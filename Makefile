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
        config edit-config push-config find-chat-id test-telegram set-cookie discover

help:
	@echo "On the Mac:"
	@echo "  make dev            create .venv with pytest and ruff"
	@echo "  make test           run the test suite"
	@echo "  make lint           ruff check"
	@echo "  make build          build the single-file zipapp $(APP)"
	@echo "  make config         create/refresh config/slot-machine.env (git-ignored) from the template"
	@echo "  make edit-config    the same, then open it in \$$EDITOR"
	@echo "On the Pi ($(PI)):"
	@echo "  make deploy         build, install the app, upload your settings (cron untouched)"
	@echo "  make push-config    upload only your settings"
	@echo "  make smoke-pi       run the installed app against fake servers"
	@echo "  make find-chat-id   / make test-telegram"
	@echo "  make set-cookie     paste a fresh _yatri_session (hidden prompt)"
	@echo "  make discover       list your schedule and location ids"
	@echo "  make cron-on        checks on CRON_SCHEDULE   (make cron-off: off)"
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

config:
	@$(PYTHON) deploy/sync_config.py

edit-config: config
	@$${EDITOR:-nano} config/slot-machine.env
	@echo "Saved locally. 'make push-config' sends it to the Pi."

push-config:
	@PI=$(PI) SSH_KEY=$(SSH_KEY) deploy/deploy.sh --push-config

find-chat-id:
	@$(SSH) $(PI) '$(REMOTE) --find-chat-id'

test-telegram:
	@$(SSH) $(PI) '$(REMOTE) --test-telegram'

set-cookie:
	@$(SSH) -t $(PI) '$(REMOTE) --set-cookie'

discover:
	@$(SSH) $(PI) '$(REMOTE) --discover'
