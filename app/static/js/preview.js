// Shared by every page that shows a signature in an <iframe data-signature-frame>:
// keeps each frame exactly as tall as the signature inside it.
window.FooterPilot = window.FooterPilot || {};

FooterPilot.fitFrame = function (frame) {
  const body = frame.contentDocument && frame.contentDocument.body;
  if (body) frame.style.height = body.scrollHeight + "px";
};

// Call once on a container. Covers frames already inside it and any that HTMX swaps
// in later. `load` does not bubble, so it is caught in the capture phase.
FooterPilot.watchPreviewFrames = function (root) {
  root.addEventListener(
    "load",
    (event) => {
      const target = event.target;
      if (target.matches && target.matches("[data-signature-frame]")) FooterPilot.fitFrame(target);
    },
    true,
  );
  root.querySelectorAll("[data-signature-frame]").forEach((frame) => {
    // A frame may have finished loading before this ran.
    if (frame.contentDocument && frame.contentDocument.readyState === "complete") {
      FooterPilot.fitFrame(frame);
    }
  });
};
