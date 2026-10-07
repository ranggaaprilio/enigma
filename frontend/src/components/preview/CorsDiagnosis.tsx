import type { ServiceDiagnosisResponse } from "../../lib/api";

/**
 * Shown per-service when the proxy has observed a rejected cross-origin request -- names the
 * offending origin and the header the API must send, instead of an unexplained network error
 * in the browser console. The proxy never injects a CORS header itself; this is diagnosis only.
 */
export function CorsDiagnosis({ diagnosis }: { diagnosis: ServiceDiagnosisResponse }) {
  const kindLabel =
    diagnosis.kind === "cors_preflight_rejected" ? "CORS preflight rejected" : "CORS blocked";

  return (
    <div className="mt-2 rounded-md border border-danger/30 bg-[#fff5f2] p-3 text-xs text-danger">
      <p className="font-semibold">{kindLabel}</p>
      <p className="mt-1">
        <span className="font-medium">{diagnosis.origin}</span> was rejected
        {diagnosis.request_method ? ` on a ${diagnosis.request_method} request` : ""}
        {diagnosis.request_path ? ` to ${diagnosis.request_path}` : ""}.
      </p>
      {diagnosis.required_header && (
        <p className="mt-1">
          The API must send{" "}
          <code className="rounded bg-danger/10 px-1 py-0.5">{diagnosis.required_header}</code>{" "}
          for this origin.
        </p>
      )}
      {diagnosis.detail && <p className="mt-1 text-danger/80">{diagnosis.detail}</p>}
      {diagnosis.occurrences > 1 && (
        <p className="mt-1 text-danger/60">Seen {diagnosis.occurrences} times.</p>
      )}
    </div>
  );
}
