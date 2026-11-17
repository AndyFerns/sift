import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { describeAllocated, describeSize, formatBytes, statusLabel } from "../lib/format";
import { fixture, mockApi, renderApp, startup } from "../test/helpers";
import { Explorer } from "./Explorer";
import { Overview } from "./Overview";

const { scan } = fixture;

function rows(): HTMLElement[] {
  return screen.queryAllByRole("row").filter((row) => row.dataset.artifact !== undefined);
}

async function loaded() {
  await screen.findByText(`Showing ${scan.artifacts.length} of ${scan.artifacts.length} artifacts`);
}

describe("Explorer", () => {
  it("shows every artifact from the scan with the scanner's own values", async () => {
    mockApi(startup());
    renderApp(<Explorer />);
    await loaded();

    expect(rows().map((row) => row.dataset.artifact)).toEqual(scan.artifacts.map((a) => a.id));
    for (const artifact of scan.artifacts) {
      const row = rows().find((r) => r.dataset.artifact === artifact.id)!;
      const cells = within(row).getAllByRole("cell");
      expect(cells[1]!.textContent).toBe(artifact.relative_path);
      expect(cells[2]!.textContent).toBe(artifact.rule);
      expect(cells[3]!.textContent).toBe(artifact.confidence + (artifact.ambiguous ? " ?" : ""));
      expect(cells[4]!.textContent).toBe(statusLabel(artifact));
      expect(cells[5]!.textContent).toBe(describeSize(artifact.size));
      expect(cells[6]!.textContent).toBe(describeAllocated(artifact.size));
      expect(cells[7]!.textContent).toBe(artifact.size!.file_count.toLocaleString("en-US"));
    }
  });

  it("only lets cleanable artifacts be selected", async () => {
    mockApi(startup());
    renderApp(<Explorer />);
    await loaded();
    for (const artifact of scan.artifacts) {
      const box = screen.getByRole("checkbox", { name: `Select ${artifact.relative_path}` });
      expect((box as HTMLInputElement).disabled).toBe(!artifact.cleanable);
      if (!artifact.cleanable) expect(box.title).toContain("Cannot be cleaned");
    }
  });

  it("filters by search text and ecosystem", async () => {
    const user = userEvent.setup();
    mockApi(startup());
    renderApp(<Explorer />);
    await loaded();

    await user.type(screen.getByRole("searchbox", { name: "Search artifacts" }), "target");
    const matching = scan.artifacts.filter((a) => a.relative_path.includes("target"));
    expect(rows()).toHaveLength(matching.length);
    expect(screen.getByTestId("showing").textContent).toContain(
      `Showing ${matching.length} of ${scan.artifacts.length} artifacts`,
    );

    await user.click(screen.getByRole("button", { name: /^Rust \(/ }));
    const rust = matching.filter((a) => a.ecosystem === "rust");
    expect(rows().map((r) => r.dataset.artifact)).toEqual(rust.map((a) => a.id));

    await user.click(screen.getByRole("button", { name: "clear filters" }));
    expect(rows()).toHaveLength(scan.artifacts.length);
  });

  it("explains when filters hide everything", async () => {
    const user = userEvent.setup();
    mockApi(startup());
    renderApp(<Explorer />);
    await loaded();
    await user.type(screen.getByRole("searchbox", { name: "Search artifacts" }), "zzz-nothing");
    expect(screen.getByText("No artifacts match these filters")).toBeTruthy();
    expect(rows()).toHaveLength(0);
  });

  it("sorts and reverses", async () => {
    const user = userEvent.setup();
    mockApi(startup());
    renderApp(<Explorer />);
    await loaded();
    await user.selectOptions(screen.getByRole("combobox", { name: "Sort by" }), "name");
    const names = rows().map((row) => within(row).getAllByRole("cell")[1]!.textContent!);
    expect(names).toEqual(
      [...names].sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase(), "en")),
    );
    await user.click(screen.getByRole("button", { name: "Reverse" }));
    expect(rows().map((row) => within(row).getAllByRole("cell")[1]!.textContent)).toEqual(
      [...names].reverse(),
    );
  });

  it("totals the selection and offers a cleanup review", async () => {
    const user = userEvent.setup();
    mockApi(startup());
    renderApp(<Explorer />);
    await loaded();
    expect(screen.queryByTestId("selection-summary")).toBeNull();

    const [first, second] = scan.artifacts.filter((a) => a.cleanable);
    await user.click(screen.getByRole("checkbox", { name: `Select ${first!.relative_path}` }));
    await user.click(screen.getByRole("checkbox", { name: `Select ${second!.relative_path}` }));
    const bytes = first!.size!.logical_bytes + second!.size!.logical_bytes;
    expect(screen.getByTestId("selection-summary").textContent).toBe(
      `2 artifacts selected · ${formatBytes(bytes)}`,
    );
    expect(screen.getByRole("button", { name: "Review cleanup…" })).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Clear selection" }));
    expect(screen.queryByTestId("selection-summary")).toBeNull();
  });

  it("select-all takes only the cleanable artifacts that are showing", async () => {
    const user = userEvent.setup();
    mockApi(startup());
    renderApp(<Explorer />);
    await loaded();
    await user.click(screen.getByRole("button", { name: /^Python \(/ }));
    await user.click(
      screen.getByRole("checkbox", { name: "Select every cleanable artifact shown" }),
    );
    const python = scan.artifacts.filter((a) => a.ecosystem === "python" && a.cleanable);
    const bytes = python.reduce((sum, a) => sum + a.size!.logical_bytes, 0);
    expect(screen.getByTestId("selection-summary").textContent).toBe(
      `${python.length} artifacts selected · ${formatBytes(bytes)}`,
    );
  });

  it("opens the detail panel with a fresh check from the API", async () => {
    const user = userEvent.setup();
    const artifact = fixture.detail.artifact;
    mockApi({
      ...startup(),
      [`GET /api/scans/${scan.id}/artifacts/${artifact.id}`]: () => fixture.detail,
    });
    renderApp(<Explorer />);
    await loaded();
    await user.click(screen.getByRole("button", { name: artifact.relative_path }));
    const panel = await screen.findByRole("complementary");
    expect(within(panel).getByText(artifact.path)).toBeTruthy();
    expect(within(panel).getByText("Cargo.toml is next to it")).toBeTruthy();
    await waitFor(() =>
      expect(within(panel).getByTestId("cleanup-verdict").textContent).toBe(
        "Cleanup allowed, after preview and confirmation.",
      ),
    );
    for (const child of fixture.detail.children) {
      expect(within(panel).getByText(formatBytes(child.logical_bytes))).toBeTruthy();
    }
  });

  it("offers JSON and CSV exports of the scan on screen", async () => {
    mockApi(startup());
    renderApp(<Explorer />);
    await loaded();
    expect(screen.getByRole("link", { name: "Export JSON" }).getAttribute("href")).toBe(
      `/api/scans/${scan.id}/report?format=json`,
    );
    expect(screen.getByRole("link", { name: "Export CSV" }).getAttribute("href")).toBe(
      `/api/scans/${scan.id}/report?format=csv`,
    );
  });

  it("has an empty state before anything is scanned", async () => {
    mockApi(startup(null));
    renderApp(<Explorer />);
    expect(await screen.findByText("Nothing to explore yet")).toBeTruthy();
  });
});

describe("Overview", () => {
  it("shows the scan's real totals", async () => {
    mockApi(startup());
    renderApp(<Overview />);
    const detected = await screen.findByTestId("total-detected");
    expect(detected.textContent).toBe(formatBytes(scan.totals.logical_bytes));
    expect(screen.getByTestId("total-cleanable").textContent).toBe(
      formatBytes(scan.totals.cleanable_logical_bytes),
    );
    expect(screen.getByText(`${scan.totals.cleanable_count} artifacts`)).toBeTruthy();
    expect(screen.getAllByText(scan.artifacts[0]!.relative_path).length).toBeGreaterThan(0);
    expect(screen.getByText("nested/outer/inner")).toBeTruthy();
  });

  it("prompts for a scan when there is none", async () => {
    mockApi(startup(null));
    renderApp(<Overview />);
    expect(await screen.findByText("Nothing scanned yet")).toBeTruthy();
    expect(screen.queryByTestId("total-detected")).toBeNull();
  });

  it("explains how to add scan roots when none are configured", async () => {
    mockApi(startup(null, { ...fixture.config, roots: [] }));
    renderApp(<Overview />);
    expect(await screen.findByText("No scan roots configured")).toBeTruthy();
    expect(screen.getByText(/sift serve --root PATH/)).toBeTruthy();
  });
});
