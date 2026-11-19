// Behaviour for the signature designer. Needs preview.js. Loaded before Alpine so the
// component is registered when Alpine starts.
document.addEventListener("alpine:init", () => {
  Alpine.data("signatureDesigner", () => ({
    init() {
      FooterPilot.watchPreviewFrames(this.$root);
    },

    // Previous / next in the "Preview as" list, wrapping round at the ends.
    step(delta) {
      const select = this.$refs.previewAs;
      const count = select.options.length;
      if (!count) return;
      select.selectedIndex = (select.selectedIndex + delta + count) % count;
      // Changing a select from script fires no event, so tell the form to save and re-render.
      select.dispatchEvent(new Event("input", { bubbles: true }));
    },
  }));
});
