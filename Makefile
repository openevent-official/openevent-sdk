PYTHON ?= python3
TEST_ARGS ?=
INSTALL_ARGS ?=

.PHONY: help init build install test e2e clean

help:
	@printf 'Usage: make <target>\n\n'
	@printf 'Targets:\n'
	@printf '  init     Generate local Python protobuf modules for development\n'
	@printf '  build    Build artifacts without installing into the current Python environment\n'
	@printf '  install  Build and install the generated wheel with pip\n'
	@printf '  test     Run tests or SDK smoke checks under build/\n'
	@printf '  e2e      Run SDK end-to-end tests against a real OpenEvent server\n'
	@printf '  clean    Remove build artifacts and temporary work files\n\n'
	@printf 'Variables:\n'
	@printf '  PYTHON=%s\n' '$(PYTHON)'
	@printf '  INSTALL_ARGS=%s\n' '$(INSTALL_ARGS)'
	@printf '\nExamples:\n'
	@printf '  make install INSTALL_ARGS="--target /opt/openevent-sdk"\n'

init:
	./generate_python_proto.sh

build:
	PYTHON="$(PYTHON)" ./build.sh

install: build
	@wheel="$$(find "dist" -maxdepth 1 -type f -name '*.whl' | sort | tail -n 1)"; \
	if [ -z "$$wheel" ]; then \
	  printf 'no wheel found in dist\n'; \
	  exit 1; \
	fi; \
	"$(PYTHON)" -m pip install $(INSTALL_ARGS) "$$wheel"

test:
	PYTHON="$(PYTHON)" ./test.sh $(TEST_ARGS)

e2e:
	PYTHON="$(PYTHON)" ./test-e2e.sh $(TEST_ARGS)

clean:
	rm -rf build dist
	rm -f src/openevent/sdk/proto/openevent_pb2.py src/openevent/sdk/proto/openevent_pb2_grpc.py
	rm -f src/openevent/sdk/proto/admin_pb2.py src/openevent/sdk/proto/admin_pb2_grpc.py
