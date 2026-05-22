import { type FormEvent, useEffect, useState } from "react";

import { useAuth } from "../auth/context";
import { ApiError } from "../lib/api";
import { navigate } from "../lib/router";

export function RegisterRoute() {
  const { status, register } = useAuth();
  const [name, setName] = useState("");
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
      setError("Enter an email and password.");
      return;
    }

    if (password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }

    setIsSubmitting(true);
    try {
      await register({ email, password, name: name.trim() || undefined });
      navigate("/");
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : "Unable to create account.");
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-[#ece7dc] px-5 py-8 text-ink">
      <section className="w-full max-w-md rounded-md border border-line bg-[#fffdf8] p-6 shadow-soft">
        <p className="text-sm font-medium uppercase tracking-[0.16em] text-ink/55">Execute</p>
        <h1 className="mt-2 text-2xl font-semibold">Create account</h1>

        <form className="mt-6 space-y-4" onSubmit={handleSubmit}>
          <label className="block text-sm font-medium">
            Name
            <input
              className="mt-2 w-full rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-ink"
              type="text"
              autoComplete="name"
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </label>

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
              autoComplete="new-password"
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
            {isSubmitting ? "Creating..." : "Create account"}
          </button>
        </form>

        <div className="mt-5 border-t border-line pt-4 text-sm text-ink/70">
          Already registered?{" "}
          <button className="font-semibold text-ink underline" type="button" onClick={() => navigate("/login")}>
            Log in
          </button>
        </div>
      </section>
    </main>
  );
}
