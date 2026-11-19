// Behaviour for the public signature generator: copying the signature. Needs
// preview.js. Loaded before Alpine so the component is registered when Alpine starts.
document.addEventListener("alpine:init", () => {
  Alpine.data("signatureGenerator", () => ({
    copied: null, // "signature" | "source" | null, drives the button labels
    error: "",

    init() {
      // Keep the preview frame as tall as the signature (see preview.js).
      FooterPilot.watchPreviewFrames(this.$root);
    },

    frame() {
      return this.$root.querySelector("[data-signature-frame]");
    },

    html() {
      const source = this.$root.querySelector("[data-signature-html]");
      return source ? source.value : "";
    },

    failed() {
      this.error = "The preview could not be updated. Please wait a moment and try again.";
    },

    flash(kind) {
      this.error = "";
      this.copied = kind;
      setTimeout(() => (this.copied = null), 2000);
    },

    // Copies formatted HTML (with a plain-text alternative) so it pastes into a mail
    // client's signature box looking like the preview.
    async copySignature() {
      const html = this.html();
      if (!html) return;
      const frame = this.frame();
      const text = (frame && frame.contentDocument && frame.contentDocument.body.innerText) || "";
      // The table layout leaves tabs and blank lines in innerText; tidy them away.
      const plain = text
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean)
        .join("\n");
      try {
        if (!navigator.clipboard || !window.ClipboardItem) throw new Error("unsupported");
        await navigator.clipboard.write([
          new ClipboardItem({
            "text/html": new Blob([html], { type: "text/html" }),
            "text/plain": new Blob([plain], { type: "text/plain" }),
          }),
        ]);
      } catch {
        if (!this.copyFrameSelection()) {
          this.error = "Copying is blocked in this browser. Select the preview and copy it manually.";
          return;
        }
      }
      this.flash("signature");
    },

    // Fallback for browsers without the async clipboard API.
    copyFrameSelection() {
      const frame = this.frame();
      const doc = frame && frame.contentDocument;
      if (!doc) return false;
      try {
        const range = doc.createRange();
        range.selectNodeContents(doc.body);
        const selection = doc.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        const done = doc.execCommand("copy");
        selection.removeAllRanges();
        return done;
      } catch {
        return false;
      }
    },

    async copySource() {
      const html = this.html();
      if (!html) return;
      try {
        await navigator.clipboard.writeText(html);
      } catch {
        this.error = "Copying is blocked in this browser.";
        return;
      }
      this.flash("source");
    },
  }));
});
