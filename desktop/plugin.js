import { Button, ROUTES_AREA, SIDEBAR_NAV_AREA, host } from "@hermes/plugin-sdk";
import { createElement as h } from "react";

// Wallet approval stays in the user's browser and the local buyer gateway.
// This SDK page never receives wallet keys, provider tokens or buyer sessions.
export default {
  id: "hermes-sell",
  name: "Hermes Sell",
  register(ctx) {
    const destinations = [
      [
        "Browse",
        "Find services from different sellers.",
        "http://127.0.0.1:8795",
      ],
      [
        "My services",
        "Create, test, publish and list your own services.",
        "http://127.0.0.1:8786",
      ],
      [
        "Purchases",
        "Review approvals, results and receipts across sellers.",
        "http://127.0.0.1:8767/purchases",
      ],
    ];
    function Page() {
      return h(
        "div",
        { className: "h-full overflow-auto p-6 text-sm" },
        h("h1", { className: "mb-3 text-xl font-semibold" }, "Hermes Sell"),
        h(
          "p",
          { className: "mb-6 text-(--ui-text-secondary)" },
          "Turn your skills into paid APIs. Discover and buy services from other Hermes users.",
        ),
        ...destinations.map(([label, description, url]) =>
          h(
            "section",
            {
              key: label,
              className: "mb-6 rounded border border-(--ui-stroke-primary) p-4",
            },
            h("h2", { className: "mb-2 font-semibold" }, label),
            h(
              "p",
              { className: "mb-3 text-(--ui-text-secondary)" },
              description,
            ),
            h(
              Button,
              {
                type: "button",
                onClick: async () => {
                  if (!(await ctx.os.openExternal(url)))
                    host.notify({
                      kind: "info",
                      message: "Open " + url + " in your browser.",
                    });
                },
              },
              "Open " + label,
            ),
            h(
              "p",
              { className: "mt-2 text-xs text-(--ui-text-tertiary)" },
              url,
            ),
          ),
        ),
        h(
          "p",
          { className: "text-(--ui-text-tertiary)" },
          "Start the local seller, directory and buyer gateway first. These default workspace addresses open in your browser. Base Sepolia uses test USDC; simulation uses fixed examples.",
        ),
      );
    }
    ctx.registerMany([
      {
        id: "page",
        area: ROUTES_AREA,
        data: { path: "/hermes-sell" },
        render: () => h(Page),
      },
      {
        id: "navigation",
        area: SIDEBAR_NAV_AREA,
        data: { path: "/hermes-sell", label: "Sell", codicon: "extensions" },
      },
    ]);
  },
};
