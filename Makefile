RELEASE_ENV ?= release.env
DOCKERFILE ?= apps/inference-service/Dockerfile
BUILD_CONTEXT ?= apps/inference-service

.PHONY: release-preview release docker-build docker-push test runtime-base-preview runtime-base-release

release-preview:
	@./scripts/release.sh preview $(RELEASE_ENV)

release:
	@./scripts/release.sh prepare $(RELEASE_ENV)
	@$(MAKE) docker-build docker-push

docker-build:
	@set -a; . ./$(RELEASE_ENV); set +a; \
		docker build --pull \
			--build-arg RELEASE_VERSION="$$RELEASE_VERSION" \
			--build-arg SOURCE_COMMIT="$$RELEASE_COMMIT" \
			--tag "$$REGISTRY/$$IMAGE:$$RELEASE_VERSION" \
			--tag "$$REGISTRY/$$IMAGE:$$RELEASE_COMMIT" \
			--file "$(DOCKERFILE)" "$(BUILD_CONTEXT)"

docker-push:
	@set -a; . ./$(RELEASE_ENV); set +a; \
		docker push "$$REGISTRY/$$IMAGE:$$RELEASE_VERSION"; \
		docker push "$$REGISTRY/$$IMAGE:$$RELEASE_COMMIT"

test:
	@cd apps/inference-service && python3 -m pytest -q

runtime-base-preview:
	@./scripts/runtime-base-release.sh preview

runtime-base-release:
	@./scripts/runtime-base-release.sh prepare
	@set -a; . ./projects/model-runtime-base/base.env; set +a; \
		docker build --pull \
			--build-arg RUNTIME_BASE_VERSION="$$RUNTIME_BASE_VERSION" \
			--build-arg RUNTIME_BASE_INPUT_SHA256="$$RUNTIME_BASE_INPUT_SHA256" \
			--tag "$$RUNTIME_BASE_REGISTRY/$$RUNTIME_BASE_IMAGE_NAME:$$RUNTIME_BASE_VERSION" \
			./projects/model-runtime-base; \
		docker push "$$RUNTIME_BASE_REGISTRY/$$RUNTIME_BASE_IMAGE_NAME:$$RUNTIME_BASE_VERSION"; \
		./scripts/runtime-base-release.sh finalize
