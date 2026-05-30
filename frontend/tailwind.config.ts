import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        primary: "#f54e00",
        "primary-active": "#d04200",
        canvas: "#f7f7f4",
        panel: "#fafaf7",
        ink: "#26251e",
        body: "#5a5852",
        muted: "#807d72",
        "muted-soft": "#a09c92",
        line: "#e6e5e0",
        "line-soft": "#efeee8",
        "line-strong": "#cfcdc4",
        "timeline-thinking": "#dfa88f",
        "timeline-grep": "#9fc9a2",
        "timeline-read": "#9fbbe0",
        "timeline-edit": "#c0a8dd",
        "timeline-done": "#c08532",
        signal: "#1f8a65",
        warn: "#c08532",
        danger: "#cf2d56"
      },
      boxShadow: {
        soft: "none"
      }
    }
  },
  plugins: []
} satisfies Config;
