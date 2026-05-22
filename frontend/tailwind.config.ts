import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#172026",
        panel: "#f7f5ef",
        line: "#d8d3c7",
        signal: "#247c6d",
        warn: "#a15c1b",
        danger: "#b42318"
      },
      boxShadow: {
        soft: "0 12px 36px rgba(23, 32, 38, 0.08)"
      }
    }
  },
  plugins: []
} satisfies Config;
