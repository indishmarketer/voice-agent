/* Embeddable voice agent widget.
 *
 * Usage on a customer's site - works in <head> or <body>, with or without
 * async/defer, and works even when a site builder's "custom code" box
 * injects it via JS rather than raw HTML (see findTag() below):
 *
 *   <script src="https://<your-domain>/embed.js"
 *           data-account="<slug>"   (omit this for the main/platform account)
 *           data-label="Voice Agent"   (optional, shown under the button)
 *           data-position="bottom-right"  (optional: bottom-right|bottom-left)
 *           async></script>
 *
 * Self-contained: injects its own button + centered modal, no dependency on
 * the host page's CSS or JS, and namespaced so it cannot collide with the
 * host's own styles. The modal loads the same full call UI served at "/",
 * scoped to this account via ?account=<slug> (or unscoped, for the main
 * account) - a behaviour/UI change to the main call screen applies to
 * every embed for free.
 */
(function () {
  if (window.__voiceAgentEmbedLoaded) return; // idempotent if pasted twice
  window.__voiceAgentEmbedLoaded = true;

  function findTag() {
    // document.currentScript is only reliable for a script executing as
    // part of the initial synchronous HTML parse. Several site builders'
    // "custom code" boxes inject the tag via JS instead, which leaves
    // currentScript null - fall back to searching the DOM by src, which
    // survives either way and does not depend on data-account being set
    // (the main account's own embed has no slug to key off of).
    if (document.currentScript) return document.currentScript;
    var tags = document.querySelectorAll('script[src*="embed.js"]');
    return tags.length ? tags[tags.length - 1] : null;
  }

  var tag = findTag();
  if (!tag) {
    console.error("[voice-agent embed] could not find its own <script> tag - " +
                  "the widget will not appear.");
    return;
  }
  var account = tag.getAttribute("data-account") || ""; // "" = the main account
  var label = tag.getAttribute("data-label") || "Voice Agent";
  var position = tag.getAttribute("data-position") === "bottom-left" ? "left" : "right";
  var origin = new URL(tag.src, location.href).origin;

  var style = document.createElement("style");
  style.textContent = [
    "#va-embed-launcher{position:fixed;bottom:20px;" + position + ":20px;",
    "z-index:2147483000;display:flex;flex-direction:column;align-items:center;",
    "gap:4px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;}",
    "#va-embed-btn{width:56px;height:56px;border-radius:50%;background:#4f5bd5;",
    "box-shadow:0 4px 18px rgba(0,0,0,.28);border:none;cursor:pointer;",
    "display:flex;align-items:center;justify-content:center;",
    "transition:transform .15s ease;padding:0;}",
    "#va-embed-btn:hover{transform:scale(1.06);}",
    "#va-embed-btn svg{width:24px;height:24px;}",
    "#va-embed-label{font-size:11px;font-weight:600;color:#fff;",
    "background:rgba(20,20,30,.72);padding:3px 9px;border-radius:99px;",
    "white-space:nowrap;max-width:120px;overflow:hidden;text-overflow:ellipsis;",
    "pointer-events:none;}",
    "#va-embed-overlay{position:fixed;inset:0;background:rgba(10,10,15,.55);",
    "z-index:2147483001;display:none;align-items:center;justify-content:center;",
    "padding:24px;box-sizing:border-box;}",
    "#va-embed-overlay.open{display:flex;}",
    "#va-embed-modal{position:relative;width:380px;max-width:100%;height:640px;",
    "max-height:calc(100vh - 48px);border-radius:20px;overflow:hidden;",
    "box-shadow:0 20px 60px rgba(0,0,0,.45);background:#0b0c11;}",
    "#va-embed-modal iframe{width:100%;height:100%;border:0;display:block;}",
    "#va-embed-close{position:absolute;top:10px;right:10px;width:30px;height:30px;",
    "border-radius:50%;border:none;background:rgba(0,0,0,.4);color:#fff;",
    "cursor:pointer;z-index:1;font-size:18px;line-height:1;",
    "display:flex;align-items:center;justify-content:center;}",
    "@media (max-width:520px){",
    "#va-embed-overlay{padding:0;}",
    "#va-embed-modal{width:100vw;height:100vh;max-height:100vh;border-radius:0;}",
    "}",
  ].join("");
  document.head.appendChild(style);

  var launcher = document.createElement("div");
  launcher.id = "va-embed-launcher";
  launcher.innerHTML =
    '<button id="va-embed-btn" type="button" aria-label="' + label + '">' +
    '<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">' +
    '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" fill="#fff"/>' +
    '<path d="M19 11a7 7 0 0 1-14 0" stroke="#fff" stroke-width="2" stroke-linecap="round" fill="none"/>' +
    '<path d="M12 18v3" stroke="#fff" stroke-width="2" stroke-linecap="round"/></svg>' +
    "</button>" +
    '<span id="va-embed-label"></span>';
  // textContent, not innerHTML, for the customer-supplied label - avoids
  // interpreting anything they put in data-label as markup.
  launcher.querySelector("#va-embed-label").textContent = label;

  var overlay = document.createElement("div");
  overlay.id = "va-embed-overlay";
  overlay.innerHTML =
    '<div id="va-embed-modal">' +
    '<button id="va-embed-close" type="button" aria-label="Close">&times;</button>' +
    "</div>";

  var modal = overlay.querySelector("#va-embed-modal");
  var iframe = null;

  function open() {
    overlay.classList.add("open");
    if (!iframe) {
      iframe = document.createElement("iframe");
      iframe.src = account ? origin + "/?account=" + encodeURIComponent(account) : origin + "/";
      iframe.allow = "microphone";
      modal.appendChild(iframe);
    }
  }
  function close() {
    overlay.classList.remove("open");
  }

  launcher.querySelector("#va-embed-btn").addEventListener("click", open);
  overlay.querySelector("#va-embed-close").addEventListener("click", close);
  overlay.addEventListener("click", function (e) {
    if (e.target === overlay) close(); // click on the backdrop, not the modal itself
  });

  function mount() {
    document.body.appendChild(launcher);
    document.body.appendChild(overlay);
  }
  if (document.readyState === "complete" || document.readyState === "interactive") {
    mount();
  } else {
    document.addEventListener("DOMContentLoaded", mount);
  }
})();
