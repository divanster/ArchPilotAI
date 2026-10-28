import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Modal, StatusBadge } from "./ui";
import { hrefFor, parseHash } from "./routes";

describe("Modal", () => {
  it("is an accessible dialog that closes on Escape and moves focus inside", () => {
    const onClose = vi.fn();
    render(
      <Modal title="Demo" onClose={onClose}>
        <input aria-label="Field" />
      </Modal>,
    );
    const dialog = screen.getByRole("dialog", { name: "Demo" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Close Demo" }));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("keeps Tab focus inside the dialog", () => {
    render(
      <Modal title="Demo" onClose={() => {}}>
        <input aria-label="Field" />
      </Modal>,
    );
    const field = screen.getByLabelText("Field");
    field.focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Close Demo" }));
  });
});

describe("StatusBadge", () => {
  it("keeps the raw status text for each review state", () => {
    render(
      <>
        <StatusBadge status="proposed" testId="a" />
        <StatusBadge status="confirmed" testId="b" />
        <StatusBadge status="rejected" testId="c" />
      </>,
    );
    expect(screen.getByTestId("a")).toHaveTextContent("proposed");
    expect(screen.getByTestId("b")).toHaveClass("badge-confirmed");
    expect(screen.getByTestId("c")).toHaveClass("badge-rejected");
  });
});

describe("routes", () => {
  it("parses and builds hash routes", () => {
    expect(parseHash("#/projects/abc")).toEqual({
      page: "projects",
      arg: "abc",
    });
    expect(parseHash("")).toEqual({ page: "dashboard" });
    expect(parseHash("#/documents/p1/d%201")).toEqual({
      page: "documents",
      arg: "p1",
      sub: "d 1",
    });
    expect(hrefFor("documents", "p1", "d 1")).toBe("#/documents/p1/d%201");
    expect(parseHash("#/nope")).toEqual({ page: "dashboard" });
    expect(hrefFor("documents", "a b")).toBe("#/documents/a%20b");
    expect(hrefFor("playground")).toBe("#/playground");
  });
});

