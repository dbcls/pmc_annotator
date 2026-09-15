import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

function argument(name) {
  const position = process.argv.indexOf(name);
  if (position < 0 || !process.argv[position + 1]) throw new Error(`Missing ${name}`);
  return process.argv[position + 1];
}

function parseTsv(text) {
  const [header, ...lines] = text.trim().split(/\r?\n/);
  const columns = header.split("\t");
  return lines.filter(Boolean).map(line => Object.fromEntries(
    columns.map((column, index) => [column, line.split("\t")[index] ?? ""])));
}

function matrix(rows, columns) {
  return [columns, ...rows.map(row => columns.map(column => row[column] ?? null))];
}

function addTableSheet(workbook, name, rows, columns) {
  const sheet = workbook.worksheets.add(name);
  sheet.showGridLines = false;
  const values = matrix(rows, columns);
  sheet.getRangeByIndexes(0, 0, values.length, columns.length).values = values;
  const table = sheet.tables.add(
    `A1:${String.fromCharCode(64 + columns.length)}${values.length}`, true,
    `${name.replace(/[^A-Za-z0-9]/g, "")}Table`);
  table.style = "TableStyleMedium2";
  sheet.getRange(`A1:${String.fromCharCode(64 + columns.length)}1`).format = {
    fill: "#1F4E78", font: { name: "Arial", bold: true, color: "#FFFFFF" },
    horizontalAlignment: "center", verticalAlignment: "center",
  };
  sheet.getUsedRange().format.font = { name: "Arial", size: 10 };
  sheet.getUsedRange().format.autofitColumns();
  sheet.getUsedRange().format.autofitRows();
  sheet.freezePanes.freezeRows(1);
  return sheet;
}

const comparisonPath = argument("--comparison");
const rejectedPath = argument("--rejected");
const outputPath = argument("--output");
const comparison = parseTsv(await fs.readFile(comparisonPath, "utf8"));
const rejected = JSON.parse(await fs.readFile(rejectedPath, "utf8"));
const membershipOrder = ["both", "togoid_only", "europepmc_only"];
const count = membership => comparison.filter(row => row.membership === membership).length;
const byDatabase = [...new Set(comparison.map(row => row.db))].sort().map(db => ({
  db,
  both: comparison.filter(row => row.db === db && row.membership === "both").length,
  togoid_only: comparison.filter(row => row.db === db && row.membership === "togoid_only").length,
  europepmc_only: comparison.filter(row => row.db === db && row.membership === "europepmc_only").length,
}));

const workbook = Workbook.create();
const summary = workbook.worksheets.add("Summary");
summary.showGridLines = false;
summary.getRange("A2").values = [["Europe PMC and TogoID accession comparison"]];
summary.getRange("A2").format.font = { name: "Arial", size: 14, bold: true, color: "#1F1F1F" };
summary.getRange("A4:B7").values = [
  ["Membership", "Unique accessions"],
  ["Both", count("both")],
  ["TogoID only", count("togoid_only")],
  ["Europe PMC only", count("europepmc_only")],
];
summary.getRange("A4:B4").format = { fill: "#1F4E78", font: { name: "Arial", bold: true, color: "#FFFFFF" } };
summary.getRange("A4:B7").format.borders = { preset: "outside", style: "thin", color: "#B7C9D6" };
summary.getRange("D4:G4").values = [["Database", "Both", "TogoID only", "Europe PMC only"]];
summary.getRangeByIndexes(4, 3, byDatabase.length, 4).values = byDatabase.map(row => [row.db, row.both, row.togoid_only, row.europepmc_only]);
summary.getRange(`D4:G${4 + byDatabase.length}`).format.borders = { preset: "outside", style: "thin", color: "#B7C9D6" };
summary.getRange("D4:G4").format = { fill: "#1F4E78", font: { name: "Arial", bold: true, color: "#FFFFFF" } };
summary.getRange("A29").values = [["Rejected rows need XML-alignment review; they are not evidence of a false annotation."]];
summary.getRange("A29:H29").format.font = { name: "Arial", size: 10, italic: true, color: "#595959" };
summary.getRange("A:A").format.columnWidth = 22;
summary.getRange("B:B").format.columnWidth = 18;
summary.getRange("D:D").format.columnWidth = 20;
summary.getRange("E:G").format.columnWidth = 16;

const overlapChart = summary.charts.add("bar", summary.getRange("A4:B7"));
overlapChart.title = "Comparison overlap";
overlapChart.hasLegend = false;
overlapChart.titleTextStyle.typeface = "Arial";
overlapChart.setPosition("A11", "H27");
const databaseChart = summary.charts.add("bar", summary.getRange(`D4:G${4 + byDatabase.length}`));
databaseChart.title = "Comparison by database";
databaseChart.legend = { position: "top", textStyle: { typeface: "Arial" } };
databaseChart.titleTextStyle.typeface = "Arial";
databaseChart.setPosition("I4", "Q27");

const columns = ["doc_id", "db", "surface", "membership"];
addTableSheet(workbook, "Both", comparison.filter(row => row.membership === "both"), columns);
addTableSheet(workbook, "Europe PMC", comparison.filter(row => row.membership !== "togoid_only"), columns);
addTableSheet(workbook, "TogoID", comparison.filter(row => row.membership !== "europepmc_only"), columns);
addTableSheet(workbook, "Comparison", comparison, columns);
addTableSheet(workbook, "TogoID only", comparison.filter(row => row.membership === "togoid_only"), columns);
addTableSheet(workbook, "Europe PMC only", comparison.filter(row => row.membership === "europepmc_only"), columns);
const review = rejected.map(item => ({
  doc_id: item.doc_id,
  reason: item.reason,
  epmc_label: item.annotation?.memberOf ?? "",
  identifier: item.annotation?.target?.selector?.exact ?? "",
  identifiers_url: typeof item.annotation?.body === "string" ? item.annotation.body : "",
  target_section: item.annotation?.target?.isPartOf ?? "",
  target_prefix: item.annotation?.target?.selector?.prefix ?? "",
  target_suffix: item.annotation?.target?.selector?.suffix ?? "",
  local_candidate_count: item.local_candidates?.length ?? "",
  local_candidate_contexts: (item.local_candidates ?? []).map(candidate =>
    `passage ${candidate.passage_idx}: ${candidate.context}`).join(" | "),
}));
addTableSheet(workbook, "Rejected review", review,
  ["doc_id", "reason", "epmc_label", "identifier", "identifiers_url", "target_section",
   "target_prefix", "target_suffix", "local_candidate_count", "local_candidate_contexts"]);

workbook.recalculate();
await fs.mkdir(path.dirname(outputPath), { recursive: true });
const preview = await workbook.render({ sheetName: "Summary", autoCrop: "all", scale: 1, format: "png" });
await fs.writeFile(`${outputPath}.preview.png`, new Uint8Array(await preview.arrayBuffer()));
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
