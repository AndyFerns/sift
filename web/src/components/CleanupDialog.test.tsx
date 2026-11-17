import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { CleanupResult } from "../api/schema";
import { formatBytes } from "../lib/format";
import { apiError, fixture, mockApi, type Call } from "../test/helpers";
import { CleanupDialog } from "./CleanupDialog";

const { plan, scan } = fixture;
const ids = plan.items.map((item) => item.artifact_id);
const deletable = plan.items.filter((item) => item.allowed);
const refused = plan.items.filter((item) => !item.allowed);

const cancelled: CleanupResult = {
  ...fixture.dryRunResult,
  id: "cancelled",
  plan_id: plan.id,
  dry_run: false,
  cancelled: true,
};

const deleted: CleanupResult = {
  ...fixture.dryRunResult,
  id: "done",
  plan_id: plan.id,
  dry_run: false,
  cancelled: false,
  items: plan.items.map((item) => ({
    artifact_id: item.artifact_id,
    name: item.name,
    path: item.path,
    relative_path: item.relative_path,
    status: item.allowed ? "deleted" : "refused",
    selected_bytes: item.logical_bytes,
    deleted_bytes: item.allowed ? item.logical_bytes : 0,
    failed_bytes: 0,
    deleted_files: item.allowed ? item.file_count : 0,
    message: item.allowed ? null : `refused: ${item.refusal_reason}`,
    errors: [],
  })),
  totals: {
    ...fixture.dryRunResult.totals,
    selected_bytes: plan.totals.selected_bytes,
    deleted_bytes: plan.totals.deletable_bytes,
    refused_bytes: plan.totals.refused_bytes,
    would_delete_bytes: 0,
    deleted_count: deletable.length,
    refused_count: refused.length,
  },
};

function open(extra: Record<string, (body: unknown) => unknown> = {}) {
  const onClose = vi.fn();
  const calls = mockApi({
    "POST /api/cleanup/preview": () => plan,
    "POST /api/cleanup/cancel": () => cancelled,
    ...extra,
  });
  render(<CleanupDialog scanId={scan.id} artifactIds={ids} onClose={onClose} />);
  return { onClose, calls };
}

function sent(calls: Call[], path: string): Call[] {
  return calls.filter((call) => call.path === path);
}

const deleteButton = () => screen.getByRole("button", { name: "Delete permanently" });

describe("CleanupDialog", () => {
  it("asks the API for a preview of exactly the selection", async () => {
    const { calls } = open();
    await screen.findByText("Cleanup preview");
    expect(sent(calls, "/api/cleanup/preview")).toEqual([
      {
        method: "POST",
        path: "/api/cleanup/preview",
        body: { scan_id: scan.id, artifact_ids: ids, dry_run: false },
      },
    ]);
  });

  it("lists exactly what will be deleted and what is refused", async () => {
    open();
    const table = await screen.findByTestId("will-delete");
    for (const item of deletable) {
      const row = within(table).getByText(item.path).closest("tr")!;
      expect(within(row).getByText(item.rule)).toBeTruthy();
      expect(within(row).getByText(formatBytes(item.logical_bytes))).toBeTruthy();
    }
    expect(within(table).getAllByRole("row")).toHaveLength(deletable.length + 1);

    const refusedTable = screen.getByTestId("refused");
    for (const item of refused) {
      expect(within(refusedTable).getByText(item.path)).toBeTruthy();
      expect(within(refusedTable).getByText(item.refusal_reason!)).toBeTruthy();
      expect(within(table).queryByText(item.path)).toBeNull();
    }
    expect(screen.getByTestId("to-delete").textContent).toBe(
      `${formatBytes(plan.totals.deletable_bytes)} · ${deletable.length} artifacts · ` +
        `${plan.totals.deletable_file_count} files`,
    );
    expect(screen.getByText("Deletion is permanent.")).toBeTruthy();
  });

  it("starts with Cancel focused and Delete disabled", async () => {
    open();
    await screen.findByText("Cleanup preview");
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Cancel — delete nothing" }),
    );
    expect((deleteButton() as HTMLButtonElement).disabled).toBe(true);
  });

  it("backs out on Cancel without deleting anything", async () => {
    const user = userEvent.setup();
    const { onClose, calls } = open();
    await user.click(await screen.findByRole("button", { name: "Cancel — delete nothing" }));
    await waitFor(() => expect(onClose).toHaveBeenCalledWith(false));
    expect(sent(calls, "/api/cleanup/cancel")).toEqual([
      { method: "POST", path: "/api/cleanup/cancel", body: { plan_id: plan.id } },
    ]);
    expect(sent(calls, "/api/cleanup/execute")).toEqual([]);
  });

  it("backs out on Escape", async () => {
    const user = userEvent.setup();
    const { onClose, calls } = open();
    await screen.findByText("Cleanup preview");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(onClose).toHaveBeenCalledWith(false));
    expect(sent(calls, "/api/cleanup/cancel")).toHaveLength(1);
    expect(sent(calls, "/api/cleanup/execute")).toEqual([]);
  });

  it("backs out on Enter, because Cancel has the focus", async () => {
    const user = userEvent.setup();
    const { onClose, calls } = open();
    await screen.findByText("Cleanup preview");
    await user.keyboard("{Enter}");
    await waitFor(() => expect(onClose).toHaveBeenCalledWith(false));
    expect(sent(calls, "/api/cleanup/execute")).toEqual([]);
  });

  it.each(["yes", "y", "delet", "delete it", "remove"])(
    "keeps Delete disabled when %j is typed",
    async (word) => {
      const user = userEvent.setup();
      const { calls } = open();
      await screen.findByText("Cleanup preview");
      await user.type(screen.getByRole("textbox"), word);
      expect((deleteButton() as HTMLButtonElement).disabled).toBe(true);
      await user.click(deleteButton());
      await user.keyboard("{Enter}");
      expect(sent(calls, "/api/cleanup/execute")).toEqual([]);
    },
  );

  it("deletes only after the word is typed, sending the token and the word", async () => {
    const user = userEvent.setup();
    const { onClose, calls } = open({ "POST /api/cleanup/execute": () => deleted });
    await screen.findByText("Cleanup preview");
    await user.type(screen.getByRole("textbox"), "Delete");
    expect((deleteButton() as HTMLButtonElement).disabled).toBe(false);
    expect(sent(calls, "/api/cleanup/execute")).toEqual([]); // typing alone does nothing

    await user.click(deleteButton());
    await screen.findByText("Cleanup finished");
    expect(sent(calls, "/api/cleanup/execute")).toEqual([
      {
        method: "POST",
        path: "/api/cleanup/execute",
        body: {
          plan_id: plan.id,
          confirmation_token: plan.confirmation_token,
          confirmation: "Delete",
        },
      },
    ]);
    expect(sent(calls, "/api/cleanup/cancel")).toEqual([]);
    expect(screen.getByTestId("deleted").textContent).toBe(
      formatBytes(plan.totals.deletable_bytes),
    );
    const result = screen.getByTestId("result");
    expect(within(result).getAllByText("deleted")).toHaveLength(deletable.length);
    expect(within(result).getAllByText("refused")).toHaveLength(refused.length);

    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledWith(true);
  });

  it("runs a dry run that deletes nothing", async () => {
    const user = userEvent.setup();
    const dryPlan = { ...plan, id: "dry-plan", dry_run: true };
    let previews = 0;
    const { onClose, calls } = open({
      "POST /api/cleanup/preview": () => (previews++ === 0 ? plan : dryPlan),
      "POST /api/cleanup/execute": () => fixture.dryRunResult,
    });
    await user.click(await screen.findByRole("button", { name: "Dry run" }));
    await screen.findByText("Dry run — nothing was deleted");
    expect(sent(calls, "/api/cleanup/preview")[1]!.body).toEqual({
      scan_id: scan.id,
      artifact_ids: ids,
      dry_run: true,
    });
    expect(sent(calls, "/api/cleanup/execute")[0]!.body).toEqual({
      plan_id: "dry-plan",
      confirmation_token: "",
      confirmation: "",
    });
    expect(screen.getByTestId("deleted").textContent).toBe("0 B");
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledWith(false);
  });

  it("offers no confirmation when nothing can be deleted", async () => {
    const nothing = {
      ...plan,
      items: refused,
      totals: { ...plan.totals, deletable_count: 0, deletable_bytes: 0, deletable_file_count: 0 },
    };
    open({ "POST /api/cleanup/preview": () => nothing });
    await screen.findByText("Nothing in the selection can be deleted.");
    expect(screen.queryByRole("textbox")).toBeNull();
    expect((deleteButton() as HTMLButtonElement).disabled).toBe(true);
  });

  it("reports a refusal from the server instead of pretending it worked", async () => {
    const user = userEvent.setup();
    const { onClose } = open({
      "POST /api/cleanup/execute": () =>
        apiError(409, "expired", "the preview has expired; build a new one"),
    });
    await screen.findByText("Cleanup preview");
    await user.type(screen.getByRole("textbox"), "delete");
    await user.click(deleteButton());
    expect((await screen.findByRole("alert")).textContent).toBe(
      "the preview has expired; build a new one",
    );
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledWith(false);
  });

  it("shows an error when the preview cannot be built", async () => {
    const { onClose } = open({
      "POST /api/cleanup/preview": () =>
        apiError(403, "outside-roots", "that scan is outside the configured scan roots"),
    });
    expect((await screen.findByRole("alert")).textContent).toBe(
      "that scan is outside the configured scan roots",
    );
    await userEvent.setup().click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledWith(false);
  });
});
