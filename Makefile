PYTHON ?= python3
TEST_ARGS ?=
unexport PYTHONPATH PYTHONHOME
export PYTHON
export PYTHONDONTWRITEBYTECODE = 1
export PIP_NO_COMPILE = 1
export PIP_NO_CACHE_DIR = 1
export PIP_DISABLE_PIP_VERSION_CHECK = 1

.PHONY: help build install test-env test e2e clean

help:
	@echo 'Usage: make build | install | test-env | test | e2e | clean'

build install test-env:
	./build.sh $@

test: test-env
	bash scripts/run-tests.sh unit $(TEST_ARGS)

e2e: test-env
	bash scripts/run-tests.sh e2e $(TEST_ARGS)

clean:
	rm -rf build/generated build/tmp build/unit build/e2e
	rm -f dist/openevent_sdk-*.whl
