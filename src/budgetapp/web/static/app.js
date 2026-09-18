// Served from 'self' only (CSP forbids inline scripts).

// Forms with data-confirm ask before submitting (used for deletes).
document.addEventListener("submit", (event) => {
  const message = event.target.dataset.confirm;
  if (message && !window.confirm(message)) {
    event.preventDefault();
  }
});

// Controls with data-autosubmit save as soon as they change (e.g. a transaction's category).
document.addEventListener("change", (event) => {
  const control = event.target;
  if (control.matches("[data-autosubmit]") && control.form) {
    control.form.requestSubmit();
  }
});

// Money rains when a loan is paid off: the server flashes a "celebrate" message.
// Each bill sets CSS variables through the CSSOM, which the CSP allows (no inline styles).
function rainMoney(count = 70) {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const layer = document.createElement("div");
  layer.className = "money-rain";
  layer.setAttribute("aria-hidden", "true");
  const kinds = ["💵", "💵", "💸", "💰"];
  let longest = 0;
  for (let i = 0; i < count; i++) {
    const bill = document.createElement("span");
    bill.textContent = kinds[i % kinds.length];
    const duration = 2.6 + Math.random() * 2.4;
    const delay = Math.random() * (count > 100 ? 3.5 : 2);
    longest = Math.max(longest, duration + delay);
    bill.style.setProperty("--x", `${Math.random() * 100}vw`);
    bill.style.setProperty("--size", `${22 + Math.random() * 26}px`);
    bill.style.setProperty("--drift", `${(Math.random() - 0.5) * 30}vw`);
    bill.style.setProperty("--spin", `${(Math.random() - 0.5) * 720}deg`);
    bill.style.animationDuration = `${duration}s`;
    bill.style.animationDelay = `${delay}s`;
    layer.append(bill);
  }
  document.body.append(layer);
  setTimeout(() => layer.remove(), (longest + 0.5) * 1000);
}
window.rainMoney = rainMoney;

// Home-page background video: plays muted on a loop, pauses while the tab is hidden, and
// stays on its first frame when the system asks for reduced motion.
{
  const backdrop = document.querySelector(".backdrop-video");
  if (backdrop) {
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const play = () => {
      if (!still && !document.hidden) backdrop.play().catch(() => {});
    };
    document.addEventListener("visibilitychange", () => (document.hidden ? backdrop.pause() : play()));
    play();
  }
}

document.addEventListener("DOMContentLoaded", () => {
  if (document.querySelector(".flash-celebrate-big")) rainMoney(160);
  else if (document.querySelector(".flash-celebrate")) rainMoney(70);
});

// A button with data-print prints the page (used for the recovery code sheet).
document.addEventListener("click", (event) => {
  if (event.target.closest("[data-print]")) {
    window.print();
  }
});
