/**
 * Build decision_confidence.pdf from decision_confidence.md (KaTeX via md-to-pdf + Edge).
 */
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function stripEditorNotes(source) {
  const lines = [];
  let skipPreview = false;
  for (const line of source.split(/\r?\n/)) {
    if (line.trim().startsWith("> **Preview:**")) {
      skipPreview = true;
      continue;
    }
    if (skipPreview) {
      if (line.trim().startsWith(">")) continue;
      skipPreview = false;
    }
    if (line.trim().startsWith("*Reviewer checkpoint:")) continue;
    lines.push(line);
  }
  return lines.join("\n");
}

const mdPath = path.join(__dirname, "decision_confidence.md");
const printMdPath = path.join(__dirname, ".decision_confidence_print.md");
const pdfPath = path.join(__dirname, "decision_confidence.pdf");
const configPath = path.join(__dirname, "pdf_config.js");

const md = stripEditorNotes(fs.readFileSync(mdPath, "utf8"));
fs.writeFileSync(printMdPath, md, "utf8");

const result = spawnSync(
  process.platform === "win32" ? "npx.cmd" : "npx",
  ["md-to-pdf", printMdPath, "--config-file", configPath],
  { cwd: __dirname, stdio: "inherit", shell: true },
);

fs.unlinkSync(printMdPath);
const builtPdf = path.join(__dirname, ".decision_confidence_print.pdf");
if (fs.existsSync(builtPdf)) {
  fs.renameSync(builtPdf, pdfPath);
}

if (result.status !== 0) {
  process.exit(result.status ?? 1);
}

console.log(`Wrote ${pdfPath}`);
