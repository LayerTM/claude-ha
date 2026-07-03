/**
 * Claude chat card for Home Assistant.
 *
 * Talks to HA (not the add-on directly — the add-on port is internal-only) via
 * the `claude_ha.ask` service, which returns Claude's answer plus any structured
 * `proposal`. Read mode is always used for typed messages; a proposal is offered
 * as an inline Apply/Dismiss, and Apply issues the confirmed `mode: "write"` call
 * with the proposal's intents.
 */
const CARD_VERSION = "0.2.0";

class ClaudeChatCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._messages = [];
    this._busy = false;
    this._built = false;
  }

  setConfig(config) {
    this._config = config || {};
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._built) this._build();
  }

  getCardSize() {
    return 8;
  }

  static getStubConfig() {
    return { title: "Claude" };
  }

  _build() {
    this._built = true;
    const style = document.createElement("style");
    style.textContent = `
      ha-card { display: flex; flex-direction: column; height: 100%; }
      .header { padding: 12px 16px; font-weight: 600; border-bottom: 1px solid var(--divider-color); }
      .log { flex: 1; overflow-y: auto; padding: 12px 16px; display: flex; flex-direction: column; gap: 10px; min-height: 220px; }
      .msg { max-width: 85%; padding: 8px 12px; border-radius: 14px; white-space: pre-wrap; word-wrap: break-word; line-height: 1.35; }
      .user { align-self: flex-end; background: #d97757; color: #fff; border-bottom-right-radius: 4px; }
      .assistant { align-self: flex-start; background: var(--secondary-background-color); border-bottom-left-radius: 4px; }
      .error { align-self: flex-start; background: var(--error-color, #db4437); color: #fff; }
      .empty { color: var(--secondary-text-color); align-self: center; margin: auto 0; }
      .proposal { align-self: flex-start; max-width: 85%; border: 1px solid #d97757; border-radius: 12px; padding: 10px 12px; }
      .proposal .sum { font-weight: 600; }
      .proposal .targets { color: var(--secondary-text-color); font-size: 0.9em; margin: 4px 0 8px; }
      .proposal .acts { display: flex; gap: 8px; }
      button.act { border: none; border-radius: 8px; padding: 6px 14px; cursor: pointer; font: inherit; }
      button.apply { background: #d97757; color: #fff; }
      button.dismiss { background: var(--secondary-background-color); color: var(--primary-text-color); }
      .done { color: var(--secondary-text-color); font-size: 0.9em; margin-top: 6px; }
      .bar { display: flex; gap: 8px; padding: 12px 16px; border-top: 1px solid var(--divider-color); }
      .bar input { flex: 1; padding: 10px 12px; border-radius: 10px; border: 1px solid var(--divider-color); background: var(--card-background-color); color: var(--primary-text-color); font: inherit; }
      .bar button { border: none; border-radius: 10px; padding: 0 16px; background: #d97757; color: #fff; cursor: pointer; font: inherit; }
      .bar button:disabled, .bar input:disabled { opacity: 0.6; cursor: default; }
    `;

    const card = document.createElement("ha-card");
    const header = document.createElement("div");
    header.className = "header";
    header.textContent = this._config.title || "Claude";

    this._log = document.createElement("div");
    this._log.className = "log";

    const bar = document.createElement("div");
    bar.className = "bar";
    this._input = document.createElement("input");
    this._input.type = "text";
    this._input.placeholder = "Ask Claude…";
    this._input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") this._send();
    });
    this._sendBtn = document.createElement("button");
    this._sendBtn.textContent = "Send";
    this._sendBtn.addEventListener("click", () => this._send());
    bar.append(this._input, this._sendBtn);

    card.append(header, this._log, bar);
    this.shadowRoot.append(style, card);
    this._renderLog();
  }

  _setBusy(busy) {
    this._busy = busy;
    this._input.disabled = busy;
    this._sendBtn.disabled = busy;
    this._sendBtn.textContent = busy ? "…" : "Send";
    if (!busy) this._input.focus();
  }

  async _send() {
    const text = this._input.value.trim();
    if (!text || this._busy) return;
    this._input.value = "";
    this._messages.push({ role: "user", text });
    this._renderLog();
    await this._ask({ prompt: text });
  }

  async _ask(data) {
    this._setBusy(true);
    try {
      const result = await this._hass.callService(
        "claude_ha",
        "ask",
        data,
        undefined,
        false,
        true,
      );
      const resp = (result && result.response) || {};
      this._messages.push({
        role: "assistant",
        text: resp.text || "",
        proposal: resp.proposal || null,
        prompt: data.prompt,
      });
    } catch (err) {
      this._messages.push({
        role: "error",
        text: (err && err.message) || String(err),
      });
    } finally {
      this._setBusy(false);
      this._renderLog();
    }
  }

  async _apply(msg) {
    if (!msg.proposal || msg.applied || msg.dismissed) return;
    msg.applied = true;
    this._renderLog();
    await this._ask({
      prompt: msg.prompt,
      mode: "write",
      intents: msg.proposal.intents || [],
    });
  }

  _dismiss(msg) {
    msg.dismissed = true;
    this._renderLog();
  }

  _renderLog() {
    if (!this._log) return;
    this._log.textContent = "";
    if (this._messages.length === 0) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "Ask Claude anything about your home.";
      this._log.append(empty);
      return;
    }
    for (const msg of this._messages) {
      if (msg.text) {
        const bubble = document.createElement("div");
        bubble.className = `msg ${msg.role}`;
        bubble.textContent = msg.text;
        this._log.append(bubble);
      }
      if (msg.proposal && (msg.proposal.summary || (msg.proposal.intents || []).length)) {
        this._log.append(this._renderProposal(msg));
      }
    }
    this._log.scrollTop = this._log.scrollHeight;
  }

  _renderProposal(msg) {
    const box = document.createElement("div");
    box.className = "proposal";
    const sum = document.createElement("div");
    sum.className = "sum";
    sum.textContent = msg.proposal.summary || "Claude proposes a change.";
    box.append(sum);

    const targets = [];
    for (const intent of msg.proposal.intents || []) {
      for (const t of intent.targets || []) targets.push(t);
    }
    if (targets.length) {
      const tgt = document.createElement("div");
      tgt.className = "targets";
      tgt.textContent = `Affects: ${[...new Set(targets)].join(", ")}`;
      box.append(tgt);
    }

    if (msg.applied) {
      const done = document.createElement("div");
      done.className = "done";
      done.textContent = "Applied.";
      box.append(done);
    } else if (msg.dismissed) {
      const done = document.createElement("div");
      done.className = "done";
      done.textContent = "Dismissed.";
      box.append(done);
    } else {
      const acts = document.createElement("div");
      acts.className = "acts";
      const apply = document.createElement("button");
      apply.className = "act apply";
      apply.textContent = "Apply";
      apply.addEventListener("click", () => this._apply(msg));
      const dismiss = document.createElement("button");
      dismiss.className = "act dismiss";
      dismiss.textContent = "Dismiss";
      dismiss.addEventListener("click", () => this._dismiss(msg));
      acts.append(apply, dismiss);
      box.append(acts);
    }
    return box;
  }
}

if (!customElements.get("claude-chat-card")) {
  customElements.define("claude-chat-card", ClaudeChatCard);
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "claude-chat-card",
  name: "Claude Chat",
  description: "Chat with Claude and apply its suggestions.",
  preview: false,
});

console.info(
  `%c CLAUDE-CHAT-CARD %c v${CARD_VERSION} `,
  "color: #fff; background: #d97757; border-radius: 3px 0 0 3px;",
  "color: #d97757; background: #2b2622; border-radius: 0 3px 3px 0;",
);
