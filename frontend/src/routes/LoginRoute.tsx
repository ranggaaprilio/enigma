import { type FormEvent, useState } from "react";
import { Link, useNavigate } from "react-router";

import { useAuth } from "../auth/context";
import { ApiError } from "../lib/api";

export function LoginRoute() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);

    if (!email.trim() || !password) {
      setError("Enter your email and password.");
      return;
    }

    setIsSubmitting(true);
    try {
      await login({ email, password });
      navigate("/", { replace: true });
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : "Unable to log in.");
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-canvas px-5 py-8 text-ink">
      <section className="w-full max-w-md rounded-xl border border-line bg-white p-6 shadow-soft">
        <p className="text-xs font-semibold uppercase tracking-[0.16em] text-primary">ENIGMA</p>
        <h1 className="mt-2 text-3xl font-normal tracking-[-0.02em]">Log in</h1>

        <form className="mt-6 space-y-4" onSubmit={handleSubmit}>
          <label className="block text-sm font-medium">
            Email
            <input
              className="mt-2 w-full rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-ink"
              type="email"
              autoComplete="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          </label>

          <label className="block text-sm font-medium">
            Password
            <input
              className="mt-2 w-full rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-ink"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>

          {error && (
            <div className="rounded-md border border-danger/30 bg-[#fff5f2] px-3 py-2 text-sm text-danger">
              {error}
            </div>
          )}

          <button
            className="h-10 w-full rounded-md bg-primary px-4 py-2 text-sm font-medium text-white transition hover:bg-primary-active disabled:cursor-not-allowed disabled:bg-primary/45"
            type="submit"
            disabled={isSubmitting}
          >
            {isSubmitting ? "Logging in..." : "Log in"}
          </button>
        </form>

        <div className="mt-5 border-t border-line pt-4 text-sm text-ink/70">
          New here?{" "}
          <Link className="font-semibold text-ink underline" to="/register">
            Create an account
          </Link>
        </div>
      </section>
    </main>
  );
}
