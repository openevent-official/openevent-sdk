PYTHON ?= python3
TEST_ARGS ?=
INSTALL_ARGS ?=

.PHONY: help init dev build install test-env test e2e clean

help:
	@printf 'Usage: make <target>\n\n'
	@printf 'Targets:\n'
	@printf '  init     Generate and stage the package under build/python/ (no runtime dependencies)\n'
	@printf '  dev      Build and prepare the SDK development environment in build/test-env/\n'
	@printf '  build    Build artifacts without installing into the current Python environment\n'
	@printf '  install  Build and install the generated wheel with pip\n'
	@printf '  test-env Build and install the current SDK into build/test-env/\n'
	@printf '  test     Run SDK unit tests using the current built wheel\n'
	@printf '  e2e      Run SDK end-to-end tests against a real OpenEvent server\n'
	@printf '  clean    Remove build artifacts and temporary work files\n\n'
	@printf 'Variables:\n'
	@printf '  PYTHON=%s\n' '$(PYTHON)'
	@printf '  INSTALL_ARGS=%s\n' '$(INSTALL_ARGS)'
	@printf '\nExamples:\n'
	@printf '  make install PYTHON=/opt/openevent-sdk/bin/python\n'

init:
	PYTHON="$(PYTHON)" ./generate_python_proto.sh

dev: test-env

build:
	PYTHON="$(PYTHON)" ./build.sh

install: build
	PYTHONDONTWRITEBYTECODE=1 "$(PYTHON)" scripts/install.py $(INSTALL_ARGS)

test-env: export PYTHONPATH =
test-env: export PYTHONHOME =
test-env: build
	PYTHONDONTWRITEBYTECODE=1 "$(PYTHON)" -B scripts/prepare_test_env.py

test:
	PYTHON="$(PYTHON)" ./test.sh $(TEST_ARGS)

e2e:
	PYTHON="$(PYTHON)" ./test-e2e.sh $(TEST_ARGS)

clean:
	rm -rf build dist
