// The cat that lives along the bottom of the top bar (drawn in _cat.html, styled in app.css).
// It rests (sits and stares, or naps), then strolls to a random spot, at random intervals.
// Clicking it makes it blink at you, waking it up or stopping a walk. Position and mood
// survive page loads.
//
// Walking is moved frame by frame here, in step with the walk pose, rather than by a CSS
// transition: with a transition, a background tab could pause the timers but not the slide
// (or the other way round), so the cat kept sliding after it had stopped walking.
(() => {
  const cat = document.querySelector(".cat");
  if (!cat) return;
  const lane = cat.parentElement;
  const KEY = "budget-cat";
  const SPEED = 55; // px per second
  const BOX = 100; // the cat's width in px
  const MAX_STEP_MS = 50; // a long gap between frames (a hidden tab) doesn't jump it ahead
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const rand = (lo, hi) => lo + Math.random() * (hi - lo);
  let timer = 0;
  let frame = 0;
  let pokeTimer = 0;
  let state = load();

  function load() {
    try {
      const saved = JSON.parse(localStorage.getItem(KEY));
      if (saved && typeof saved.x === "number") return saved;
    } catch {
      // storage blocked or unreadable: start fresh
    }
    return { x: Math.random(), pose: "sit", facing: "right", until: 0 };
  }

  function save() {
    try {
      localStorage.setItem(KEY, JSON.stringify(state));
    } catch {
      // not saved; it just starts fresh next page
    }
  }

  const room = () => Math.max(0, lane.clientWidth - BOX);

  // A CSS variable set through the CSSOM, which the CSP allows (no inline styles).
  function place() {
    cat.style.setProperty("--x", `${Math.round(state.x * room())}px`);
  }

  function show(pose) {
    state.pose = pose;
    cat.dataset.pose = pose;
    cat.dataset.facing = state.facing;
    save();
  }

  function later(ms, fn) {
    clearTimeout(timer);
    timer = setTimeout(fn, Math.max(0, ms));
  }

  function stop() {
    clearTimeout(timer);
    cancelAnimationFrame(frame);
  }

  function rest(ms = rand(8000, 40000), pose = Math.random() < 0.5 ? "sit" : "nap") {
    stop();
    state.until = Date.now() + ms;
    show(pose);
    later(ms, walk);
  }

  function walk() {
    if (reduced || room() < BOX) return rest();
    const from = state.x;
    let target = Math.random();
    if (Math.abs(target - from) * room() < 120) {
      target = from > 0.5 ? rand(0, 0.35) : rand(0.65, 1);
    }
    state.facing = target > from ? "right" : "left";
    show("stand"); // a stretch before setting off
    later(600, () => {
      show("walk");
      const distance = Math.abs(target - from) * room();
      let travelled = 0;
      let last = null;
      let saved = 0;
      const step = (now) => {
        if (state.pose !== "walk") return; // stopped by a click
        travelled += (SPEED * Math.min(last === null ? 0 : now - last, MAX_STEP_MS)) / 1000;
        last = now;
        const done = distance === 0 || travelled >= distance;
        state.x = done ? target : from + (target - from) * (travelled / distance);
        place();
        if (!done) {
          if (now - saved > 500) {
            save(); // so a page change mid-walk picks up about where it was
            saved = now;
          }
          frame = requestAnimationFrame(step);
          return;
        }
        show("stand");
        later(500, () => rest());
      };
      frame = requestAnimationFrame(step);
    });
  }

  cat.addEventListener("click", () => {
    stop();
    clearTimeout(pokeTimer);
    cat.classList.remove("poked");
    void cat.offsetWidth; // restart the blink and head-tilt animations
    cat.classList.add("poked");
    pokeTimer = setTimeout(() => cat.classList.remove("poked"), 1000);
    rest(rand(6000, 15000), "sit"); // sits, blinks and looks at you
  });
  window.addEventListener("resize", place);
  window.addEventListener("pagehide", save);

  // Pick up where it left off on the previous page (a walk in progress ends where it was).
  place();
  if (state.pose !== "sit" && state.pose !== "nap") state.pose = "sit";
  const remaining = state.until - Date.now();
  if (remaining > 0) {
    show(state.pose);
    later(remaining, walk);
  } else {
    rest(rand(1500, 5000), state.pose);
  }
})();
