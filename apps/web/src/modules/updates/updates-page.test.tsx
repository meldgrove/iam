import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { UpdatesPage } from "./updates-page";

const admin = vi.hoisted(() => ({ value: false }));
vi.mock("@/hooks/use-me", () => ({ useIsAdmin: () => admin.value }));

describe("UpdatesPage", () => {
  it("renders nothing once the admin role is gone, as AdminUpdates drops the update status", () => {
    admin.value = false;
    const { container } = render(<UpdatesPage />);
    expect(container).toBeEmptyDOMElement();
  });
});
