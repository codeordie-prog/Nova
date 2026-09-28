/** @type {import('md-to-pdf/dist/lib/config').Config} */
const markedKatex = require("marked-katex-extension");

module.exports = {
  marked_extensions: [
    markedKatex({
      throwOnError: false,
      nonStandard: true,
      output: "html",
    }),
  ],
  stylesheet: [
    "pdf_styles.css",
    "https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css",
  ],
  pdf_options: {
    format: "A4",
    margin: { top: "18mm", bottom: "20mm", left: "16mm", right: "16mm" },
    printBackground: true,
  },
  launch_options: {
    executablePath:
      "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  },
  body_class: "decision-confidence-paper",
  css: `
    .katex-display {
      margin: 0.75em 0;
      padding: 0.25em 0;
      overflow-x: auto;
      overflow-y: hidden;
      page-break-inside: avoid;
    }
    .katex-display > .katex {
      font-size: 1.05em;
    }
    .katex {
      font-size: 1.02em;
    }
  `,
};
