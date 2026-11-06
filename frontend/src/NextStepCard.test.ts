import { describe, expect, it } from "vitest";
import { deriveNextStep, type NextStepState } from "./NextStepCard";

const done: NextStepState = {
  requirements: { total: 4, confirmed: 4 },
  architecture: { drafts: 0, approved: 1 },
  plan: { drafts: 0, approved: 1 },
  ciApprovalPending: false,
  proposedPatch: false,
  changeToReview: false,
  openTask: null,
  acceptedPatches: 0,
  repositoryConnected: true,
};

const step = (over: Partial<NextStepState>) => deriveNextStep({ ...done, ...over }).id;

describe("deriveNextStep", () => {
  it("covers each state in priority order", () => {
    expect(step({ requirements: { total: 3, confirmed: 0 } })).toBe("confirm-requirements");
    expect(step({ architecture: { drafts: 1, approved: 0 } })).toBe("approve-architecture");
    expect(step({ plan: { drafts: 1, approved: 0 } })).toBe("approve-plan");
    expect(step({ ciApprovalPending: true, proposedPatch: true })).toBe("approve-ci-branch");
    expect(step({ proposedPatch: true })).toBe("review-patch");
    expect(step({ changeToReview: true })).toBe("review-change");
    expect(step({ openTask: { key: "T-001", checklist: "none" } })).toBe("propose-checklist");
    expect(step({ openTask: { key: "T-001", checklist: "proposed" } })).toBe("approve-checklist");
    expect(step({ openTask: { key: "T-001", checklist: "approved" } })).toBe("run-next-step");
    expect(step({ acceptedPatches: 2 })).toBe("push-pr");
  });

  it("asks to connect a repository before pushing", () => {
    const s = deriveNextStep({ ...done, acceptedPatches: 1, repositoryConnected: false });
    expect(s.page).toBe("repository");
  });

  it("offers one primary action", () => {
    const s = deriveNextStep({ ...done, proposedPatch: true });
    expect(s.action).toBe("Review patch");
  });
});
