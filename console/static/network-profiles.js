(() => {
  const normalize = (value, fallback) => (value || "").trim() || fallback;

  document.querySelectorAll("[data-network-driver-guide]").forEach((guide) => {
    const form = guide.closest("form");
    const driver = form?.querySelector('select[name="driver"]');
    const interfaceInput = form?.querySelector('input[name="interface_name"]');
    const selector = form?.querySelector('select[name="selector"]');
    const destinationCidrs = form?.querySelector('textarea[name="destination_cidrs"]');
    const addressFamily = form?.querySelector('select[name="address_family"]');
    const authorization = form?.querySelector('input[name="require_authorization"]');
    const mode = form?.querySelector('select[name="mode"]');
    const uplink = form?.querySelector('input[name="host_interface"]');
    const state = guide.querySelector("[data-driver-state]");
    if (!form || !driver) return;

    const render = () => {
      const selected = driver.value;
      const kind = guide.dataset.networkDriverGuide;
      if (kind === "ingress") {
        const passive = selected !== "authorized-veth";
        if (interfaceInput) interfaceInput.value = selected === "none" ? "" : passive ? "transport0" : "di0";
        [selector, destinationCidrs, authorization].filter(Boolean).forEach((control) => {
          const label = control.closest("label");
          if (label) label.hidden = passive;
          control.disabled = passive;
        });
        if (authorization) {
          authorization.checked = !passive;
          // Authorization is determined by the driver, not an independent switch.
          authorization.disabled = true;
        }
        if (!passive && selector && !selector.value) selector.value = "source";
      }
      if (kind === "egress" && interfaceInput) {
        interfaceInput.value = selected === "none" ? "" : "do0";
        [mode, uplink].filter(Boolean).forEach((control) => {
          control.disabled = selected === "none";
          if (control.closest("label")) control.closest("label").hidden = selected === "none";
        });
      }
      if (interfaceInput) {
        interfaceInput.disabled = selected === "none";
        if (interfaceInput.closest("label")) interfaceInput.closest("label").hidden = selected === "none";
      }
      guide.querySelectorAll("[data-driver-panel]").forEach((panel) => {
        panel.hidden = panel.dataset.driverPanel !== selected;
      });
      guide.querySelectorAll("[data-selector-guide]").forEach((panel) => {
        panel.hidden = kind === "ingress" && selected !== "authorized-veth";
      });
      const interfaceName = normalize(interfaceInput?.value, "");
      const family = addressFamily?.value || "dual";
      const implemented = kind === "ingress"
        ? family === "dual" && (["unlimited-veth", "none"].includes(selected)
          || (selected === "authorized-veth" && selector?.value === "source"
          && interfaceName === "di0" && Boolean(authorization?.checked)))
        : family === "dual" && (selected === "none" || (selected === "veth-out" && interfaceName === "do0"));
      state.textContent = implemented ? "READY" : "DESIGN ONLY";
      state.classList.toggle("is-design", !implemented);

      const conventionalInterface = guide.dataset.networkDriverGuide === "ingress"
        ? "di0" : selected === "on-a-stick" ? "di0" : "do0";
      guide.querySelectorAll("[data-interface-label]").forEach((label) => {
        label.textContent = normalize(interfaceInput?.value, conventionalInterface);
      });
      guide.querySelectorAll("[data-egress-effect]").forEach((label) => {
        label.textContent = (mode?.value || "masquerade").toUpperCase();
      });
      guide.querySelectorAll("[data-uplink-label]").forEach((label) => {
        label.textContent = normalize(uplink?.value, "global route");
      });
      if (selector) {
        guide.querySelectorAll("[data-selector-panel]").forEach((panel) => {
          panel.hidden = panel.dataset.selectorPanel !== selector.value;
        });
        const selectorState = guide.querySelector("[data-selector-state]");
        if (selectorState) selectorState.textContent = selector.value.toUpperCase();
        const selectorLabels = {
          source: "source IP",
          destination: "destination CIDR",
          "source-destination": "source + destination",
        };
        guide.querySelectorAll("[data-driver-match-label]").forEach((label) => {
          label.textContent = selectorLabels[selector.value] || selector.value;
        });
        const firstCidr = (destinationCidrs?.value || "").split(/\r?\n/)
          .map((value) => value.trim()).find(Boolean);
        guide.querySelectorAll("[data-destination-label]").forEach((label) => {
          label.textContent = firstCidr || "destination CIDR";
        });
        guide.querySelectorAll("[data-family-label]").forEach((label) => {
          label.textContent = family.toUpperCase();
        });
        guide.querySelectorAll("[data-source-label]").forEach((label) => {
          label.textContent = authorization?.checked ? "authorized IP" : "source IP";
        });
      }
    };

    [driver, interfaceInput, mode, uplink, selector, destinationCidrs, addressFamily, authorization].filter(Boolean).forEach((control) => {
      control.addEventListener(control.tagName === "SELECT" ? "change" : "input", render);
    });
    render();
  });

})();
