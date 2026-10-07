# Producer image runtime contract

Inference runs inside the same versioned Airflow or Jupyter image that produced
the model. The inference service does not install model requirements and does
not pull images in production. Every producer image referenced by a model must
already exist in the Docker image store on the production host.

## Producer configuration

Set `ML_INFERENCE_PRODUCER_IMAGE` in each Airflow and Jupyter service. Its value
must be the exact, never-reused image tag that identifies that build, for
example:

```text
ML_INFERENCE_PRODUCER_IMAGE=registry.company.local/ml-airflow:2026.10.06-17
```

The logging package reads this variable and writes MLflow model-version tag
`ml_inference.producer_image`. Missing or empty values fail model publication.
Do not reuse a tag for a different build. Tags are the producer-side contract;
on deployment the service resolves the tag against the local Docker Engine and
stores the immutable RepoDigest when available, otherwise the immutable local
Docker image ID. The resolved value is the runtime grouping key.

## Image contents and identity

Each supported producer image must contain Python, the model's dependencies,
MLflow, FastAPI, and Uvicorn. It must also provide UID and GID `10001`, used by
the runtime process. Airflow and its scheduler/worker should use the same
versioned image, and all services publishing models must set the same image
value for that build. Jupyter follows the same contract.

An image build is a new environment identity. If dependencies or operating
system packages change, publish a new tag and update `ML_INFERENCE_PRODUCER_IMAGE`.
Two model versions are grouped only when their resolved local immutable image
identity is equal.

## Runtime launch and mounts

The service starts a container from the resolved local image with
`--pull=never`, overrides its entrypoint to run Uvicorn, and uses UID/GID
`10001:10001`. It mounts the model-artifact cache at `/models:ro` and a
versioned copy of the inference runner at `/opt/ml-inference-runner:ro`. The
container joins the internal `ml-inference-runtime` network and publishes no
host port. It receives no Docker socket, MLflow credentials, object-store
credentials, or database credentials.

The runner loads the group's manifest with `mlflow.pyfunc.load_model`, serves
`/health` and `/predict`, and never installs packages. The API downloads
artifacts and prepares read permissions before starting the container.

## Operations

Preload each producer image onto the production Docker host as part of the
existing Airflow/Jupyter image rollout. A deployment fails with
`PRODUCER_IMAGE_UNAVAILABLE` if the referenced image is absent; it will not
attempt a registry pull. Old producer images must remain available while a
runtime group or its rollback window still refers to them.

The API image still uses `projects/model-runtime-base` for its own control-plane
Python dependencies. That base is not used to execute model code; model runtime
dependencies come from producer images.
