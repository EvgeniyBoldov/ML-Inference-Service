# ML Inference Contracts

Shared, dependency-light integration library for the ML Inference Service.

- `log_pyfunc_model` publishes a deployable MLflow PyFunc registered model with
  required serving metadata. It reads `ML_INFERENCE_PRODUCER_IMAGE` from the
  training container and writes its value to the model-version tag
  `ml_inference.producer_image`. Set it to the exact, never-reused local image
  tag used by that Airflow/Jupyter service, for example
  `registry.local/ml-airflow:2026.10.06-17`. Install with `pip install '.[mlflow]'`.
- `DeploymentClient` is a standard-library HTTP client suitable for an Airflow
  Python task. It submits an immutable MLflow URI and waits for `active`.

The canonical contract is documented in
[`../../docs/mlflow-model-contract.md`](../../docs/mlflow-model-contract.md).
Step-by-step Jupyter and Airflow usage is in
[`../../docs/ml-engineer-guide.md`](../../docs/ml-engineer-guide.md).
