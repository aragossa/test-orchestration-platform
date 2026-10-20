import { cloneElement, useEffect, useId, useMemo, useState, type ReactElement } from "react";
import { useNavigate } from "react-router-dom";
import {
  api,
  ApiError,
  FAILURE_SIMULATIONS,
  type Environment,
  type FailureSimulation,
  type TestDefinition,
} from "../lib/api";

interface FormState {
  environment: string;
  deployment_version: string;
  worker_instances: string;
  data_generators: string;
  timeout_seconds: string;
  selected: Set<string>;
  failure_simulation: FailureSimulation;
}

export function NewRun() {
  const navigate = useNavigate();
  const [environments, setEnvironments] = useState<Environment[] | null>(null);
  const [tests, setTests] = useState<TestDefinition[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [form, setForm] = useState<FormState | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  useEffect(() => {
    Promise.all([api.environments(), api.tests()])
      .then(([envs, catalog]) => {
        envs.sort((a, b) => envRank(a.id) - envRank(b.id));
        setEnvironments(envs);
        setTests(catalog);
        const first = envs.find((e) => e.id === "test") ?? envs[0];
        if (first) setForm(defaultsFor(first, "none", new Set()));
      })
      .catch((err: unknown) => setLoadError(err instanceof Error ? err.message : String(err)));
  }, []);

  const currentEnv = useMemo(
    () => environments?.find((e) => e.id === form?.environment) ?? null,
    [environments, form?.environment],
  );

  if (loadError) return <div className="error-box">Could not load reference data: {loadError}</div>;
  if (!form || !environments || !tests || !currentEnv) return <div className="empty">Loading…</div>;

  const overridden = {
    deployment_version: form.deployment_version !== currentEnv.default_deployment_version,
    worker_instances: Number(form.worker_instances) !== currentEnv.default_worker_instances,
    data_generators: Number(form.data_generators) !== currentEnv.default_data_generators,
    timeout_seconds: Number(form.timeout_seconds) !== currentEnv.default_timeout_seconds,
  };

  const selectEnvironment = (env: Environment) =>
    setForm(defaultsFor(env, form.failure_simulation, form.selected));

  const toggleTest = (id: string) => {
    const next = new Set(form.selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setForm({ ...form, selected: next });
  };

  const localErrors = validate(form);
  const canSubmit = Object.keys(localErrors).length === 0 && !submitting;

  const submit = async () => {
    setSubmitError(null);
    setFieldErrors({});
    if (!canSubmit) {
      setFieldErrors(localErrors);
      return;
    }
    setSubmitting(true);
    try {
      const ordered = tests.filter((t) => form.selected.has(t.id)).map((t) => t.id);
      const run = await api.createRun({
        environment: form.environment,
        deployment_version: form.deployment_version.trim(),
        worker_instances: Number(form.worker_instances),
        data_generators: Number(form.data_generators),
        timeout_seconds: Number(form.timeout_seconds),
        selected_test_ids: ordered,
        failure_simulation: form.failure_simulation,
      });
      navigate(`/runs/${run.id}`);
    } catch (err) {
      if (err instanceof ApiError) {
        setSubmitError(err.message);
        const byField: Record<string, string> = {};
        for (const d of err.details) if (d.field) byField[d.field.split(".").pop() ?? d.field] = d.message;
        setFieldErrors(byField);
      } else {
        setSubmitError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setSubmitting(false);
    }
  };

  const simulation = FAILURE_SIMULATIONS.find((s) => s.value === form.failure_simulation);

  return (
    <>
      <div className="page-header">
        <h1>Create new run</h1>
      </div>

      <form
        className="stack"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <section className="card">
          <h2>Environment</h2>
          <div className="segmented" role="radiogroup" aria-label="Environment">
            {environments.map((env) => (
              <button
                type="button"
                key={env.id}
                className={env.id === form.environment ? "on" : ""}
                onClick={() => selectEnvironment(env)}
                aria-pressed={env.id === form.environment}
              >
                {env.id}
              </button>
            ))}
          </div>
          <p className="muted small" style={{ margin: "10px 0 0" }}>
            Defaults for <code>{currentEnv.id}</code>: {currentEnv.default_worker_instances} worker
            {currentEnv.default_worker_instances === 1 ? "" : "s"}, {currentEnv.default_data_generators}{" "}
            generator{currentEnv.default_data_generators === 1 ? "" : "s"}, version{" "}
            <code>{currentEnv.default_deployment_version}</code>, timeout {currentEnv.default_timeout_seconds}s.
            Loaded from the backend; you can override them below.
          </p>
        </section>

        <section className="card">
          <h2>Deployment configuration</h2>
          <div className="form-grid">
            <Field
              label="Deployment version"
              error={fieldErrors.deployment_version}
              overridden={overridden.deployment_version}
            >
              <input
                type="text"
                value={form.deployment_version}
                onChange={(e) => setForm({ ...form, deployment_version: e.target.value })}
              />
            </Field>
            <Field
              label="Worker instances (1–5)"
              error={fieldErrors.worker_instances}
              overridden={overridden.worker_instances}
            >
              <input
                type="number"
                min={1}
                max={5}
                value={form.worker_instances}
                onChange={(e) => setForm({ ...form, worker_instances: e.target.value })}
              />
            </Field>
            <Field
              label="Data generators (1–5)"
              error={fieldErrors.data_generators}
              overridden={overridden.data_generators}
            >
              <input
                type="number"
                min={1}
                max={5}
                value={form.data_generators}
                onChange={(e) => setForm({ ...form, data_generators: e.target.value })}
              />
            </Field>
            <Field
              label="Test timeout (seconds)"
              error={fieldErrors.timeout_seconds}
              overridden={overridden.timeout_seconds}
              hint="Applies to deployment readiness and to each test."
            >
              <input
                type="number"
                min={1}
                max={3600}
                value={form.timeout_seconds}
                onChange={(e) => setForm({ ...form, timeout_seconds: e.target.value })}
              />
            </Field>
          </div>
        </section>

        <section className="card">
          <h2>
            Select tests{" "}
            <span className="muted" style={{ textTransform: "none", fontWeight: 400 }}>
              ({form.selected.size} of {tests.length} selected)
            </span>
          </h2>
          <div className="choice-list">
            {tests.map((t) => {
              const checked = form.selected.has(t.id);
              return (
                <label key={t.id} className={`choice ${checked ? "selected" : ""}`}>
                  <input type="checkbox" checked={checked} onChange={() => toggleTest(t.id)} />
                  <span>
                    <span className="choice-title">
                      <strong>{t.name}</strong>
                      <span className="badge category">{t.category}</span>
                      <span className="muted small">~{(t.base_duration_ms / 1000).toFixed(0)}s</span>
                    </span>
                    <span className="choice-desc">{t.description}</span>
                  </span>
                </label>
              );
            })}
          </div>
          {fieldErrors.selected_test_ids ? (
            <div className="field-error" style={{ marginTop: 8 }}>
              {fieldErrors.selected_test_ids}
            </div>
          ) : null}
        </section>

        <section className="card">
          <h2>Failure simulation</h2>
          <div className="choice-list">
            {FAILURE_SIMULATIONS.map((s) => (
              <label
                key={s.value}
                className={`choice ${form.failure_simulation === s.value ? "selected" : ""}`}
              >
                <input
                  type="radio"
                  name="failure_simulation"
                  checked={form.failure_simulation === s.value}
                  onChange={() => setForm({ ...form, failure_simulation: s.value })}
                />
                <span>
                  <span className="choice-title">
                    <code>{s.label}</code>
                  </span>
                  <span className="choice-desc">{s.hint}</span>
                </span>
              </label>
            ))}
          </div>
          {simulation?.value === "test_timeout" && Number(form.timeout_seconds) > 15 ? (
            <div className="warn-box" style={{ marginTop: 10 }}>
              With a {form.timeout_seconds}s timeout the hanging test will take {form.timeout_seconds}s to be
              marked TIMED_OUT. Consider a shorter timeout for a quick demonstration.
            </div>
          ) : null}
        </section>

        {submitError ? <div className="error-box">{submitError}</div> : null}

        <div style={{ display: "flex", gap: 12, justifyContent: "flex-end" }}>
          <button type="button" onClick={() => navigate("/")}>
            Cancel
          </button>
          <button type="submit" className="primary" disabled={!canSubmit}>
            {submitting ? "Starting…" : "Start run"}
          </button>
        </div>
      </form>
    </>
  );
}

const ENV_ORDER = ["test", "stage"];
function envRank(id: string): number {
  const index = ENV_ORDER.indexOf(id);
  return index === -1 ? ENV_ORDER.length : index;
}

function defaultsFor(env: Environment, simulation: FailureSimulation, selected: Set<string>): FormState {
  return {
    environment: env.id,
    deployment_version: env.default_deployment_version,
    worker_instances: String(env.default_worker_instances),
    data_generators: String(env.default_data_generators),
    timeout_seconds: String(env.default_timeout_seconds),
    selected,
    failure_simulation: simulation,
  };
}

function validate(form: FormState): Record<string, string> {
  const errors: Record<string, string> = {};
  if (!form.deployment_version.trim()) errors.deployment_version = "Required";
  for (const key of ["worker_instances", "data_generators"] as const) {
    const n = Number(form[key]);
    if (!Number.isInteger(n) || n < 1 || n > 5) errors[key] = "Integer between 1 and 5";
  }
  const timeout = Number(form.timeout_seconds);
  if (!Number.isInteger(timeout) || timeout <= 0) errors.timeout_seconds = "Positive integer";
  if (form.selected.size === 0) errors.selected_test_ids = "Select at least one test";
  return errors;
}

function Field({
  label,
  error,
  hint,
  overridden,
  children,
}: {
  label: string;
  error?: string;
  hint?: string;
  overridden?: boolean;
  children: ReactElement<{ id?: string }>;
}) {
  // Associate the label with its input so it is reachable by assistive tech
  // (and by tests) via the label text.
  const id = useId();
  return (
    <div className="field">
      <label htmlFor={id}>
        {label}
        {overridden ? <span className="badge warn" style={{ marginLeft: 6 }}>overridden</span> : null}
      </label>
      {cloneElement(children, { id })}
      {error ? <span className="field-error">{error}</span> : hint ? <span className="field-hint">{hint}</span> : null}
    </div>
  );
}
