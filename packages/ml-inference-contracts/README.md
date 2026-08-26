# ML Inference Contracts

Shared, dependency-light integration library for the ML Inference Service.

- `log_pyfunc_model` publishes a deployable MLflow PyFunc registered model with
  required serving metadata. Install with `pip install '.[mlflow]'`.
- `DeploymentClient` is a standard-library HTTP client suitable for an Airflow
  Python task. It submits an immutable MLflow URI and waits for `active`.

The canonical contract is documented in
[`../../docs/mlflow-model-contract.md`](../../docs/mlflow-model-contract.md).

