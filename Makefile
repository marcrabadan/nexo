# Setup and checks for Nexo. `make` alone lists the targets.
#
# Tools: Python 3.12 or newer (phase 0), cargo 1.89 or newer and clang
# (phase 1), node and npm (the VS Code extension). Each target says what
# is missing and how to install it.

PYTHON ?= python3.12
VENV := .venv
PIP := $(VENV)/bin/pip
NEXO := $(VENV)/bin/nexo
PYTEST := $(VENV)/bin/pytest
EXT := editors/vscode

# cargo is keg-only in Homebrew and lives in ~/.cargo/bin with rustup.sh.
CARGO ?= $(shell command -v cargo 2>/dev/null \
	|| command -v $(HOME)/.cargo/bin/cargo 2>/dev/null \
	|| command -v /opt/homebrew/opt/rustup/bin/cargo 2>/dev/null)

.DEFAULT_GOAL := help
.PHONY: help setup tools venv compiler release extension vsix \
	test test-compiler test-extension conformance differential clean

help:
	@echo "Setup:"
	@echo "  make setup            venv, compiler and extension: everything the tools allow"
	@echo "  make tools            what is installed and what is missing"
	@echo "  make venv             .venv with the interpreter and pytest (phase 0)"
	@echo "  make compiler         compiler/target/debug/nexo (phase 1)"
	@echo "  make release          compiler/target/release/nexo, the one to put in nexo.path"
	@echo "  make extension        npm install in $(EXT)"
	@echo "  make vsix             $(EXT)/nexo.vsix"
	@echo ""
	@echo "Checks:"
	@echo "  make test             pytest (compiled tests skip without cargo and clang)"
	@echo "  make test-compiler    cargo test"
	@echo "  make test-extension   the extension's grammar, activation and server tests"
	@echo "  make conformance      the conformance suite with the compiler"
	@echo "  make differential     the compiler gives the interpreter's output"
	@echo ""
	@echo "  make clean            .venv, compiler/target and the extension's node_modules"

tools:
	@printf '%-8s %s\n' python "$$(command -v $(PYTHON) || echo 'not found: install Python 3.12 or newer')"
	@printf '%-8s %s\n' cargo "$${CARGO:-$(CARGO)}$(if $(CARGO),, (not found: brew install rustup && rustup default stable))"
	@printf '%-8s %s\n' clang "$$(command -v clang || echo 'not found: xcode-select --install')"
	@printf '%-8s %s\n' node "$$(command -v node || echo 'not found: brew install node')"
	@printf '%-8s %s\n' nexo "$$(test -x $(NEXO) && echo $(NEXO) || echo 'not built: make venv')"

setup: venv extension
	@if [ -n "$(CARGO)" ]; then $(MAKE) compiler; \
	else echo "cargo not found, phase 1 skipped: brew install rustup && rustup default stable"; fi
	@echo "setup done. make tools lists what is installed."

venv: $(NEXO)

$(NEXO): pyproject.toml
	@command -v $(PYTHON) >/dev/null || { echo "$(PYTHON) not found: install Python 3.12 or newer"; exit 1; }
	$(PYTHON) -m venv $(VENV)
	$(PIP) install -e '.[dev]'
	@touch $(NEXO)

compiler:
	@test -n "$(CARGO)" || { echo "cargo not found: brew install rustup && rustup default stable"; exit 1; }
	cd compiler && $(CARGO) build

release:
	@test -n "$(CARGO)" || { echo "cargo not found: brew install rustup && rustup default stable"; exit 1; }
	cd compiler && $(CARGO) build --release
	@echo "nexo.path: $(CURDIR)/compiler/target/release/nexo"

extension: $(EXT)/node_modules

$(EXT)/node_modules: $(EXT)/package.json
	@command -v npm >/dev/null || { echo "npm not found: brew install node"; exit 1; }
	cd $(EXT) && npm install
	@touch $(EXT)/node_modules

vsix: extension
	cd $(EXT) && npm run package

test: venv
	$(PYTEST)

test-compiler:
	@test -n "$(CARGO)" || { echo "cargo not found: brew install rustup && rustup default stable"; exit 1; }
	cd compiler && $(CARGO) test

test-extension: extension
	cd $(EXT) && npm test

conformance: compiler
	$(PYTHON) compiler/scripts/conformance.py

differential: venv compiler
	$(VENV)/bin/python compiler/scripts/differential.py

clean:
	rm -rf $(VENV) compiler/target $(EXT)/node_modules

.PHONY: ai-guide ai-guide-check
ai-guide:
	python3 docs/ai-guide/build.py --measure

ai-guide-check:
	python3 docs/ai-guide/build.py --measure --check
	python3 -m pytest tests/test_ai_guide.py tests/test_ai_guide_advanced.py
	node --test tests/ai-guide.test.cjs
