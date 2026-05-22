import { type FormEvent, useEffect, useState } from "react";

import { useAuth } from "../auth/context";
import { ApiError } from "../lib/api";
import { navigate } from "../lib/router";

export function LoginRoute() {
  const { status, login } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    if (status === "authenticated") {
      navigate("/");
    }
  }, [status]);

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
      navigate("/");
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : "Unable to log in.");
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-[#ece7dc] px-5 py-8 text-ink">
      <section className="w-full max-w-md rounded-md border border-line bg-[#fffdf8] p-6 shadow-soft">
        <p className="text-sm font-medium uppercase tracking-[0.16em] text-ink/55">Execute</p>
        <h1 className="mt-2 text-2xl font-semibold">Log in</h1>

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
            className="w-full rounded-md bg-ink px-4 py-2 font-semibold text-white transition hover:bg-ink/90 disabled:cursor-not-allowed disabled:bg-ink/45"
            type="submit"
            disabled={isSubmitting}
          >
            {isSubmitting ? "Logging in..." : "Log in"}
          </button>
        </form>

        <div className="mt-5 border-t border-line pt-4 text-sm text-ink/70">
          New here?{" "}
          <button className="font-semibold text-ink underline" type="button" onClick={() => navigate("/register")}>
            Create an account
          </button>
        </div>
      </section>
    </main>
  );
}
