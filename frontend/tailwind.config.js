/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: Object.fromEntries(
        [
          "background",
          "surface",
          "muted",
          "border",
          "foreground",
          "primary",
          "primary-strong",
          "accent",
          "danger",
          "selected",
        ].map((name) => [name, `rgb(var(--${name}) / <alpha-value>)`]),
      ),
      fontFamily: {
        sans: ["system-ui", '"Microsoft YaHei"', "sans-serif"],
      },
    },
  },
  plugins: [],
};
