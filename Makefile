PYTHON ?= python3
TEST_ARGS ?=
INSTALL_ARGS ?=

.PHONY: help init build install test e2e clean

help:
	@printf 'Usage: make <target>\n\n'
	@printf 'Targets:\n'
	@printf '  init     Assemble the development package and protobuf modules under build/python/\n'
	@printf '  build    Build artifacts without installing into the current Python environment\n'
	@printf '  install  Build and install the generated wheel with pip\n'
	@printf '  test     Run tests or SDK smoke checks under build/\n'
	@printf '  e2e      Run SDK end-to-end tests against a real OpenEvent server\n'
	@printf '  clean    Remove build artifacts and temporary work files\n\n'
	@printf 'Variables:\n'
	@printf '  PYTHON=%s\n' '$(PYTHON)'
	@printf '  INSTALL_ARGS=%s\n' '$(INSTALL_ARGS)'
	@printf '\nExamples:\n'
	@printf '  make install PYTHON=/opt/openevent-sdk/bin/python\n'

init:
	PYTHON="$(PYTHON)" ./generate_python_proto.sh

build:
	PYTHON="$(PYTHON)" ./build.sh

install: build
	PYTHONDONTWRITEBYTECODE=1 "$(PYTHON)" scripts/install.py $(INSTALL_ARGS)

test:
	PYTHON="$(PYTHON)" ./test.sh $(TEST_ARGS)

e2e:
	PYTHON="$(PYTHON)" ./test-e2e.sh $(TEST_ARGS)

clean:
	rm -rf build dist
	# Also remove generated modules left by the old source-tree build layout.
	rm -f src/openevent/sdk/proto/openevent_pb2.py src/openevent/sdk/proto/openevent_pb2_grpc.py
	rm -f src/openevent/sdk/proto/admin_pb2.py src/openevent/sdk/proto/admin_pb2_grpc.py
