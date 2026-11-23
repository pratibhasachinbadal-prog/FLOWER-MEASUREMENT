document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-password-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      const input = button.parentElement.querySelector("input");
      const reveal = input.type === "password";
      input.type = reveal ? "text" : "password";
      button.textContent = reveal ? "Hide" : "Show";
      button.setAttribute("aria-label", reveal ? "Hide password" : "Show password");
    });
  });

  document.querySelectorAll("[data-strength-input]").forEach((input) => {
    const meter = input.closest(".form-stack").querySelector(".password-strength");
    const fill = meter.querySelector("[data-strength-fill]");
    const label = meter.querySelector("[data-strength-label]");
    const updateStrength = () => {
      const value = input.value;
      if (!value) {
        meter.dataset.strength = "";
        fill.style.width = "0";
        label.textContent = "Enter a password to check its strength";
        return;
      }

      const score =
        (value.length >= 10 ? 2 : 0) +
        (value.length >= 14 ? 1 : 0) +
        (/[a-z]/.test(value) ? 1 : 0) +
        (/[A-Z]/.test(value) ? 1 : 0) +
        (/\d/.test(value) ? 1 : 0) +
        (/[^a-zA-Z0-9]/.test(value) ? 1 : 0);
      const strength =
        value.length < 10 || score <= 3
          ? "weak"
          : score <= 4
            ? "fair"
            : score <= 5
              ? "good"
              : "strong";
      meter.dataset.strength = strength;
      fill.style.width = `${Math.round((score / 7) * 100)}%`;
      label.textContent =
        strength === "weak"
          ? "Too short or easy to guess"
          : `${strength[0].toUpperCase()}${strength.slice(1)} password`;
    };

    input.addEventListener("input", updateStrength);
    updateStrength();
  });

  document.querySelectorAll("[data-sidebar-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelector("#sidebar")?.classList.toggle("open");
    });
  });

  document.addEventListener("click", (event) => {
    const sidebar = document.querySelector("#sidebar");
    if (
      sidebar?.classList.contains("open") &&
      !sidebar.contains(event.target) &&
      !event.target.closest("[data-sidebar-toggle]")
    ) {
      sidebar.classList.remove("open");
    }
  });

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  document.querySelectorAll("[data-file-name]").forEach((input) => {
    input.addEventListener("change", () => {
      const label = document.querySelector("[data-file-label]");
      if (label) label.textContent = input.files?.[0]?.name || "No file selected";
    });
  });

  document.querySelectorAll("[data-count]").forEach((element) => {
    const target = Number(element.dataset.count);
    if (!Number.isFinite(target) || target <= 0) return;
    const start = performance.now();
    const duration = 550;
    const animate = (now) => {
      const progress = Math.min((now - start) / duration, 1);
      element.textContent = String(Math.round(target * (1 - (1 - progress) ** 3)));
      if (progress < 1) requestAnimationFrame(animate);
    };
    requestAnimationFrame(animate);
  });
});
