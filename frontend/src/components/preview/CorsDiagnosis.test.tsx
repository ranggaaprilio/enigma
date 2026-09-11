import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ServiceDiagnosisResponse } from "../../lib/api";
import { CorsDiagnosis } from "./CorsDiagnosis";

function diagnosis(overrides: Partial<ServiceDiagnosisResponse> = {}): ServiceDiagnosisResponse {
  return {
    id: "diag-1",
    kind: "cors_missing_allow_origin",
    origin: "https://web-demo-a1b2c3.localhost:8001",
    request_method: "GET",
    request_path: "/api/users",
    required_header: null,
    status_code: 200,
    detail: null,
    occurrences: 1,
    first_seen_at: "2026-09-01T00:00:00Z",
    last_seen_at: "2026-09-01T00:05:00Z",
    ...overrides,
  };
}

describe("CorsDiagnosis", () => {
  it("names the offending origin, method and path for a missing-allow-origin diagnosis", () => {
    render(<CorsDiagnosis diagnosis={diagnosis()} />);

    expect(screen.getByText("CORS blocked")).toBeInTheDocument();
    expect(screen.getByText("https://web-demo-a1b2c3.localhost:8001")).toBeInTheDocument();
    expect(screen.getByText(/on a GET request/)).toBeInTheDocument();
    expect(screen.getByText(/to \/api\/users/)).toBeInTheDocument();
  });

  it("names the required header for a rejected preflight", () => {
    render(
      <CorsDiagnosis
        diagnosis={diagnosis({
          kind: "cors_preflight_rejected",
          required_header: "Access-Control-Allow-Headers",
        })}
      />,
    );

    expect(screen.getByText("CORS preflight rejected")).toBeInTheDocument();
    expect(screen.getByText("Access-Control-Allow-Headers")).toBeInTheDocument();
    expect(screen.getByText(/The API must send/)).toBeInTheDocument();
  });

  it("does not render a required-header line when none is set", () => {
    render(<CorsDiagnosis diagnosis={diagnosis({ required_header: null })} />);

    expect(screen.queryByText(/The API must send/)).not.toBeInTheDocument();
  });

  it("shows the occurrence count only when it is greater than one", () => {
    const { rerender } = render(<CorsDiagnosis diagnosis={diagnosis({ occurrences: 1 })} />);
    expect(screen.queryByText(/Seen/)).not.toBeInTheDocument();

    rerender(<CorsDiagnosis diagnosis={diagnosis({ occurrences: 7 })} />);
    expect(screen.getByText("Seen 7 times.")).toBeInTheDocument();
  });

  it("renders the detail line when present", () => {
    render(<CorsDiagnosis diagnosis={diagnosis({ detail: "Preflight responded 403." })} />);

    expect(screen.getByText("Preflight responded 403.")).toBeInTheDocument();
  });
});
