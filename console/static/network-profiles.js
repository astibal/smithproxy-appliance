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
      if (kind === "egress" && interfaceInput) {
        interfaceInput.value = selected === "on-a-stick" ? "di0" : "do0";
      }
      if (kind === "egress" && ["tuntom", "tuntom-via"].includes(selected) && mode) {
        mode.value = "routed";
      }
      form.querySelectorAll("[data-tuntom-fields]").forEach((fields) => {
        const tuntomSelected = ["tuntom", "tuntom-via"].includes(selected);
        fields.hidden = !tuntomSelected;
        fields.querySelectorAll("input, select, textarea").forEach((control) => {
          const viaOnly = control.closest("[data-via-only]");
          control.disabled = !tuntomSelected || (Boolean(viaOnly) && selected !== "tuntom-via");
        });
        fields.querySelectorAll("[data-via-only]").forEach((item) => {
          item.hidden = selected !== "tuntom-via";
        });
        fields.querySelectorAll("[data-direct-only]").forEach((item) => {
          item.hidden = selected !== "tuntom";
        });
      });
      guide.querySelectorAll("[data-driver-panel]").forEach((panel) => {
        panel.hidden = panel.dataset.driverPanel !== selected;
      });
      guide.querySelectorAll("[data-selector-guide]").forEach((panel) => {
        panel.hidden = kind === "ingress" && selected !== "split-veth";
      });
      const interfaceName = normalize(interfaceInput?.value, "");
      const family = addressFamily?.value || "dual";
      const implemented = kind === "ingress"
        ? selected === "split-veth" && selector?.value === "source"
          && interfaceName === "di0" && Boolean(authorization?.checked) && family === "dual"
        : ["split-veth", "on-a-stick", "tuntom-via"].includes(selected)
          && interfaceName === (selected === "on-a-stick" ? "di0" : "do0")
          && family === "dual";
      const viaShadow = kind === "ingress" && selected === "tuntom-via";
      state.textContent = viaShadow ? "LOCKED BY VIA" : implemented ? "READY" : "DESIGN ONLY";
      state.classList.toggle("is-design", !implemented && !viaShadow);

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

  document.querySelectorAll("[data-network-profile-compose]").forEach((compose) => {
    const ingressForm = compose.querySelector('[data-profile-side="ingress"]');
    const egressForm = compose.querySelector('[data-profile-side="egress"]');
    const ingressDriver = ingressForm?.querySelector('select[name="driver"]');
    const egressDriver = egressForm?.querySelector('select[name="driver"]');
    if (!ingressForm || !egressForm || !ingressDriver || !egressDriver) return;
    let previousIngressDriver = ingressDriver.value;

    const lockIngress = (locked) => {
      if (locked) {
        if (ingressDriver.value !== "tuntom-via") previousIngressDriver = ingressDriver.value;
        ingressForm.classList.add("network-profile-side-locked");
        ingressDriver.value = "tuntom-via";
        ingressDriver.dispatchEvent(new Event("change", {bubbles: true}));
        ingressForm.querySelectorAll("input,select,textarea,button").forEach((control) => {
          if (control.matches('[name="csrf_token"],[name="kind"]')) return;
          control.disabled = true;
        });
      } else {
        ingressForm.classList.remove("network-profile-side-locked");
        ingressForm.querySelectorAll("input,select,textarea,button").forEach((control) => {
          control.disabled = false;
        });
        if (ingressDriver.value === "tuntom-via") {
          ingressDriver.value = previousIngressDriver || "split-veth";
        }
        ingressDriver.dispatchEvent(new Event("change", {bubbles: true}));
      }
      ingressForm.setAttribute("aria-disabled", String(locked));
      const eyebrow = ingressForm.querySelector(".section-head .eyebrow");
      if (eyebrow) eyebrow.textContent = locked ? "INGRESS · VIA LOCKED" : "INGRESS";
    };
    const sync = () => {
      const via = egressDriver.value === "tuntom-via";
      compose.classList.toggle("network-profile-compose-via", via);
      lockIngress(via);
    };
    egressDriver.addEventListener("change", sync);
    sync();
  });
})();
