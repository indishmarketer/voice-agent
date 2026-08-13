/* Embeddable voice agent widget.
 *
 * Usage on a customer's site:
 *   <script src="https://<your-domain>/embed.js" data-account="<slug>" async></script>
 *
 * Self-contained: injects its own floating button + iframe overlay, no
 * dependency on the host page's CSS or JS. The iframe loads the same full
 * call UI served at "/", scoped to this account via ?account=<slug> - so a
 * behaviour/UI change to the main call screen applies to embeds for free.
 */
(function () {
  var thisScript = document.currentScript;
  var account = thisScript ? thisScript.getAttribute("data-account") : "";
  if (!account) {
    console.error("[voice-agent embed] missing data-account attribute");
    return;
  }

  var origin = new URL(thisScript.src, location.href).origin;
  var open = false;

  var style = document.createElement("style");
  style.textContent = [
    "#va-embed-btn{position:fixed;bottom:20px;right:20px;width:60px;height:60px;",
    "border-radius:50%;background:#4f5bd5;box-shadow:0 4px 18px rgba(0,0,0,.25);",
    "border:none;cursor:pointer;z-index:2147483000;display:flex;align-items:center;",
    "justify-content:center;transition:transform .15s ease;}",
    "#va-embed-btn:hover{transform:scale(1.06);}",
    "#va-embed-btn svg{width:26px;height:26px;}",
    "#va-embed-panel{position:fixed;bottom:92px;right:20px;width:380px;",
    "max-width:calc(100vw - 32px);height:640px;max-height:calc(100vh - 120px);",
    "border-radius:18px;overflow:hidden;box-shadow:0 12px 40px rgba(0,0,0,.35);",
    "z-index:2147483000;display:none;background:#0b0c11;}",
    "#va-embed-panel.open{display:block;}",
    "#va-embed-panel iframe{width:100%;height:100%;border:0;}",
    "#va-embed-close{position:absolute;top:8px;right:8px;width:28px;height:28px;",
    "border-radius:50%;border:none;background:rgba(0,0,0,.35);color:#fff;",
    "cursor:pointer;z-index:1;font-size:16px;line-height:1;}",
    "@media (max-width:480px){#va-embed-panel{right:12px;left:12px;width:auto;bottom:84px;}}",
  ].join("");
  document.head.appendChild(style);

  var btn = document.createElement("button");
  btn.id = "va-embed-btn";
  btn.setAttribute("aria-label", "Open voice assistant");
  btn.innerHTML =
    '<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">' +
    '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" fill="#fff"/>' +
    '<path d="M19 11a7 7 0 0 1-14 0" stroke="#fff" stroke-width="2" stroke-linecap="round" fill="none"/>' +
    '<path d="M12 18v3" stroke="#fff" stroke-width="2" stroke-linecap="round"/></svg>';

  var panel = document.createElement("div");
  panel.id = "va-embed-panel";

  var closeBtn = document.createElement("button");
  closeBtn.id = "va-embed-close";
  closeBtn.innerHTML = "&times;";
  closeBtn.setAttribute("aria-label", "Close");

  var iframe = null;

  function togglePanel() {
    open = !open;
    panel.classList.toggle("open", open);
    if (open && !iframe) {
      iframe = document.createElement("iframe");
      iframe.src = origin + "/?account=" + encodeURIComponent(account);
      iframe.allow = "microphone";
      panel.appendChild(iframe);
      panel.appendChild(closeBtn);
    }
  }

  btn.addEventListener("click", togglePanel);
  closeBtn.addEventListener("click", togglePanel);

  function mount() {
    document.body.appendChild(btn);
    document.body.appendChild(panel);
  }
  if (document.readyState === "complete" || document.readyState === "interactive") {
    mount();
  } else {
    document.addEventListener("DOMContentLoaded", mount);
  }
})();
