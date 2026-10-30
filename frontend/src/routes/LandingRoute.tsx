import { useState } from "react";
import {
  Activity,
  ArrowRight,
  Bot,
  Check,
  ChevronRight,
  Code2,
  Eye,
  FileCode2,
  GitBranch,
  Menu,
  Monitor,
  Server,
  ShieldCheck,
  Terminal,
  Workflow,
  X,
} from "lucide-react";
import { Link } from "react-router";

const productFeatures = [
  {
    icon: Bot,
    label: "Agent runs",
    title: "Watch the work, not just the answer.",
    body: "Follow prompts, tool calls, changed files, and streamed output as one readable execution history.",
  },
  {
    icon: Server,
    label: "Isolated runtime",
    title: "One persistent workspace per project.",
    body: "Keep the repository, environment variables, terminal, and running services together inside an isolated sandbox: Daytona in the cloud, or Docker on your own machine.",
  },
  {
    icon: GitBranch,
    label: "Repository control",
    title: "Review before anything leaves.",
    body: "Inspect status and diffs, then pull, commit, and push from the same place where the agent did its work.",
  },
  {
    icon: FileCode2,
    label: "Editor + files",
    title: "Step in whenever the code needs you.",
    body: "Browse project files or open the workspace in code-server without losing the context of the run.",
  },
  {
    icon: Terminal,
    label: "Terminal + commands",
    title: "Keep the real tools close.",
    body: "Use a live terminal and reusable project commands for tests, builds, and the checks your repository already trusts.",
  },
  {
    icon: Monitor,
    label: "Preview + audit",
    title: "See the result and the record.",
    body: "Open the running application, track workspace status, and review the important actions that happened along the way.",
  },
];

const workflowSteps = [
  {
    number: "01",
    title: "Connect the repository",
    body: "Create an isolated project workspace and clone the code into it.",
  },
  {
    number: "02",
    title: "Brief the agent",
    body: "Describe the change from the same control room that holds the project context.",
  },
  {
    number: "03",
    title: "Observe execution",
    body: "See files read, commands run, edits made, and output streamed in real time.",
  },
  {
    number: "04",
    title: "Inspect the result",
    body: "Review the diff, run checks, open the editor, and verify the live preview.",
  },
  {
    number: "05",
    title: "Commit with confidence",
    body: "Commit and push the reviewed changes with an audit trail left behind.",
  },
];

const quickstartLines = [
  { prompt: "$", command: "cp .env.example .env" },
  { prompt: "#", command: "add DAYTONA_API_KEY to .env", muted: true },
  { prompt: "$", command: "podman compose up --build" },
];

export function LandingRoute() {
  const [menuOpen, setMenuOpen] = useState(false);

  function closeMenu() {
    setMenuOpen(false);
  }

  return (
    <div className="min-h-screen overflow-x-hidden bg-canvas text-ink">
      <a
        className="sr-only z-50 rounded-md bg-ink px-4 py-3 text-sm font-medium text-canvas focus:not-sr-only focus:fixed focus:left-4 focus:top-4"
        href="#main-content"
      >
        Skip to content
      </a>

      <header className="relative z-40 border-b border-line bg-canvas/95 backdrop-blur">
        <div className="mx-auto flex h-16 max-w-[1200px] items-center justify-between px-5 lg:px-8">
          <a
            className="flex min-h-11 items-center gap-3 rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:ring-offset-canvas"
            href="#top"
            aria-label="Enigma home"
          >
            <span className="flex h-9 w-9 items-center justify-center rounded-md bg-primary text-white">
              <Workflow className="h-[18px] w-[18px]" aria-hidden="true" />
            </span>
            <span className="text-xs font-semibold uppercase tracking-[0.2em] text-primary">
              Enigma
            </span>
          </a>

          <nav className="hidden items-center gap-8 md:flex" aria-label="Primary navigation">
            <a className="landing-nav-link" href="#product">
              Product
            </a>
            <a className="landing-nav-link" href="#workflow">
              Workflow
            </a>
            <a className="landing-nav-link" href="#quickstart">
              Quickstart
            </a>
          </nav>

          <div className="hidden items-center gap-3 md:flex">
            <Link className="landing-text-link" to="/login">
              Sign in
            </Link>
            <Link className="landing-button-primary" to="/register">
              Create account
            </Link>
          </div>

          <button
            className="flex h-11 w-11 items-center justify-center rounded-md border border-line bg-white text-ink transition hover:bg-panel focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:ring-offset-canvas md:hidden"
            type="button"
            aria-expanded={menuOpen}
            aria-controls="mobile-navigation"
            aria-label={menuOpen ? "Close navigation" : "Open navigation"}
            onClick={() => setMenuOpen((current) => !current)}
          >
            {menuOpen ? (
              <X className="h-5 w-5" aria-hidden="true" />
            ) : (
              <Menu className="h-5 w-5" aria-hidden="true" />
            )}
          </button>
        </div>

        {menuOpen && (
          <nav
            id="mobile-navigation"
            className="absolute left-0 right-0 top-16 border-b border-line bg-canvas px-5 py-5 md:hidden"
            aria-label="Mobile navigation"
          >
            <div className="mx-auto flex max-w-[1200px] flex-col">
              <a className="landing-mobile-link" href="#product" onClick={closeMenu}>
                Product
                <ChevronRight className="h-4 w-4" aria-hidden="true" />
              </a>
              <a className="landing-mobile-link" href="#workflow" onClick={closeMenu}>
                Workflow
                <ChevronRight className="h-4 w-4" aria-hidden="true" />
              </a>
              <a className="landing-mobile-link" href="#quickstart" onClick={closeMenu}>
                Quickstart
                <ChevronRight className="h-4 w-4" aria-hidden="true" />
              </a>
              <div className="mt-4 grid grid-cols-2 gap-3 border-t border-line pt-5">
                <Link className="landing-button-secondary" to="/login" onClick={closeMenu}>
                  Sign in
                </Link>
                <Link className="landing-button-primary" to="/register" onClick={closeMenu}>
                  Create account
                </Link>
              </div>
            </div>
          </nav>
        )}
      </header>

      <main id="main-content">
        <section id="top" className="px-5 pb-16 pt-16 sm:pt-20 lg:px-8 lg:pb-24 lg:pt-28">
          <div className="mx-auto max-w-[1200px]">
            <div className="grid items-end gap-10 lg:grid-cols-12 lg:gap-8">
              <div className="lg:col-span-8">
                <div className="mb-7 inline-flex items-center gap-2 rounded-full border border-line-strong bg-white px-3 py-1.5 text-xs font-medium text-body">
                  <span className="h-1.5 w-1.5 rounded-full bg-signal" />
                  Self-hosted AI development control room
                </div>
                <h1 className="max-w-5xl text-[46px] font-normal leading-[0.98] tracking-[-0.045em] sm:text-[64px] lg:text-[82px]">
                  Give every coding agent a room you can see.
                </h1>
              </div>
              <div className="lg:col-span-4 lg:pb-2">
                <p className="max-w-md text-base leading-7 text-body sm:text-lg sm:leading-8">
                  Enigma brings the agent, repository, terminal, editor, preview, and audit
                  trail into one observable workspace you run yourself.
                </p>
                <div className="mt-7 flex flex-wrap items-center gap-3">
                  <a className="landing-button-dark" href="#quickstart">
                    Run Enigma locally
                    <ArrowRight className="h-4 w-4" aria-hidden="true" />
                  </a>
                  <Link className="landing-text-link min-h-11 px-2" to="/register">
                    Create an account
                  </Link>
                </div>
              </div>
            </div>

            <div className="mt-14 lg:mt-20">
              <AgentFlightRecorder />
            </div>
          </div>
        </section>

        <section id="product" className="scroll-mt-16 border-t border-line px-5 py-20 lg:px-8 lg:py-28">
          <div className="mx-auto max-w-[1200px]">
            <div className="grid gap-8 lg:grid-cols-12">
              <div className="lg:col-span-4">
                <p className="landing-eyebrow">One room, complete context</p>
                <h2 className="mt-4 max-w-sm text-4xl font-normal leading-[1.08] tracking-[-0.035em] sm:text-5xl">
                  The whole development loop stays visible.
                </h2>
              </div>
              <div className="lg:col-span-8 lg:pt-2">
                <p className="max-w-2xl text-lg leading-8 text-body">
                  An AI coding run touches more than a chat box. Enigma keeps the code,
                  runtime, tools, and result together so you can understand what changed
                  and take over at any moment.
                </p>
              </div>
            </div>

            <div className="mt-14 grid border-l border-t border-line sm:grid-cols-2 lg:grid-cols-3">
              {productFeatures.map(({ icon: Icon, label, title, body }) => (
                <article
                  key={label}
                  className="group min-h-[300px] border-b border-r border-line bg-white p-6 transition-colors hover:bg-panel sm:p-7"
                >
                  <div className="flex items-center justify-between">
                    <span className="flex h-10 w-10 items-center justify-center rounded-md border border-line bg-canvas text-ink">
                      <Icon className="h-[18px] w-[18px]" aria-hidden="true" />
                    </span>
                    <span className="text-[11px] font-semibold uppercase tracking-[0.14em] text-muted">
                      {label}
                    </span>
                  </div>
                  <h3 className="mt-14 text-2xl font-normal leading-tight tracking-[-0.025em]">
                    {title}
                  </h3>
                  <p className="mt-4 text-sm leading-6 text-body">{body}</p>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section className="border-t border-line bg-ink px-5 py-20 text-canvas lg:px-8 lg:py-28">
          <div className="mx-auto max-w-[1200px]">
            <div className="grid gap-12 lg:grid-cols-12 lg:items-center">
              <div className="lg:col-span-5">
                <p className="text-xs font-semibold uppercase tracking-[0.16em] text-[#dfa88f]">
                  Your infrastructure
                </p>
                <h2 className="mt-5 text-4xl font-normal leading-[1.08] tracking-[-0.035em] sm:text-5xl">
                  Your agent should work where you can inspect it.
                </h2>
                <p className="mt-6 max-w-lg text-base leading-7 text-[#cfcdc4]">
                  Enigma runs alongside your existing stack. Project work happens inside
                  an isolated sandbox (Daytona or local Docker), while the control plane stays in your
                  hands.
                </p>
                <ul className="mt-8 space-y-4">
                  {[
                    "Cookie-based access to private project workspaces",
                    "Persistent repository and runtime context",
                    "Short-lived access to editor and preview surfaces",
                  ].map((item) => (
                    <li key={item} className="flex items-start gap-3 text-sm text-[#e6e5e0]">
                      <Check className="mt-0.5 h-4 w-4 shrink-0 text-[#9fc9a2]" aria-hidden="true" />
                      {item}
                    </li>
                  ))}
                </ul>
              </div>

              <div className="lg:col-span-7">
                <RuntimeDiagram />
              </div>
            </div>
          </div>
        </section>

        <section id="workflow" className="scroll-mt-16 border-t border-line px-5 py-20 lg:px-8 lg:py-28">
          <div className="mx-auto max-w-[1200px]">
            <div className="grid gap-8 lg:grid-cols-12">
              <div className="lg:col-span-5">
                <p className="landing-eyebrow">A reviewable workflow</p>
                <h2 className="mt-4 max-w-md text-4xl font-normal leading-[1.08] tracking-[-0.035em] sm:text-5xl">
                  From repository to reviewed change.
                </h2>
              </div>
              <div className="lg:col-span-7 lg:pt-3">
                <p className="max-w-2xl text-lg leading-8 text-body">
                  Let the agent move quickly without turning the work into a black box.
                  Every stage leads naturally to a place where you can inspect, verify,
                  or intervene.
                </p>
              </div>
            </div>

            <ol className="mt-14 border-t border-line">
              {workflowSteps.map((step, index) => (
                <li
                  key={step.number}
                  className="grid gap-4 border-b border-line py-7 sm:grid-cols-[80px_1fr] lg:grid-cols-[100px_1fr_1fr] lg:items-center"
                >
                  <span className="font-mono text-xs text-muted">{step.number}</span>
                  <h3 className="text-2xl font-normal tracking-[-0.025em]">{step.title}</h3>
                  <div className="flex items-center gap-6">
                    <p className="max-w-lg text-sm leading-6 text-body">{step.body}</p>
                    {index < workflowSteps.length - 1 && (
                      <ArrowRight
                        className="hidden h-4 w-4 shrink-0 text-muted-soft lg:block"
                        aria-hidden="true"
                      />
                    )}
                  </div>
                </li>
              ))}
            </ol>
          </div>
        </section>

        <section id="quickstart" className="scroll-mt-16 border-t border-line px-5 py-20 lg:px-8 lg:py-28">
          <div className="mx-auto max-w-[1200px]">
            <div className="overflow-hidden rounded-xl border border-line bg-white">
              <div className="grid lg:grid-cols-2">
                <div className="flex flex-col justify-between border-b border-line p-7 sm:p-10 lg:min-h-[480px] lg:border-b-0 lg:border-r">
                  <div>
                    <p className="landing-eyebrow">Quickstart</p>
                    <h2 className="mt-4 max-w-md text-4xl font-normal leading-[1.08] tracking-[-0.035em] sm:text-5xl">
                      Bring up the whole control room.
                    </h2>
                    <p className="mt-6 max-w-md text-base leading-7 text-body">
                      Enigma ships as a FastAPI and React stack with PostgreSQL, Redis,
                      and Podman Compose for local development.
                    </p>
                  </div>
                  <div className="mt-10 flex items-center gap-3 text-sm text-body">
                    <ShieldCheck className="h-5 w-5 text-signal" aria-hidden="true" />
                    Runs on infrastructure you control
                  </div>
                </div>

                <div className="bg-panel p-5 sm:p-8 lg:flex lg:items-center">
                  <div className="w-full overflow-hidden rounded-lg border border-line-strong bg-ink text-canvas">
                    <div className="flex h-11 items-center justify-between border-b border-white/10 px-4">
                      <div className="flex gap-1.5" aria-hidden="true">
                        <span className="h-2.5 w-2.5 rounded-full bg-[#dfa88f]" />
                        <span className="h-2.5 w-2.5 rounded-full bg-[#c08532]" />
                        <span className="h-2.5 w-2.5 rounded-full bg-[#9fc9a2]" />
                      </div>
                      <span className="font-mono text-[11px] text-[#a09c92]">terminal</span>
                    </div>
                    <div className="min-h-[250px] space-y-5 p-5 font-mono text-xs leading-6 sm:p-7 sm:text-sm">
                      {quickstartLines.map((line) => (
                        <div
                          key={line.command}
                          className={line.muted ? "text-[#a09c92]" : "text-[#efeee8]"}
                        >
                          <span className="mr-3 text-[#9fc9a2]">{line.prompt}</span>
                          {line.command}
                        </div>
                      ))}
                      <div className="border-t border-white/10 pt-5 text-[#9fbbe0]">
                        Frontend ready at http://localhost:5173
                      </div>
                      <div className="text-[#9fc9a2]">API connected · workspace ready</div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </section>

        <section className="border-t border-line px-5 py-24 text-center lg:px-8 lg:py-32">
          <div className="mx-auto max-w-3xl">
            <Activity className="mx-auto h-7 w-7 text-primary" aria-hidden="true" />
            <h2 className="mt-6 text-4xl font-normal leading-[1.06] tracking-[-0.04em] sm:text-6xl">
              Make agent work legible.
            </h2>
            <p className="mx-auto mt-6 max-w-xl text-base leading-7 text-body">
              Run Enigma locally, connect a repository, and keep the entire development
              loop in view.
            </p>
            <div className="mt-8 flex flex-wrap items-center justify-center gap-3">
              <a className="landing-button-dark" href="#quickstart">
                View quickstart
                <ArrowRight className="h-4 w-4" aria-hidden="true" />
              </a>
              <Link className="landing-button-secondary" to="/login">
                Sign in
              </Link>
            </div>
          </div>
        </section>
      </main>

      <footer className="border-t border-line px-5 py-8 lg:px-8">
        <div className="mx-auto flex max-w-[1200px] flex-col gap-6 text-sm text-body sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-center gap-3">
            <span className="flex h-8 w-8 items-center justify-center rounded-md bg-primary text-white">
              <Workflow className="h-4 w-4" aria-hidden="true" />
            </span>
            <span className="font-medium text-ink">Enigma</span>
            <span className="text-muted">Self-hosted AI development control room.</span>
          </div>
          <div className="flex items-center gap-6">
            <a className="landing-text-link" href="#product">
              Product
            </a>
            <a className="landing-text-link" href="#quickstart">
              Quickstart
            </a>
            <Link className="landing-text-link" to="/login">
              Sign in
            </Link>
          </div>
        </div>
      </footer>
    </div>
  );
}

function AgentFlightRecorder() {
  return (
    <div
      className="overflow-hidden rounded-xl border border-line-strong bg-white"
      aria-label="Illustration of an observable Enigma agent run"
    >
      <div className="flex min-h-12 flex-wrap items-center justify-between gap-3 border-b border-line bg-panel px-4 py-2">
        <div className="flex items-center gap-3">
          <div className="flex gap-1.5" aria-hidden="true">
            <span className="h-2.5 w-2.5 rounded-full bg-line-strong" />
            <span className="h-2.5 w-2.5 rounded-full bg-line-strong" />
            <span className="h-2.5 w-2.5 rounded-full bg-line-strong" />
          </div>
          <span className="font-mono text-[11px] text-muted">enigma / storefront</span>
        </div>
        <div className="flex items-center gap-2 text-[11px] text-body">
          <span className="h-1.5 w-1.5 rounded-full bg-signal" />
          workspace ready
        </div>
      </div>

      <div className="grid min-h-[470px] lg:grid-cols-[0.95fr_1.25fr_0.9fr]">
        <div className="border-b border-line bg-panel p-4 sm:p-5 lg:border-b-0 lg:border-r">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold uppercase tracking-[0.14em] text-muted">
              Agent run
            </span>
            <span className="rounded-full border border-line bg-white px-2.5 py-1 font-mono text-[10px] text-body">
              02:14
            </span>
          </div>
          <div className="mt-5 rounded-lg border border-line bg-white p-4">
            <p className="text-sm leading-6 text-ink">
              Add an order summary to checkout and verify the total updates correctly.
            </p>
          </div>
          <div className="relative mt-6 space-y-5 before:absolute before:bottom-2 before:left-[9px] before:top-2 before:w-px before:bg-line-strong">
            <TimelineRow tone="thinking" label="Thinking" detail="Trace checkout state" delay="0ms" />
            <TimelineRow tone="grep" label="Grepping" detail="OrderSummary references" delay="120ms" />
            <TimelineRow tone="read" label="Reading" detail="3 source files" delay="240ms" />
            <TimelineRow tone="edit" label="Editing" detail="CheckoutPanel.tsx" delay="360ms" />
            <TimelineRow tone="done" label="Done" detail="Tests passed" delay="480ms" />
          </div>
        </div>

        <div className="hidden border-r border-line bg-white sm:block">
          <div className="flex h-11 items-center justify-between border-b border-line px-4">
            <div className="flex items-center gap-2 font-mono text-[11px] text-body">
              <FileCode2 className="h-3.5 w-3.5" aria-hidden="true" />
              CheckoutPanel.tsx
            </div>
            <span className="font-mono text-[10px] text-signal">+18 −2</span>
          </div>
          <div className="overflow-hidden p-4 font-mono text-[11px] leading-6 text-body">
            <CodeLine number="44" text="const subtotal = cart.items.reduce(" />
            <CodeLine number="45" text="  (sum, item) => sum + item.total," />
            <CodeLine number="46" text="  0" />
            <CodeLine number="47" text=");" />
            <CodeLine number="48" text="" />
            <CodeLine number="49" text="<OrderSummary" added />
            <CodeLine number="50" text="  subtotal={subtotal}" added />
            <CodeLine number="51" text="  shipping={shipping}" added />
            <CodeLine number="52" text="  total={subtotal + shipping}" added />
            <CodeLine number="53" text="/>" added />
            <CodeLine number="54" text="" />
            <CodeLine number="55" text="<CheckoutActions />" />
          </div>
          <div className="mx-4 mt-1 border-t border-line pt-4">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-[0.14em] text-muted">
                Changed files
              </span>
              <span className="text-xs text-body">2 files</span>
            </div>
            <div className="mt-3 space-y-2 font-mono text-[10px] text-body">
              <div className="flex justify-between rounded-md bg-panel px-3 py-2">
                <span>CheckoutPanel.tsx</span>
                <span className="text-signal">M</span>
              </div>
              <div className="flex justify-between rounded-md bg-panel px-3 py-2">
                <span>checkout.test.tsx</span>
                <span className="text-signal">M</span>
              </div>
            </div>
          </div>
        </div>

        <div className="hidden bg-panel lg:block">
          <div className="border-b border-line">
            <div className="flex h-11 items-center gap-2 border-b border-line bg-white px-4 font-mono text-[11px] text-body">
              <Terminal className="h-3.5 w-3.5" aria-hidden="true" />
              test output
            </div>
            <div className="min-h-[190px] bg-[#26251e] p-4 font-mono text-[10px] leading-5 text-[#cfcdc4]">
              <p>
                <span className="text-[#9fc9a2]">$</span> pnpm test checkout
              </p>
              <p className="mt-3 text-[#9fbbe0]">RUN v2.1.1</p>
              <p className="mt-3">
                <span className="text-[#9fc9a2]">✓</span> updates subtotal
              </p>
              <p>
                <span className="text-[#9fc9a2]">✓</span> includes shipping
              </p>
              <p>
                <span className="text-[#9fc9a2]">✓</span> renders order summary
              </p>
              <p className="mt-3 text-[#9fc9a2]">3 tests passed</p>
            </div>
          </div>
          <div className="p-4">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-[0.14em] text-muted">
                Preview
              </span>
              <span className="flex items-center gap-1.5 text-[10px] text-signal">
                <span className="h-1.5 w-1.5 rounded-full bg-signal" />
                :5173
              </span>
            </div>
            <div className="mt-3 overflow-hidden rounded-lg border border-line bg-white">
              <div className="flex h-8 items-center gap-1.5 border-b border-line px-3">
                <span className="h-1.5 w-1.5 rounded-full bg-line-strong" />
                <span className="h-1.5 w-1.5 rounded-full bg-line-strong" />
                <span className="h-1.5 w-1.5 rounded-full bg-line-strong" />
              </div>
              <div className="p-4">
                <div className="h-2 w-16 rounded-full bg-line" />
                <div className="mt-5 grid grid-cols-[1fr_70px] gap-4">
                  <div className="space-y-2">
                    <div className="h-2 rounded-full bg-line-soft" />
                    <div className="h-2 w-4/5 rounded-full bg-line-soft" />
                    <div className="h-2 w-3/5 rounded-full bg-line-soft" />
                  </div>
                  <div className="rounded-md border border-line bg-panel p-2">
                    <div className="h-2 w-full rounded-full bg-line" />
                    <div className="mt-2 h-2 w-2/3 rounded-full bg-primary/40" />
                  </div>
                </div>
                <div className="mt-5 h-8 rounded-md bg-ink" />
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function TimelineRow({
  tone,
  label,
  detail,
  delay,
}: {
  tone: "thinking" | "grep" | "read" | "edit" | "done";
  label: string;
  detail: string;
  delay: string;
}) {
  const toneClasses = {
    thinking: "bg-timeline-thinking",
    grep: "bg-timeline-grep",
    read: "bg-timeline-read",
    edit: "bg-timeline-edit",
    done: "bg-timeline-done text-white",
  };

  return (
    <div
      className="landing-timeline-row relative flex items-center gap-3"
      style={{ animationDelay: delay }}
    >
      <span className={`relative z-10 h-[19px] w-[19px] rounded-full border-4 border-panel ${toneClasses[tone]}`} />
      <div className="min-w-0">
        <span
          className={`inline-flex rounded-full px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.1em] ${toneClasses[tone]}`}
        >
          {label}
        </span>
        <p className="mt-1 truncate text-xs text-body">{detail}</p>
      </div>
    </div>
  );
}

function CodeLine({
  number,
  text,
  added = false,
}: {
  number: string;
  text: string;
  added?: boolean;
}) {
  return (
    <div className={`-mx-4 flex px-4 ${added ? "bg-[#eef8ef] text-ink" : ""}`}>
      <span className="w-8 shrink-0 select-none text-muted-soft">{number}</span>
      <span className="mr-2 w-2 shrink-0 select-none text-signal">{added ? "+" : " "}</span>
      <span className="whitespace-pre">{text || " "}</span>
    </div>
  );
}

function RuntimeDiagram() {
  return (
    <div className="rounded-xl border border-white/15 bg-[#2d2c25] p-4 sm:p-6">
      <div className="flex items-center justify-between border-b border-white/10 pb-4">
        <span className="font-mono text-xs text-[#cfcdc4]">project.workspace</span>
        <span className="flex items-center gap-2 rounded-full border border-white/10 px-3 py-1 font-mono text-[10px] text-[#9fc9a2]">
          <span className="h-1.5 w-1.5 rounded-full bg-[#9fc9a2]" />
          isolated
        </span>
      </div>
      <div className="grid gap-3 py-5 sm:grid-cols-2">
        <RuntimeCell icon={GitBranch} label="repository" value="main · clean" />
        <RuntimeCell icon={Bot} label="agent" value="one active run" />
        <RuntimeCell icon={Terminal} label="terminal" value="/workspace" />
        <RuntimeCell icon={Eye} label="preview" value="port detected" />
      </div>
      <div className="flex flex-col gap-4 border-t border-white/10 pt-5 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-3">
          <span className="flex h-9 w-9 items-center justify-center rounded-md border border-white/10 bg-white/5">
            <Server className="h-4 w-4 text-[#9fbbe0]" aria-hidden="true" />
          </span>
          <div>
            <p className="text-sm text-[#efeee8]">Daytona or Docker sandbox</p>
            <p className="mt-0.5 font-mono text-[10px] text-[#a09c92]">persistent project runtime</p>
          </div>
        </div>
        <div className="flex items-center gap-2 text-xs text-[#cfcdc4]">
          <ShieldCheck className="h-4 w-4 text-[#9fc9a2]" aria-hidden="true" />
          controlled access
        </div>
      </div>
    </div>
  );
}

function RuntimeCell({
  icon: Icon,
  label,
  value,
}: {
  icon: typeof Code2;
  label: string;
  value: string;
}) {
  return (
    <div className="rounded-lg border border-white/10 bg-white/[0.035] p-4">
      <div className="flex items-center gap-2 text-[#a09c92]">
        <Icon className="h-3.5 w-3.5" aria-hidden="true" />
        <span className="font-mono text-[10px]">{label}</span>
      </div>
      <p className="mt-5 text-sm text-[#efeee8]">{value}</p>
    </div>
  );
}
